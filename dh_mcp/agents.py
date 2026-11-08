"""High-level workflows: explore, test, design / rehearse / record tutorials, goal-driven agent."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urldefrag, urljoin, urlparse

from jinja2 import Template

from . import prompts
from .browser import Session, manager, new_session
from .config import settings
from .llm import get_llm
from .manual import build_manual, html_to_pdf, op_text
from .steps import (
    StepResult,
    compact_elements,
    describe_step,
    element_to_target,
    execute_step,
    normalize_steps,
)
from .tts import available_engine, synthesize
from .video import Cue, render_tutorial_video, webm_to_mp4

log = logging.getLogger("dh.agents")
Progress = Callable[[str, float], Awaitable[None]] | None


async def _p(progress: Progress, msg: str, frac: float) -> None:
    log.info("[%3d%%] %s", int(frac * 100), msg)
    if progress:
        try:
            await progress(msg, frac)
        except Exception:  # noqa: BLE001
            pass


def slug(s: str, n: int = 40) -> str:
    s = re.sub(r"[^\w一-鿿-]+", "-", s or "").strip("-")
    return s[:n] or "job"


def job_dir(kind: str, name: str) -> Path:
    d = settings.output_dir / kind / f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}_{slug(name)}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def auth_state_path(profile: str | None) -> str | None:
    if not profile:
        return None
    return str(settings.output_dir / "auth" / f"{slug(profile)}.json")


def with_urls(d: dict[str, Any]) -> dict[str, Any]:
    """Add HTTP URLs next to file paths when PUBLIC_BASE_URL is set."""
    out = dict(d)
    for k, v in d.items():
        if isinstance(v, str) and v.startswith(str(settings.output_dir)):
            u = settings.artifact_url(Path(v))
            if u:
                out[f"{k}_url"] = u
    return out


# =============================================================== login
async def login(url: str, username: str, password: str, profile: str = "default",
                success_text: str = "", progress: Progress = None) -> dict[str, Any]:
    """Log in with heuristics (falls back to the AI agent) and save the session cookies as a profile."""
    async with manager.sem:
        s = await new_session()
        try:
            await s.page.goto(url, wait_until="domcontentloaded")
            await s.settle(800)
            page = s.page
            pwd = page.locator("input[type=password]").first
            if await pwd.count() == 0:
                raise RuntimeError("找不到密碼欄位")
            user = page.locator(
                "input[type=email], input[type=text], input[name*=user i], input[name*=account i], input:not([type])"
            ).first
            await user.fill(username)
            await pwd.fill(password)
            btn = page.locator("button[type=submit], input[type=submit]")
            if await btn.count() == 0:
                btn = page.get_by_role("button", name=re.compile("登入|登录|Login|Sign in|送出|確定", re.I))
            if await btn.count():
                await btn.first.click()
            else:
                await pwd.press("Enter")
            await s.settle(1500)
            ok = True
            if success_text:
                ok = success_text in await page.locator("body").inner_text()
            elif await page.locator("input[type=password]").count() and await page.locator("input[type=password]").first.is_visible():
                ok = False
            path = auth_state_path(profile)
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            await s.context.storage_state(path=path)
            shot = job_dir("auth", profile) / "after_login.png"
            await s.screenshot(shot)
            return with_urls({"ok": ok, "profile": profile, "storage_state": path, "url_after": page.url, "screenshot": str(shot),
                              "note": "" if ok else "登入後仍看到密碼欄位，可能登入失敗（請檢查帳密或驗證碼）"})
        finally:
            await s.close()


# =============================================================== explore
async def explore(url: str, max_pages: int = 8, auth_profile: str | None = None, describe: bool = True,
                  progress: Progress = None) -> dict[str, Any]:
    out = job_dir("explore", urlparse(url).netloc + urlparse(url).path)
    origin = urlparse(url).netloc
    seen: set[str] = set()
    queue: deque[str] = deque([url])
    pages: list[dict[str, Any]] = []
    async with manager.sem:
        s = await new_session(storage_state=auth_state_path(auth_profile))
        try:
            while queue and len(pages) < max_pages:
                u = queue.popleft()
                if u in seen:
                    continue
                seen.add(u)
                await _p(progress, f"探索 {u}", len(pages) / max_pages)
                try:
                    await s.page.goto(u, wait_until="domcontentloaded")
                    await s.settle(800)
                except Exception as e:  # noqa: BLE001
                    pages.append({"url": u, "error": str(e)[:200]})
                    continue
                snap = await s.snapshot()
                shot = out / f"page_{len(pages) + 1:02d}.png"
                png = await s.screenshot(shot)
                desc = ""
                if describe and settings.use_vision and len(pages) < 6:
                    try:
                        desc = await get_llm().describe_image(png, prompts.EXPLORE_DESCRIBE)
                    except Exception as e:  # noqa: BLE001
                        desc = f"(視覺描述失敗：{e})"
                pages.append({
                    "url": snap.get("url"), "title": snap.get("title"), "headings": snap.get("headings"),
                    "description": desc, "screenshot": str(shot),
                    "elements": compact_elements(snap, 60),
                })
                for e in snap.get("elements", []):
                    href = e.get("href")
                    if not href or href.startswith(("javascript:", "mailto:", "tel:")):
                        continue
                    nu = urldefrag(urljoin(snap["url"], href))[0]
                    if urlparse(nu).netloc == origin and nu not in seen and not re.search(r"logout|signout|登出", nu, re.I):
                        queue.append(nu)
                # SPA hash routes
                for e in snap.get("elements", []):
                    href = e.get("href") or ""
                    if href.startswith("#/"):
                        nu = urljoin(snap["url"], href)
                        if nu not in seen:
                            queue.append(nu)
        finally:
            await s.close()
    result = {"start_url": url, "pages": pages, "output_dir": str(out)}
    (out / "site_map.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def site_brief(site: dict[str, Any] | None, max_chars: int = 9000) -> str:
    if not site:
        return ""
    parts = []
    for p in site.get("pages", []):
        if p.get("error"):
            continue
        parts.append(f"## {p.get('title')} ({p.get('url')})\n標題：{' / '.join(p.get('headings') or [])}\n"
                     f"{p.get('description', '')}\n元素：\n{p.get('elements', '')[:1500]}")
    return "\n\n".join(parts)[:max_chars]


async def page_brief(url: str, auth_profile: str | None) -> tuple[str, bytes | None]:
    async with manager.sem:
        s = await new_session(storage_state=auth_state_path(auth_profile))
        try:
            await s.page.goto(url, wait_until="domcontentloaded")
            await s.settle(800)
            snap = await s.snapshot()
            png = await s.page.screenshot(type="jpeg", quality=70)
            txt = (f"起始頁：{snap.get('title')} {snap.get('url')}\n標題：{' / '.join(snap.get('headings', []))}\n"
                   f"頁面文字：{snap.get('text', '')[:800]}\n可互動元素：\n{compact_elements(snap, 100)}")
            return txt, png
        finally:
            await s.close()


# =============================================================== tests
async def plan_tests(url: str, goal: str = "", project_context: str = "", auth_profile: str | None = None,
                     site: dict[str, Any] | None = None, max_scenarios: int = 6) -> dict[str, Any]:
    brief, png = await page_brief(url, auth_profile)
    user = (f"起始網址：{url}\n測試目標：{goal or '系統主要功能的冒煙測試與表單驗證'}\n最多 {max_scenarios} 個情境。\n\n"
            f"{brief}\n\n{('其他頁面資訊：' + site_brief(site)) if site else ''}\n\n"
            f"{('專案原始碼分析：' + project_context[:8000]) if project_context else ''}")
    plan = await get_llm().chat_json(prompts.TEST_PLAN_SYSTEM, user,
                                     images=[png] if (png and settings.use_vision) else None, max_tokens=6000)
    scenarios = plan.get("scenarios", []) if isinstance(plan, dict) else plan
    for sc in scenarios:
        sc["steps"] = normalize_steps(sc.get("steps"))
    return {"url": url, "scenarios": scenarios[:max_scenarios]}


REPORT_TMPL = Template("""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><title>UI 測試報告</title>
<style>body{font-family:"Noto Sans CJK TC",sans-serif;max-width:1100px;margin:auto;padding:24px;color:#222}
.pass{color:#137333}.fail{color:#c5221f}.skip{color:#888}table{border-collapse:collapse;width:100%}
td,th{border:1px solid #ddd;padding:6px 8px;vertical-align:top;font-size:14px}img{max-width:360px;border:1px solid #ccc}
.card{border:1px solid #ddd;border-radius:8px;padding:12px 16px;margin:16px 0}video{max-width:100%}</style></head><body>
<h1>UI 自動化測試報告</h1><p>{{ url }} ・ {{ time }} ・ 通過 {{ passed }}/{{ total }} 個情境</p>
{% if ai %}<div class="card"><h2>AI 摘要</h2><p>{{ ai.summary }}</p>{% if ai.bugs %}<ul>{% for b in ai.bugs %}
<li><b>[{{ b.severity }}] {{ b.title }}</b>（{{ b.scenario }}）{{ b.evidence }} → {{ b.suggestion }}</li>{% endfor %}</ul>{% endif %}</div>{% endif %}
{% for sc in scenarios %}<div class="card"><h2 class="{{ 'pass' if sc.ok else 'fail' }}">{{ '✔' if sc.ok else '✘' }} {{ sc.name }}</h2>
<p>{{ sc.description }}</p>{% if sc.video %}<video controls src="{{ sc.video }}"></video>{% endif %}
<table><tr><th>#</th><th>步驟</th><th>結果</th><th>截圖</th></tr>{% for r in sc.results %}
<tr><td>{{ r.index + 1 }}</td><td>{{ r.desc }}</td><td class="{{ r.status }}">{{ r.status }}{% if r.healed %}（已自動修復定位）{% endif %}<br>{{ r.message }}</td>
<td>{% if r.screenshot %}<a href="{{ r.screenshot }}"><img src="{{ r.screenshot }}"></a>{% endif %}</td></tr>{% endfor %}</table>
{% if sc.errors %}<p><b>Console / 網路錯誤：</b></p><pre>{{ sc.errors|join('\n') }}</pre>{% endif %}</div>{% endfor %}
</body></html>""")


async def run_tests(url: str, scenarios: list[dict[str, Any]], auth_profile: str | None = None,
                    record_video: bool = True, ai_summary: bool = True, progress: Progress = None,
                    name: str = "ui-test") -> dict[str, Any]:
    out = job_dir("tests", name)
    results = []
    for si, sc in enumerate(scenarios):
        await _p(progress, f"執行情境 {si + 1}/{len(scenarios)}：{sc.get('name')}", si / max(1, len(scenarios)))
        steps = normalize_steps(sc.get("steps"))
        if not steps or steps[0]["action"] != "goto":
            steps = [{"action": "goto", "value": url}] + steps
        sdir = out / f"s{si + 1:02d}"
        async with manager.sem:
            s = await new_session(video_dir=sdir / "video" if record_video else None, animate=record_video,
                                  storage_state=auth_state_path(auth_profile))
            rows, ok, failed = [], True, False
            try:
                for i, st in enumerate(steps):
                    if failed:
                        rows.append({"index": i, "desc": describe_step(st), "status": "skip", "message": "", "healed": False})
                        continue
                    r = await execute_step(s, st, i)
                    shot = sdir / f"step_{i + 1:02d}.png"
                    try:
                        await s.screenshot(shot)
                    except Exception:  # noqa: BLE001
                        shot = None
                    status = "pass" if r.ok else ("skip" if st.get("optional") else "fail")
                    if status == "fail":
                        ok, failed = False, True
                    rows.append({"index": i, "desc": describe_step(st), "status": status,
                                 "message": r.message or r.extra.get("reason", ""), "healed": r.healed,
                                 "resolved_target": r.resolved_target,
                                 "screenshot": shot.relative_to(out).as_posix() if shot else None})
            finally:
                errors = (s.page_errors + s.console_errors)[:20] + [f"HTTP {x}" for x in s.failed_requests[:20]]
                video = await s.close()
        vrel = None
        if video and video.exists():
            try:
                mp4 = webm_to_mp4(video, sdir / "run.mp4")
                vrel = mp4.relative_to(out).as_posix()
            except Exception:  # noqa: BLE001
                vrel = video.relative_to(out).as_posix()
        results.append({"name": sc.get("name", f"情境{si + 1}"), "description": sc.get("description", ""), "ok": ok,
                        "results": rows, "errors": errors, "video": vrel, "steps": steps})
    ai = None
    if ai_summary:
        await _p(progress, "AI 撰寫測試摘要", 0.95)
        brief = [{"name": r["name"], "ok": r["ok"],
                  "failed": [x for x in r["results"] if x["status"] == "fail"], "errors": r["errors"][:8]} for r in results]
        try:
            ai = await get_llm().chat_json(prompts.REPORT_SYSTEM, json.dumps(brief, ensure_ascii=False)[:12000], max_tokens=2000)
        except Exception as e:  # noqa: BLE001
            ai = {"summary": f"(AI 摘要失敗：{e})", "bugs": []}
    passed = sum(1 for r in results if r["ok"])
    html = REPORT_TMPL.render(url=url, time=dt.datetime.now().strftime("%Y-%m-%d %H:%M"), passed=passed,
                              total=len(results), scenarios=results, ai=ai)
    (out / "report.html").write_text(html, encoding="utf-8")
    report = {"url": url, "passed": passed, "total": len(results), "ai_summary": ai,
              "scenarios": [{k: v for k, v in r.items() if k != "steps"} for r in results]}
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "scenarios.json").write_text(json.dumps({"url": url, "scenarios": [{"name": r["name"], "description": r["description"], "steps": r["steps"]} for r in results]}, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        (out / "test_generated.py").write_text(export_pytest(url, scenarios), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return with_urls({
        "passed": passed, "total": len(results), "ai_summary": ai,
        "failures": [{"scenario": r["name"], "step": x["desc"], "message": x["message"]}
                     for r in results for x in r["results"] if x["status"] == "fail"],
        "report_html": str(out / "report.html"), "report_json": str(out / "report.json"),
        "scenarios_json": str(out / "scenarios.json"), "pytest_file": str(out / "test_generated.py"),
        "output_dir": str(out),
    })


def export_pytest(url: str, scenarios: list[dict[str, Any]]) -> str:
    """Translate DSL scenarios into a standalone pytest-playwright file (for CI)."""
    def loc(t: dict[str, Any]) -> str:
        if t.get("testid"):
            return f"page.get_by_test_id({t['testid']!r})"
        if t.get("role") and t.get("name"):
            return f"page.get_by_role({t['role']!r}, name={t['name']!r})"
        if t.get("label"):
            return f"page.get_by_label({t['label']!r})"
        if t.get("placeholder"):
            return f"page.get_by_placeholder({t['placeholder']!r})"
        if t.get("selector"):
            return f"page.locator({t['selector']!r})"
        return f"page.get_by_text({(t.get('text') or t.get('name') or '')!r})"

    lines = ["# Auto-generated by 智慧數字人 MCP. Run: pytest test_generated.py --base-url ...",
             "import re", "from playwright.sync_api import Page, expect", "", f"BASE_URL = {url!r}", ""]
    for i, sc in enumerate(scenarios, 1):
        fn = f"test_{i:02d}_" + re.sub(r"\W+", "_", slug(sc.get("name", ""), 30)).strip("_").encode("ascii", "ignore").decode() or f"test_{i:02d}"
        lines += [f"def {fn}(page: Page):", f"    \"\"\"{sc.get('name', '')}\"\"\"", "    page.goto(BASE_URL)"]
        for st in normalize_steps(sc.get("steps")):
            a, t, v = st["action"], st.get("target") or {}, st.get("value")
            L = loc(t) + ".first"
            code = {
                "goto": f"page.goto({v!r})", "click": f"{L}.click()", "dblclick": f"{L}.dblclick()",
                "hover": f"{L}.hover()", "fill": f"{L}.fill({str(v)!r})", "type": f"{L}.press_sequentially({str(v)!r})",
                "check": f"{L}.check()", "uncheck": f"{L}.uncheck()", "select": f"{L}.select_option(label={str(v)!r})",
                "press": f"page.keyboard.press({str(v or 'Enter')!r})", "wait": f"page.wait_for_timeout({int(float(v or 1) * 1000)})",
                "assert_visible": f"expect({L}).to_be_visible()", "assert_not_visible": f"expect({loc(t)}).to_have_count(0)",
                "assert_text": f"expect(page.get_by_text({str(v)!r}).first).to_be_visible()",
                "assert_url": f"expect(page).to_have_url(re.compile({re.escape(str(v))!r}))",
                "assert_value": f"expect({L}).to_have_value({str(v)!r})",
            }.get(a)
            if code:
                lines.append(f"    {code}")
            elif a == "verify":
                lines.append(f"    # AI verify: {st.get('expect') or v}")
        lines.append("")
    return "\n".join(lines)


# =============================================================== goal-driven agent
async def agent_task(url: str, goal: str, auth_profile: str | None = None, max_steps: int = 20,
                     progress: Progress = None, record: bool = False) -> dict[str, Any]:
    """Observe → decide → act loop. Returns validated, replayable steps (semantic targets)."""
    out = job_dir("agent", goal)
    history: list[str] = []
    replay: list[dict[str, Any]] = []
    result, done = "", False
    async with manager.sem:
        s = await new_session(storage_state=auth_state_path(auth_profile), animate=record,
                              video_dir=out / "video" if record else None)
        try:
            await s.page.goto(url, wait_until="domcontentloaded")
            await s.settle(800)
            for i in range(max_steps):
                await _p(progress, f"代理人第 {i + 1} 步", i / max_steps)
                snap = await s.snapshot()
                user = (f"目標：{goal}\n已完成：\n" + ("\n".join(history[-12:]) or "（無）") +
                        f"\n\n目前頁面：{snap.get('title')} {snap.get('url')}\n標題：{' / '.join(snap.get('headings', []))}\n"
                        f"對話框：{snap.get('dialogs')}\n提示：{snap.get('alerts')}\n可互動元素：\n{compact_elements(snap)}")
                images = [await s.page.screenshot(type="jpeg", quality=70)] if settings.use_vision else None
                dec = await get_llm().chat_json(prompts.AGENT_SYSTEM, user, images=images, max_tokens=600)
                if not isinstance(dec, dict):
                    continue
                if dec.get("done"):
                    done, result = True, str(dec.get("result", ""))
                    break
                st = normalize_steps([dec.get("step")])
                if not st:
                    history.append(f"{i + 1}. (無效輸出)")
                    continue
                st = st[0]
                el = None
                did = (st.get("target") or {}).get("dh_id")
                if did is not None:
                    el = next((e for e in snap["elements"] if str(e["id"]) == str(did)), None)
                r = await execute_step(s, st, i, heal=False)
                history.append(f"{i + 1}. {st['action']} {el.get('text') or el.get('aria') or el.get('placeholder') if el else ''} "
                               f"{st.get('value') or ''} → {'成功' if r.ok else '失敗：' + r.message}")
                if r.ok:
                    sem_step = {k: v for k, v in st.items() if k != "target"}
                    if el:
                        sem_step["target"] = element_to_target(el)
                    elif st.get("target"):
                        sem_step["target"] = st["target"]
                    replay.append(sem_step)
            await s.screenshot(out / "final.png")
        finally:
            video = await s.close()
    res = {"goal": goal, "done": done, "result": result, "steps": replay, "history": history,
           "final_screenshot": str(out / "final.png"), "output_dir": str(out)}
    if video:
        res["video"] = str(webm_to_mp4(video, out / "agent_run.mp4"))
    (out / "agent_steps.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    return with_urls(res)


# =============================================================== tutorials
async def design_tutorial(url: str, topic: str, audience: str = "一般使用者", project_context: str = "",
                          auth_profile: str | None = None, site: dict[str, Any] | None = None) -> dict[str, Any]:
    brief, png = await page_brief(url, auth_profile)
    user = (f"教學主題：{topic}\n適用對象：{audience}\n起始網址：{url}\n\n{brief}\n\n"
            f"{('其他頁面資訊：' + site_brief(site)) if site else ''}\n\n"
            f"{('專案原始碼分析（請依實際存在的路由、按鈕文字、欄位設計流程）：' + project_context[:9000]) if project_context else ''}")
    script = await get_llm().chat_json(prompts.TUTORIAL_SYSTEM, user,
                                       images=[png] if (png and settings.use_vision) else None, max_tokens=6000)
    return normalize_script(script, url, topic, audience)


def normalize_script(script: Any, url: str, topic: str = "", audience: str = "") -> dict[str, Any]:
    if not isinstance(script, dict):
        script = {"sections": [{"title": topic, "steps": script if isinstance(script, list) else []}]}
    script.setdefault("title", topic or "操作教學")
    script.setdefault("audience", audience or "一般使用者")
    script["url"] = script.get("url") or url
    secs = script.get("sections") or [{"title": script["title"], "steps": script.get("steps", [])}]
    for sec in secs:
        sec["steps"] = normalize_steps(sec.get("steps"))
        sec.setdefault("title", "操作步驟")
    script["sections"] = [s for s in secs if s["steps"]]
    script.pop("steps", None)
    return script


def _flatten(script: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    return [(si, st) for si, sec in enumerate(script["sections"]) for st in sec["steps"]]


def _unflatten(script: dict[str, Any], flat: list[tuple[int, dict[str, Any]]]) -> None:
    for sec in script["sections"]:
        sec["steps"] = []
    for si, st in flat:
        script["sections"][min(si, len(script["sections"]) - 1)]["steps"].append(st)


async def rehearse(script: dict[str, Any], auth_profile: str | None = None, max_repairs: int = 4,
                   progress: Progress = None) -> dict[str, Any]:
    """Dry-run the script; heal selectors and let the LLM repair failing parts. Returns a fixed script."""
    flat = _flatten(script)
    repairs, notes = 0, []
    async with manager.sem:
        s = await new_session(storage_state=auth_state_path(auth_profile))
        try:
            await s.page.goto(script["url"], wait_until="domcontentloaded")
            await s.settle(800)
            i = 0
            while i < len(flat):
                si, st = flat[i]
                await _p(progress, f"排練步驟 {i + 1}/{len(flat)}：{st.get('title') or st['action']}", 0.1 + 0.3 * i / max(1, len(flat)))
                if st["action"] == "narrate":
                    i += 1
                    continue
                r = await execute_step(s, st, i)
                if r.ok:
                    if r.healed and r.resolved_target:
                        st["target"] = r.resolved_target
                        notes.append(f"步驟{i + 1} 目標已自動修正為 {r.resolved_target}")
                    i += 1
                    continue
                if st.get("optional") or st["action"].startswith("assert") or st["action"] == "verify":
                    notes.append(f"步驟{i + 1} 失敗已略過：{r.message}")
                    st["_rehearsal_error"] = r.message
                    i += 1
                    continue
                if repairs >= max_repairs:
                    notes.append(f"步驟{i + 1} 失敗且已達修復上限：{r.message}")
                    st["_rehearsal_error"] = r.message
                    i += 1
                    continue
                repairs += 1
                snap = await s.snapshot()
                remaining = [x for _, x in flat[i:]]
                user = (f"失敗步驟：{describe_step(st)}\n錯誤：{r.message}\n剩餘步驟（含失敗步驟）：\n"
                        f"{json.dumps(remaining, ensure_ascii=False)[:6000]}\n\n目前頁面：{snap.get('title')} {snap.get('url')}\n"
                        f"對話框：{snap.get('dialogs')}\n可互動元素：\n{compact_elements(snap)}")
                images = [await s.page.screenshot(type="jpeg", quality=70)] if settings.use_vision else None
                try:
                    fix = await get_llm().chat_json(prompts.REPAIR_SYSTEM, user, images=images, max_tokens=4000)
                    new_steps = normalize_steps(fix.get("steps") if isinstance(fix, dict) else fix)
                except Exception as e:  # noqa: BLE001
                    new_steps = []
                    notes.append(f"修復失敗：{e}")
                if not new_steps:
                    st["_rehearsal_error"] = r.message
                    i += 1
                    continue
                notes.append(f"步驟{i + 1} 失敗（{r.message}），AI 已改寫剩餘 {len(new_steps)} 步：{(fix or {}).get('note', '')}")
                flat = flat[:i] + [(si, x) for x in new_steps]
        finally:
            await s.close()
    _unflatten(script, flat)
    failed = sum(1 for _, st in flat if st.get("_rehearsal_error"))
    script["_rehearsal"] = {"repairs": repairs, "failed_steps": failed, "total_steps": len(flat), "notes": notes}
    return script


async def tutorial_from_agent(url: str, topic: str, audience: str, auth_profile: str | None,
                              progress: Progress = None) -> dict[str, Any]:
    run = await agent_task(url, f"示範如何：{topic}", auth_profile=auth_profile, progress=progress)
    if not run["steps"]:
        raise RuntimeError(f"代理人無法完成「{topic}」：{run.get('result')}")
    user = f"教學主題：{topic}\n適用對象：{audience}\n已驗證步驟：\n{json.dumps(run['steps'], ensure_ascii=False)[:9000]}"
    script = await get_llm().chat_json(prompts.NARRATE_SYSTEM, user, max_tokens=6000)
    script = normalize_script(script, url, topic, audience)
    if not script["sections"]:
        script["sections"] = [{"title": topic, "steps": run["steps"]}]
    return script


def _narration_seconds(text: str) -> float:
    return max(settings.min_step_seconds, len(text or "") / max(1.0, settings.caption_chars_per_sec) + 0.6)


async def record_tutorial(script: dict[str, Any], auth_profile: str | None = None, progress: Progress = None,
                          make_pdf: bool = True) -> dict[str, Any]:
    """Run the script with visible cursor, zoom, voice and subtitles; produce mp4 + manual."""
    script = normalize_script(script, script.get("url", ""), script.get("title", ""), script.get("audience", ""))
    out = job_dir("tutorials", script["title"])
    shots_dir = out / "screenshots"
    shots_dir.mkdir(exist_ok=True)
    (out / "script.json").write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 1. pre-synthesize narration so the video never waits on TTS
    engine = available_engine()
    seq: list[tuple[str, int, dict[str, Any]]] = []  # (kind, section_index, step)
    if script.get("intro"):
        seq.append(("intro", -1, {"action": "narrate", "narration": script["intro"], "title": "開場"}))
    for si, sec in enumerate(script["sections"]):
        for st in sec["steps"]:
            seq.append(("step", si, st))
    if script.get("outro"):
        seq.append(("outro", -1, {"action": "narrate", "narration": script["outro"], "title": "結尾"}))
    audio: dict[int, tuple[Path, float]] = {}
    for i, (_, _, st) in enumerate(seq):
        if st.get("narration"):
            await _p(progress, f"合成語音 {i + 1}/{len(seq)}（{engine}）", 0.4 * i / len(seq))
            wav = out / "audio" / f"n{i:03d}.wav"
            d = await synthesize(st["narration"], wav, engine)
            if d:
                audio[i] = (wav, d)

    # ---- 2. record
    cues: list[Cue] = []
    clips: list[tuple[float, Path]] = []
    manual_steps: dict[int, list[dict[str, Any]]] = {si: [] for si in range(len(script["sections"]))}
    errors: list[str] = []
    async with manager.sem:
        s = await new_session(video_dir=out / "raw", animate=True, storage_state=auth_state_path(auth_profile))
        try:
            await s.page.goto(script["url"], wait_until="domcontentloaded")
            await s.settle(1000)
            await s.ensure_cursor()
            await s.page.wait_for_timeout(300)
            trim_start = max(0.0, s.now() - 0.2)
            num = 0
            for i, (kind, si, st) in enumerate(seq):
                await _p(progress, f"錄製 {i + 1}/{len(seq)}：{st.get('title') or st['action']}", 0.4 + 0.45 * i / len(seq))
                t0 = s.now()
                narr = st.get("narration", "")
                dur = audio[i][1] if i in audio else (_narration_seconds(narr) if narr else 0.8)
                if i in audio:
                    clips.append((t0, audio[i][0]))
                shot_path: Path | None = None
                is_step = kind == "step" and st["action"] != "wait"
                if is_step:
                    num += 1
                    shot_path = shots_dir / f"step_{num:02d}.png"
                pre_shot = {"done": False}

                async def _point(box: Any, _p=shot_path, _st=st, _d=pre_shot, _t0=t0, _dur=dur) -> None:
                    # click-like actions: capture the screen *before* clicking, with the target framed
                    if _st["action"] in ("click", "dblclick", "check", "uncheck", "select", "hover"):
                        if _p is not None:
                            await s.screenshot(_p)
                            _d["done"] = True
                        # let the narrator explain first, then click (~60% into the sentence)
                        wait = (_t0 + min(_dur * 0.6, max(0.0, _dur - 0.6))) - s.now()
                        if wait > 0:
                            await s.page.wait_for_timeout(int(min(wait, 6) * 1000))

                if st["action"] == "narrate":
                    r = StepResult(index=i, action="narrate", ok=True)
                else:
                    r = await execute_step(s, st, i, on_point=_point)
                    if not r.ok:
                        errors.append(f"{st.get('title') or describe_step(st)}：{r.message}")
                if is_step and shot_path is not None:
                    box = r.extra.get("box") if r.ok else None
                    if not pre_shot["done"]:
                        if box and st["action"] in ("fill", "type", "highlight"):
                            await s.highlight(box)
                            await s.screenshot(shot_path)
                            await s.highlight(None)
                        else:
                            await s.screenshot(shot_path)
                    manual_steps[si].append({
                        "number": num, "title": st.get("title") or op_text(st) or st["action"],
                        "text": st.get("description") or narr, "op": op_text(st),
                        "image": shot_path.relative_to(out).as_posix(),
                        "error": None if r.ok else r.message,
                    })
                # hold until the narration finishes, then a short breath
                remain = (t0 + dur + 0.35) - s.now()
                if remain > 0:
                    await s.page.wait_for_timeout(int(remain * 1000))
                s.zoom_end(linger=0.2)
                if narr:
                    cues.append(Cue(t0, max(s.now() - 0.1, t0 + 0.8), narr))
            await s.page.wait_for_timeout(800)
        finally:
            raw = await s.close()

    # ---- 3. post-production
    await _p(progress, "後製影片（運鏡放大、字幕、口白）", 0.88)
    vids: dict[str, str] = {}
    if raw and raw.exists():
        vids = await asyncio.to_thread(
            render_tutorial_video, raw_video=raw, out_dir=out, cues=cues, zooms=s.zooms, audio_clips=clips,
            trim_start=trim_start, title=script["title"], name="tutorial",
        )
    # ---- 4. manual
    await _p(progress, "產生教學手冊", 0.95)
    doc = {
        "title": script["title"], "audience": script.get("audience"), "url": script["url"],
        "intro": script.get("intro", ""), "prerequisites": script.get("prerequisites", []), "tips": script.get("tips", []),
        "video": "tutorial.mp4" if vids.get("mp4") else None,
        "sections": [{"title": sec["title"], "description": sec.get("description", ""), "steps": manual_steps[si]}
                     for si, sec in enumerate(script["sections"])],
    }
    files = build_manual(doc, out, "manual")
    if make_pdf:
        try:
            files["pdf"] = str(await html_to_pdf(Path(files["html"]), out / "manual.pdf"))
        except Exception as e:  # noqa: BLE001
            errors.append(f"PDF 產生失敗：{e}")
    res = {
        "title": script["title"], "output_dir": str(out), "video": vids.get("mp4"), "subtitles_srt": vids.get("srt"),
        "manual_html": files.get("html"), "manual_pdf": files.get("pdf"), "manual_docx": files.get("docx"),
        "manual_markdown": files.get("markdown"), "script_json": str(out / "script.json"),
        "tts_engine": engine, "steps": sum(len(v) for v in manual_steps.values()), "zoom_shots": len(s.zooms),
        "errors": errors, "rehearsal": script.get("_rehearsal"),
    }
    (out / "result.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    return with_urls(res)
