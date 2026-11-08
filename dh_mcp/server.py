"""智慧數字人 MCP server: tools + HTTP (streamable /mcp, SSE /sse) + stdio transports."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import agents, jobs
from .browser import manager
from .config import settings
from .llm import get_llm
from .project import analyze_project, read_source_files
from .tts import available_engine

log = logging.getLogger("dh.server")

INSTRUCTIONS = """智慧數字人（Digital Human）：自動化 Playwright UI 測試、製作含截圖的教學手冊與「有字幕、口白、滑鼠動畫、局部放大」的教學影片。
建議流程：
1. 若系統需要登入，先呼叫 dh_login 建立登入設定檔（auth_profile），之後各工具帶入相同 auth_profile。
2. 若有專案原始碼（已掛載到伺服器），先用 dh_analyze_project 了解功能，再把 project_path 傳給其他工具，AI 會依實際路由/按鈕文字設計腳本。
3. 測試：dh_run_ui_tests(url, goal=...) 一鍵規劃＋執行＋產出報告；或先 dh_plan_ui_tests 檢視/修改情境再執行。
4. 教學：dh_create_tutorial(url, topic=...) 一鍵完成（設計腳本→排練自動修正→錄影→後製→手冊）。
   長任務預設在背景執行，回傳 job_id，請用 dh_job_status(job_id) 輪詢直到 status=done，再把結果中的檔案路徑/網址告訴使用者。
   想先審稿：dh_design_tutorial 產生腳本（可修改）→ dh_record_tutorial(script=...) 錄製。
5. 不確定畫面怎麼操作時，可用 dh_agent_task 讓數字人自己摸索完成目標，回傳可重播的步驟。"""

mcp = FastMCP(
    "digital-human",
    instructions=INSTRUCTIONS,
    host=settings.host,
    port=settings.port,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

_project_cache: dict[str, dict[str, Any]] = {}


def _progress_from(ctx: Context | None):
    if ctx is None:
        return None

    async def p(msg: str, frac: float) -> None:
        try:
            await ctx.report_progress(progress=frac, total=1.0, message=msg)
        except TypeError:
            await ctx.report_progress(frac, 1.0)
        except Exception:  # noqa: BLE001
            pass

    return p


async def _project_context(project_path: str | None, focus: str = "") -> str:
    if not project_path:
        return ""
    key = f"{project_path}|{focus}"
    if key not in _project_cache:
        _project_cache[key] = await analyze_project(project_path, focus)
    a = _project_cache[key]
    scan = a["scan"]
    return json.dumps({"feature_map": a["feature_map"], "routes": list(scan["routes"].keys())[:120],
                       "test_ids": scan["test_ids"][:120], "ui_text": scan["ui_text"][:150]}, ensure_ascii=False)


async def _run(kind: str, background: bool, ctx: Context | None, fn) -> dict[str, Any]:
    if background:
        job = jobs.start(kind, fn)
        return {"job_id": job.id, "status": "running",
                "hint": "背景執行中。請呼叫 dh_job_status(job_id) 取得進度與結果（可帶 wait_seconds 等待）。"}
    return await fn(_progress_from(ctx))


# ------------------------------------------------------------------ tools
@mcp.tool()
async def dh_check_environment() -> dict[str, Any]:
    """檢查服務狀態：LLM 連線（含模型名稱）、瀏覽器、ffmpeg、語音引擎、輸出目錄。部署後第一個呼叫它。"""
    llm = await get_llm().health()
    browser_ok, browser_err = True, ""
    try:
        b = await manager.browser()
        browser_ver = b.version
    except Exception as e:  # noqa: BLE001
        browser_ok, browser_err, browser_ver = False, str(e)[:300], ""
    return {
        "llm": llm, "browser": {"ok": browser_ok, "version": browser_ver, "error": browser_err},
        "ffmpeg": bool(shutil.which("ffmpeg")), "tts_engine": available_engine(),
        "output_dir": str(settings.output_dir), "projects_dir": str(settings.projects_dir),
        "projects_available": sorted(p.name for p in settings.projects_dir.iterdir() if p.is_dir())[:50]
        if settings.projects_dir.exists() else [],
        "public_base_url": settings.public_base_url or "(未設定 PUBLIC_BASE_URL，只回傳檔案路徑)",
    }


@mcp.tool()
async def dh_login(url: str, username: str, password: str, profile: str = "default", success_text: str = "") -> dict[str, Any]:
    """登入系統並儲存登入狀態為設定檔（auth_profile），之後測試/教學工具帶入同名 auth_profile 即免登入。
    url: 登入頁網址；success_text: 登入成功後頁面應出現的文字（選填，用來確認）。"""
    return await agents.login(url, username, password, profile, success_text)


@mcp.tool()
async def dh_explore_site(url: str, max_pages: int = 8, auth_profile: str | None = None, describe: bool = True,
                          ctx: Context | None = None) -> dict[str, Any]:
    """探索網站：爬同網域頁面，擷取截圖、可互動元素，並用視覺模型描述每頁用途。回傳網站地圖。"""
    res = await agents.explore(url, max_pages, auth_profile, describe, _progress_from(ctx))
    return {"start_url": url, "output_dir": res["output_dir"],
            "pages": [{k: p.get(k) for k in ("url", "title", "headings", "description", "screenshot", "error")} for p in res["pages"]]}


@mcp.tool()
async def dh_analyze_project(project_path: str, focus: str = "") -> dict[str, Any]:
    """分析專案原始碼（路由、頁面、表單、按鈕文字、data-testid、i18n），由 LLM 整理成功能地圖。
    project_path: 容器內路徑，或相對於 PROJECTS_DIR（預設 /workspace）的資料夾名稱。"""
    a = await analyze_project(project_path, focus)
    _project_cache[f"{project_path}|{focus}"] = a
    scan = a["scan"]
    return {"feature_map": a["feature_map"], "stack": scan["stack"], "files_scanned": scan["files_scanned"],
            "routes": list(scan["routes"].keys())[:80], "pages": scan["pages"][:60], "test_ids": scan["test_ids"][:60]}


@mcp.tool()
async def dh_read_project_files(project_path: str, files: list[str], max_chars: int = 12000) -> str:
    """讀取專案中指定檔案的原始碼（相對路徑），用於精確了解某頁面的欄位與按鈕。"""
    return read_source_files(project_path, files, max_chars)


@mcp.tool()
async def dh_plan_ui_tests(url: str, goal: str = "", project_path: str | None = None, auth_profile: str | None = None,
                           max_scenarios: int = 6, explore_pages: int = 0) -> dict[str, Any]:
    """由 AI 規劃 UI 測試情境（不執行）。回傳 scenarios，可修改後交給 dh_run_ui_tests。
    explore_pages>0 時會先探索多個頁面再規劃。"""
    site = await agents.explore(url, explore_pages, auth_profile, describe=True) if explore_pages > 0 else None
    return await agents.plan_tests(url, goal, await _project_context(project_path), auth_profile, site, max_scenarios)


@mcp.tool()
async def dh_run_ui_tests(url: str, scenarios: list[dict[str, Any]] | None = None, scenarios_json_path: str | None = None,
                          goal: str = "", project_path: str | None = None, auth_profile: str | None = None,
                          max_scenarios: int = 6, record_video: bool = True, background: bool = False,
                          ctx: Context | None = None) -> dict[str, Any]:
    """執行 Playwright UI 測試並產出報告（HTML/JSON、每步截圖、錄影、AI 問題摘要、可放進 CI 的 pytest 檔）。
    未提供 scenarios 時會依 goal / 專案原始碼自動規劃。每個步驟格式見 dh_plan_ui_tests 的輸出。"""
    async def fn(progress):
        sc = scenarios
        if not sc and scenarios_json_path:
            sc = json.loads(Path(scenarios_json_path).read_text(encoding="utf-8"))["scenarios"]
        if not sc:
            if progress:
                await progress("AI 規劃測試情境", 0.02)
            plan = await agents.plan_tests(url, goal, await _project_context(project_path), auth_profile, None, max_scenarios)
            sc = plan["scenarios"]
        return await agents.run_tests(url, sc, auth_profile, record_video, True, progress, name=goal or "ui-test")
    return await _run("ui_tests", background, ctx, fn)


@mcp.tool()
async def dh_agent_task(url: str, goal: str, auth_profile: str | None = None, max_steps: int = 20,
                        record_video: bool = False, ctx: Context | None = None) -> dict[str, Any]:
    """讓數字人「看畫面自己操作」完成目標（觀察→決策→執行迴圈），回傳實際可重播的步驟、過程與最終截圖。
    適合探索未知流程，或驗證某個任務是否能完成。"""
    return await agents.agent_task(url, goal, auth_profile, max_steps, _progress_from(ctx), record_video)


@mcp.tool()
async def dh_design_tutorial(url: str, topic: str, audience: str = "一般使用者", project_path: str | None = None,
                             auth_profile: str | None = None, rehearse: bool = True,
                             ctx: Context | None = None) -> dict[str, Any]:
    """AI 設計教學腳本（章節、步驟、口白、放大重點）。rehearse=True 會先實際排練並自動修正失敗的步驟。
    回傳 script 與存檔路徑；可修改後交給 dh_record_tutorial 錄製。"""
    progress = _progress_from(ctx)
    script = await agents.design_tutorial(url, topic, audience, await _project_context(project_path, topic), auth_profile)
    if rehearse:
        script = await agents.rehearse(script, auth_profile, progress=progress)
    d = agents.job_dir("scripts", topic)
    p = d / "script.json"
    p.write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"script_json_path": str(p), "script": script}


@mcp.tool()
async def dh_record_tutorial(script: dict[str, Any] | None = None, script_json_path: str | None = None,
                             auth_profile: str | None = None, make_pdf: bool = True, background: bool = True,
                             ctx: Context | None = None) -> dict[str, Any]:
    """依腳本錄製教學：可見滑鼠移動與點擊波紋、AI 逐字打字、局部放大運鏡、語音口白、燒錄字幕，
    輸出 tutorial.mp4 + .srt、manual.html/.pdf/.docx/.md（含標註截圖）。預設背景執行，回傳 job_id。"""
    if script is None:
        if not script_json_path:
            return {"error": "請提供 script 或 script_json_path"}
        script = json.loads(Path(script_json_path).read_text(encoding="utf-8"))

    async def fn(progress):
        return await agents.record_tutorial(script, auth_profile, progress, make_pdf)
    return await _run("record_tutorial", background, ctx, fn)


@mcp.tool()
async def dh_create_tutorial(url: str, topic: str, audience: str = "一般使用者", project_path: str | None = None,
                             auth_profile: str | None = None, mode: str = "auto", rehearse: bool = True,
                             background: bool = True, ctx: Context | None = None) -> dict[str, Any]:
    """一鍵製作教學影片與手冊：設計腳本 → 排練並自動修正 → 錄影（字幕、口白、滑鼠、放大）→ 後製 → 手冊。
    mode: auto（依原始碼/畫面設計腳本，排練失敗過多時改用代理人摸索）| design（只用設計）| agent（先讓代理人實際摸索流程再寫口白）。
    預設背景執行，回傳 job_id，請用 dh_job_status 輪詢。"""
    async def fn(progress):
        script = None
        if mode in ("auto", "design"):
            if progress:
                await progress("AI 設計教學腳本", 0.02)
            script = await agents.design_tutorial(url, topic, audience, await _project_context(project_path, topic), auth_profile)
            if rehearse:
                script = await agents.rehearse(script, auth_profile, progress=progress)
                rh = script.get("_rehearsal", {})
                if mode == "auto" and rh.get("total_steps") and rh.get("failed_steps", 0) / rh["total_steps"] > 0.3:
                    log.info("rehearsal mostly failed; switching to agent mode")
                    script = None
        if script is None:
            if progress:
                await progress("代理人實地摸索操作流程", 0.1)
            script = await agents.tutorial_from_agent(url, topic, audience, auth_profile, progress)
        return await agents.record_tutorial(script, auth_profile, progress)
    return await _run("create_tutorial", background, ctx, fn)


@mcp.tool()
async def dh_job_status(job_id: str, wait_seconds: int = 50) -> dict[str, Any]:
    """查詢背景任務進度/結果。wait_seconds：最多等待幾秒（任務完成會立即返回），建議 30~55。"""
    job = jobs.JOBS.get(job_id)
    if not job:
        return {"error": f"找不到 job {job_id}"}
    if job.task and job.status == "running" and wait_seconds > 0:
        try:
            await asyncio.wait_for(asyncio.shield(job.task), timeout=min(wait_seconds, 240))
        except asyncio.TimeoutError:
            pass
    return job.view()


@mcp.tool()
async def dh_list_jobs() -> list[dict[str, Any]]:
    """列出本次服務啟動後的所有背景任務。"""
    return [j.view(full=False) for j in jobs.JOBS.values()]


@mcp.tool()
async def dh_list_outputs(kind: str = "", limit: int = 20) -> list[dict[str, Any]]:
    """列出已產生的成果資料夾。kind: tests | tutorials | explore | agent | scripts（空白=全部）。"""
    base = settings.output_dir
    dirs = []
    for k in ([kind] if kind else ["tests", "tutorials", "explore", "agent", "scripts"]):
        d = base / k
        if d.exists():
            dirs += [p for p in d.iterdir() if p.is_dir()]
    dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for p in dirs[:limit]:
        item = {"kind": p.parent.name, "name": p.name, "path": str(p),
                "files": sorted(f.name for f in p.iterdir() if f.is_file())[:30]}
        u = settings.artifact_url(p)
        if u:
            item["url"] = u + "/"
        out.append(item)
    return out


# ------------------------------------------------------------------ HTTP app
def build_http_app():
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse
    from starlette.routing import Mount, Route
    from starlette.staticfiles import StaticFiles

    settings.output_dir.mkdir(parents=True, exist_ok=True)
    http_app = mcp.streamable_http_app()
    sse_app = mcp.sse_app()

    async def health(_: Request):
        return JSONResponse({"ok": True, "service": "digital-human-mcp"})

    async def index(_: Request):
        rows = []
        for k in ("tutorials", "tests", "explore", "agent"):
            d = settings.output_dir / k
            if not d.exists():
                continue
            for p in sorted(d.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:30]:
                links = " ".join(
                    f'<a href="/files/{k}/{p.name}/{f}">{f}</a>'
                    for f in ("tutorial.mp4", "manual.html", "manual.pdf", "manual.docx", "report.html", "agent_run.mp4", "site_map.json")
                    if (p / f).exists()
                )
                rows.append(f"<tr><td>{k}</td><td>{p.name}</td><td>{links}</td></tr>")
        html = ("<!doctype html><meta charset=utf-8><title>智慧數字人</title><style>body{font-family:sans-serif;padding:24px}"
                "td{border-bottom:1px solid #ddd;padding:6px 10px}a{margin-right:10px}</style><h1>智慧數字人 MCP</h1>"
                "<p>MCP 端點：<code>/mcp</code>（Streamable HTTP）、<code>/sse</code>（SSE）</p>"
                f"<table><tr><th>類型</th><th>名稱</th><th>檔案</th></tr>{''.join(rows)}</table>")
        return HTMLResponse(html)

    app = Starlette(
        routes=[
            Route("/", index), Route("/healthz", health),
            *http_app.routes, *sse_app.routes,
            Mount("/files", StaticFiles(directory=str(settings.output_dir), html=True), name="files"),
        ],
        lifespan=http_app.router.lifespan_context,
    )
    return app


def main() -> None:
    ap = argparse.ArgumentParser(description="智慧數字人 MCP server")
    ap.add_argument("--transport", choices=["http", "stdio", "sse", "streamable-http"], default="http",
                    help="http = Streamable HTTP(/mcp) + SSE(/sse) + 檔案瀏覽(/files)；stdio 給本機 CLI 直接啟動")
    ap.add_argument("--host", default=settings.host)
    ap.add_argument("--port", type=int, default=settings.port)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    if args.transport == "stdio":
        mcp.run("stdio")
        return
    if args.transport in ("sse", "streamable-http"):
        mcp.settings.host, mcp.settings.port = args.host, args.port
        mcp.run(args.transport)
        return
    import uvicorn

    uvicorn.run(build_http_app(), host=args.host, port=args.port, log_level="info", timeout_keep_alive=300)


if __name__ == "__main__":
    main()
