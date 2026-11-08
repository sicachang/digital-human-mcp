"""Background jobs, so long-running recordings don't hit MCP client timeouts."""
from __future__ import annotations

import asyncio
import datetime as dt
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class Job:
    id: str
    kind: str
    status: str = "running"  # running | done | error
    progress: float = 0.0
    message: str = ""
    result: Any = None
    error: str = ""
    created: str = field(default_factory=lambda: dt.datetime.now().isoformat(timespec="seconds"))
    task: asyncio.Task | None = None

    def view(self, full: bool = True) -> dict[str, Any]:
        d = {"job_id": self.id, "kind": self.kind, "status": self.status, "progress": round(self.progress, 2),
             "message": self.message, "created": self.created}
        if self.status == "done" and full:
            d["result"] = self.result
        if self.status == "error":
            d["error"] = self.error
        return d


JOBS: dict[str, Job] = {}


def start(kind: str, fn: Callable[[Callable[[str, float], Awaitable[None]]], Awaitable[Any]]) -> Job:
    job = Job(id=uuid.uuid4().hex[:10], kind=kind)

    async def progress(msg: str, frac: float) -> None:
        job.message, job.progress = msg, frac

    async def runner() -> None:
        try:
            job.result = await fn(progress)
            job.status, job.progress, job.message = "done", 1.0, "完成"
        except Exception as e:  # noqa: BLE001
            job.status, job.error = "error", f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}"

    job.task = asyncio.create_task(runner())
    JOBS[job.id] = job
    return job
