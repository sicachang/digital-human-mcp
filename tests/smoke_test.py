"""End-to-end smoke test through a real MCP client.
Usage: python tests/smoke_test.py http://127.0.0.1:8000/mcp http://127.0.0.1:8088/index.html [--quick]"""
import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def show(name, res):
    txt = res.content[0].text if res.content else ""
    print(f"\n===== {name} =====\n{txt[:1800]}")
    try:
        return json.loads(txt)
    except Exception:
        return {}


async def main(mcp_url: str, app_url: str, quick: bool):
    async with streamablehttp_client(mcp_url, timeout=60, sse_read_timeout=900) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            print("tools:", [t.name for t in tools.tools])
            show("check", await s.call_tool("dh_check_environment", {}))
            show("tests", await s.call_tool("dh_run_ui_tests", {"url": app_url, "goal": "登入與報表"}))
            if quick:
                return
            job = show("create_tutorial", await s.call_tool("dh_create_tutorial", {"url": app_url, "topic": "新增客戶"}))
            while True:
                st = show("status", await s.call_tool("dh_job_status", {"job_id": job["job_id"], "wait_seconds": 30}))
                if st.get("status") != "running":
                    break
            show("agent", await s.call_tool("dh_agent_task", {"url": app_url, "goal": "新增一位客戶", "record_video": True}))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], "--quick" in sys.argv))
