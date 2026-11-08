"""Static analysis of a web project's source code (routes, pages, forms, UI text)."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .config import settings
from .llm import get_llm

IGNORE_DIRS = {
    "node_modules", ".git", "dist", "build", ".next", ".nuxt", "out", "coverage", "__pycache__", ".venv",
    "venv", "target", ".idea", ".vscode", "vendor", ".cache", "public", "static", "assets", "migrations",
    ".turbo", ".output", "bin", "obj",
}
CODE_EXT = {
    ".vue", ".jsx", ".tsx", ".js", ".ts", ".svelte", ".html", ".py", ".java", ".kt", ".go", ".php", ".cs",
    ".cshtml", ".razor", ".rb", ".erb", ".json", ".yaml", ".yml",
}
ROUTE_PATTERNS = [
    (r"""path\s*:\s*['"`]([^'"`]+)['"`]""", "router"),  # vue-router / angular / react-router objects
    (r"""<Route[^>]*\bpath=\{?['"`]([^'"`]+)['"`]""", "jsx-route"),
    (r"""@(?:app|router|bp|blueprint|api)\.(?:get|post|route|put|delete)\(\s*['"]([^'"]+)['"]""", "python"),
    (r"""\bpath\(\s*['"]([^'"]*)['"]""", "django"),
    (r"""@(?:Get|Post|Request)Mapping\(\s*(?:value\s*=\s*)?['"]([^'"]+)['"]""", "spring"),
    (r"""Route::(?:get|post|any)\(\s*['"]([^'"]+)['"]""", "laravel"),
    (r"""@page\s+["']([^"']+)["']""", "razor"),
]
UI_TEXT_PATTERNS = [
    r"""(?:label|placeholder|title|aria-label)\s*=\s*["']([^"'{}]{2,40})["']""",
    r""">\s*([一-鿿][^<>{}\n]{1,30})\s*<""",  # CJK text nodes
    r"""(?:label|title|text)\s*:\s*['"]([一-鿿A-Za-z][^'"]{1,30})['"]""",
]
TEST_ID_PATTERN = r"""data-(?:testid|test|cy)\s*=\s*["']([^"']+)["']"""


def resolve_project_path(path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = settings.projects_dir / p
    p = p.resolve()
    if not p.exists():
        raise FileNotFoundError(
            f"找不到專案路徑 {p}。請確認已把專案目錄掛載到容器內（預設 {settings.projects_dir}），"
            f"例如 docker run -v /path/to/project:{settings.projects_dir}/myapp:ro"
        )
    return p


def _detect_stack(root: Path) -> dict[str, Any]:
    stack: dict[str, Any] = {"frameworks": [], "package": None}
    pkg = root / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="ignore"))
            deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            stack["package"] = data.get("name")
            for name, label in [
                ("next", "Next.js"), ("nuxt", "Nuxt"), ("react", "React"), ("vue", "Vue"), ("@angular/core", "Angular"),
                ("svelte", "Svelte"), ("element-plus", "Element Plus"), ("element-ui", "Element UI"),
                ("antd", "Ant Design"), ("ant-design-vue", "Ant Design Vue"), ("@mui/material", "MUI"),
                ("vuetify", "Vuetify"), ("naive-ui", "Naive UI"), ("react-router-dom", "React Router"),
                ("vue-router", "Vue Router"), ("vue-i18n", "vue-i18n"), ("react-i18next", "react-i18next"),
            ]:
                if name in deps:
                    stack["frameworks"].append(label)
        except Exception:  # noqa: BLE001
            pass
    for f, label in [("manage.py", "Django"), ("requirements.txt", "Python"), ("pom.xml", "Java/Maven"),
                     ("build.gradle", "Gradle"), ("go.mod", "Go"), ("composer.json", "PHP"), ("Gemfile", "Ruby")]:
        if (root / f).exists():
            stack["frameworks"].append(label)
    return stack


def scan_project(path: str, max_files: int = 4000) -> dict[str, Any]:
    root = resolve_project_path(path)
    routes: dict[str, set[str]] = {}
    pages: list[str] = []
    ui_text: dict[str, int] = {}
    test_ids: set[str] = set()
    i18n_files: list[str] = []
    readme = ""
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and not d.startswith(".")]
        for fn in filenames:
            fp = Path(dirpath) / fn
            rel = fp.relative_to(root).as_posix()
            if fn.lower().startswith("readme") and not readme:
                readme = fp.read_text(encoding="utf-8", errors="ignore")[:3000]
            if fp.suffix.lower() not in CODE_EXT:
                continue
            count += 1
            if count > max_files:
                break
            try:
                if fp.stat().st_size > 400_000:
                    continue
                src = fp.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            low = rel.lower()
            if re.search(r"(locales?|i18n|lang)/", low) and fp.suffix in (".json", ".js", ".ts", ".yaml", ".yml"):
                i18n_files.append(rel)
            if fp.suffix in (".json", ".yaml", ".yml"):
                if "i18n" in low or "locale" in low or "lang" in low:
                    for m in re.finditer(r""":\s*["']([一-鿿][^"']{1,30})["']""", src):
                        ui_text[m.group(1)] = ui_text.get(m.group(1), 0) + 1
                continue
            if re.search(r"(pages?|views?|screens?|routes?|app)/", low) and fp.suffix in (".vue", ".tsx", ".jsx", ".svelte", ".html", ".cshtml", ".razor"):
                pages.append(rel)
            for pat, kind in ROUTE_PATTERNS:
                for m in re.finditer(pat, src):
                    r = m.group(1).strip()
                    if 0 < len(r) < 120 and not r.startswith(("http", "./", "../")) and not r.endswith((".js", ".css", ".png")):
                        routes.setdefault(r, set()).add(rel)
            for pat in UI_TEXT_PATTERNS:
                for m in re.finditer(pat, src):
                    t = m.group(1).strip()
                    ui_text[t] = ui_text.get(t, 0) + 1
            for m in re.finditer(TEST_ID_PATTERN, src):
                test_ids.add(m.group(1))
    top_text = sorted(ui_text.items(), key=lambda kv: -kv[1])[:250]
    return {
        "root": str(root),
        "stack": _detect_stack(root),
        "files_scanned": count,
        "routes": {r: sorted(f)[:3] for r, f in list(routes.items())[:200]},
        "pages": pages[:200],
        "test_ids": sorted(test_ids)[:200],
        "ui_text": [t for t, _ in top_text],
        "i18n_files": i18n_files[:30],
        "readme": readme,
    }


SUMMARY_SYSTEM = """你是資深前端架構師與技術文件作者。根據專案原始碼掃描結果，整理出「使用者看得到的功能地圖」，
供後續自動化 UI 測試與教學影片設計使用。回覆 JSON：
{
 "app_name": "...", "summary": "一段話說明這個平台做什麼",
 "features": [
   {"name": "功能名稱", "route": "/路由或 null", "description": "使用者能做什麼",
    "key_ui": ["重要按鈕/欄位文字"], "test_ids": ["可用的 data-testid"], "priority": "high|medium|low"}
 ],
 "suggested_tutorials": ["建議製作的教學主題"],
 "suggested_tests": ["建議測試的情境"]
}"""


async def analyze_project(path: str, focus: str = "") -> dict[str, Any]:
    scan = scan_project(path)
    brief = {k: scan[k] for k in ("stack", "routes", "pages", "test_ids", "ui_text", "i18n_files")}
    user = (
        f"README（節錄）：\n{scan['readme'][:2000]}\n\n掃描結果：\n{json.dumps(brief, ensure_ascii=False)[:14000]}"
        + (f"\n\n請特別聚焦：{focus}" if focus else "")
    )
    try:
        summary = await get_llm().chat_json(SUMMARY_SYSTEM, user, max_tokens=3000)
    except Exception as e:  # noqa: BLE001
        summary = {"error": f"LLM 摘要失敗：{e}"}
    return {"scan": scan, "feature_map": summary}


def read_source_files(path: str, rel_files: list[str], max_chars: int = 12000) -> str:
    """Return the content of selected files (used when the agent wants exact UI code)."""
    root = resolve_project_path(path)
    out, total = [], 0
    for rel in rel_files:
        fp = (root / rel).resolve()
        if not str(fp).startswith(str(root)) or not fp.is_file():
            continue
        src = fp.read_text(encoding="utf-8", errors="ignore")
        chunk = src[: max(0, max_chars - total)]
        out.append(f"===== {rel} =====\n{chunk}")
        total += len(chunk)
        if total >= max_chars:
            break
    return "\n\n".join(out)
