"""A tiny fake OpenAI-compatible LLM used for offline smoke tests (no real model needed).
It recognizes which agent prompt is calling and returns plausible JSON for the bundled demo app."""
import json
import os
import re
import sys

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

CALLS: list[str] = []

TUTORIAL = {
    "title": "如何在示範 CRM 新增客戶",
    "audience": "業務同仁",
    "intro": "大家好，這支影片教你如何登入示範 CRM，並新增一位客戶。",
    "prerequisites": ["已取得系統帳號與密碼"],
    "sections": [
        {"title": "登入系統", "description": "使用公司配發的帳號登入。", "steps": [
            {"action": "fill", "target": {"label": "帳號"}, "value": "demo", "title": "輸入帳號", "narration": "首先在帳號欄位輸入你的帳號。"},
            {"action": "fill", "target": {"label": "密碼"}, "value": "demo123", "title": "輸入密碼", "narration": "接著輸入密碼，密碼會以圓點隱藏。"},
            {"action": "click", "target": {"role": "button", "name": "登入"}, "title": "點擊登入", "narration": "按下登入按鈕進入系統。"},
        ]},
        {"title": "新增客戶", "description": "在客戶管理頁面建立新客戶。", "steps": [
            # deliberately wrong name -> exercises self-healing
            {"action": "click", "target": {"role": "link", "name": "客戶資料維護"}, "title": "進入客戶管理", "narration": "從左側選單進入客戶管理。"},
            {"action": "click", "target": {"testid": "add-customer"}, "title": "點擊新增客戶", "narration": "點擊新增客戶按鈕，開啟表單。"},
            {"action": "fill", "target": {"label": "客戶名稱"}, "value": "智慧數字人科技", "title": "輸入客戶名稱", "narration": "輸入客戶名稱，這是必填欄位。", "zoom": True},
            {"action": "fill", "target": {"placeholder": "02-1234-5678"}, "value": "02-8888-9999", "title": "輸入電話", "narration": "再輸入聯絡電話。"},
            {"action": "select", "target": {"label": "客戶等級"}, "value": "VIP", "title": "選擇等級", "narration": "客戶等級選擇 VIP。"},
            {"action": "click", "target": {"role": "button", "name": "儲存"}, "title": "儲存", "narration": "最後按下儲存。"},
            {"action": "highlight", "target": {"text": "客戶已新增成功"}, "title": "確認結果", "narration": "右上角出現新增成功的提示，表格中也多了一筆資料。"},
        ]},
    ],
    "outro": "以上就是新增客戶的完整流程，謝謝收看。",
    "tips": ["客戶名稱為必填欄位"],
}

PLAN = {"scenarios": [
    {"name": "正確帳密登入", "priority": "high", "steps": [
        {"action": "fill", "target": {"label": "帳號"}, "value": "demo"},
        {"action": "fill", "target": {"label": "密碼"}, "value": "demo123"},
        {"action": "click", "target": {"role": "button", "name": "登入"}},
        {"action": "assert_text", "value": "歡迎回來"}]},
    {"name": "錯誤密碼顯示錯誤訊息", "priority": "high", "steps": [
        {"action": "fill", "target": {"label": "帳號"}, "value": "demo"},
        {"action": "fill", "target": {"label": "密碼"}, "value": "wrong"},
        {"action": "click", "target": {"role": "button", "name": "登入"}},
        {"action": "verify", "expect": "畫面顯示帳號或密碼錯誤"}]},
    {"name": "報表頁應有圖表（預期失敗）", "priority": "medium", "steps": [
        {"action": "fill", "target": {"label": "帳號"}, "value": "demo"},
        {"action": "fill", "target": {"label": "密碼"}, "value": "demo123"},
        {"action": "click", "target": {"role": "button", "name": "登入"}},
        {"action": "click", "target": {"role": "link", "name": "報表"}},
        {"action": "assert_text", "value": "月營收圖表"}]},
]}


def pick_element(user: str) -> int | None:
    step = re.search(r"要執行的步驟：(.*)", user)
    want = step.group(1) if step else ""
    for line in re.findall(r"^#(\d+) .*$", user, re.M):
        pass
    best = None
    for m in re.finditer(r"^#(\d+) (.*)$", user, re.M):
        if "客戶" in want and "客戶管理" in m.group(2):
            return int(m.group(1))
        names = re.findall(r'"([^"]+)"', want)
        if any(n and n in m.group(2) for n in names):
            best = best or int(m.group(1))
    return best


def agent_decide(user: str) -> dict:
    done = user.count("→ 成功")
    els = {m.group(2): int(m.group(1)) for m in re.finditer(r"^#(\d+) \S+ .*?(?:text|label|placeholder)=\"([^\"]+)\"", user, re.M)}

    def el(*keys):
        for k in keys:
            for name, i in els.items():
                if k in name:
                    return i
        return None
    seq = [
        ("fill", el("請輸入帳號", "帳號"), "demo", "輸入帳號"),
        ("fill", el("請輸入密碼", "密碼"), "demo123", "輸入密碼"),
        ("click", el("登入"), None, "點擊登入"),
        ("click", el("客戶管理"), None, "進入客戶管理"),
        ("click", el("新增客戶"), None, "開啟新增表單"),
        ("fill", el("台灣科技", "客戶名稱"), "代理人測試公司", "輸入客戶名稱"),
        ("click", el("儲存"), None, "儲存"),
    ]
    if done >= len(seq):
        return {"thought": "已完成", "done": True, "result": "已新增客戶"}
    a, i, v, t = seq[done]
    return {"thought": t, "done": False, "step": {"action": a, "target": {"dh_id": i}, "value": v, "title": t, "narration": t}}


async def chat(req: Request):
    body = await req.json()
    msgs = body["messages"]
    system = msgs[0]["content"] if msgs[0]["role"] == "system" else ""
    u = msgs[-1]["content"]
    user = u if isinstance(u, str) else " ".join(p.get("text", "") for p in u if p.get("type") == "text")
    if "教學影片導演兼技術文件作者" in system:
        kind, out = "tutorial", TUTORIAL
    elif "已實際驗證可執行" in system:
        kind = "narrate"
        steps = json.loads(user.split("已驗證步驟：\n", 1)[1])
        out = {"title": "代理人示範", "intro": "這是代理人實地操作的示範。", "sections": [{"title": "操作", "steps": steps}], "outro": "完成。"}
    elif "QA 自動化測試工程師" in system:
        kind, out = "plan", PLAN
    elif "選出最符合的元素" in system:
        kind, out = "heal", {"dh_id": pick_element(user), "reason": "文字最相近"}
    elif "UI 測試驗證員" in system:
        kind, out = "verify", {"pass": "錯誤" in user, "reason": "頁面文字含錯誤訊息" if "錯誤" in user else "未見預期內容"}
    elif "QA 主管" in system:
        kind, out = "report", {"summary": "3 個情境中 2 個通過，報表頁缺少圖表。", "bugs": [{"title": "報表頁無圖表", "severity": "minor", "scenario": "報表", "evidence": "頁面顯示建置中", "suggestion": "確認需求"}], "recommendations": []}
    elif "修復專家" in system:
        kind, out = "repair", {"steps": [], "note": "mock"}
    elif "AI 代理人" in system:
        kind, out = "agent", agent_decide(user)
    elif "功能地圖" in system:
        kind, out = "project", {"app_name": "示範 CRM", "summary": "客戶管理系統", "features": [{"name": "新增客戶", "route": "/customers"}]}
    else:
        kind, out = "text", "OK，這是登入頁面，可以輸入帳號密碼。"
    CALLS.append(kind)
    print("mock llm:", kind, file=sys.stderr)
    content = out if isinstance(out, str) else "```json\n" + json.dumps(out, ensure_ascii=False) + "\n```"
    return JSONResponse({"choices": [{"message": {"role": "assistant", "content": content}}]})


async def models(_):
    return JSONResponse({"data": [{"id": "mock-qwen"}]})


app = Starlette(routes=[Route("/v1/chat/completions", chat, methods=["POST"]), Route("/v1/models", models)])

if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("MOCK_HOST", "127.0.0.1"), port=int(sys.argv[1]) if len(sys.argv) > 1 else 9192, log_level="warning")
