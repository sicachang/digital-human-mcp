"""Playwright browser management, human-like actions and a recording timeline."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, BrowserContext, Locator, Page, Playwright, async_playwright

from .config import settings

log = logging.getLogger("dh.browser")
OVERLAY_JS = (Path(__file__).parent / "overlay.js").read_text(encoding="utf-8")


class BrowserManager:
    def __init__(self) -> None:
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._lock = asyncio.Lock()
        self.sem = asyncio.Semaphore(settings.max_concurrent_browsers)

    async def browser(self) -> Browser:
        async with self._lock:
            if self._browser and self._browser.is_connected():
                return self._browser
            if not self._pw:
                self._pw = await async_playwright().start()
            kw: dict[str, Any] = {
                "headless": settings.headless,
                "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--font-render-hinting=none"],
            }
            if settings.chromium_executable:
                kw["executable_path"] = settings.chromium_executable
            self._browser = await self._pw.chromium.launch(**kw)
            return self._browser

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        self._browser = self._pw = None


manager = BrowserManager()


@dataclass
class ZoomEvent:
    t0: float
    t1: float
    box: tuple[float, float, float, float]  # x, y, w, h in viewport CSS px


@dataclass
class Session:
    context: BrowserContext
    page: Page
    video_dir: Path | None
    animate: bool  # show cursor + slow human-like motion
    t_start: float = field(default_factory=time.monotonic)
    zooms: list[ZoomEvent] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)
    cursor: tuple[float, float] = (-1.0, -1.0)
    _zoom_open: tuple[float, tuple[float, float, float, float]] | None = None

    # ---------- time ----------
    def now(self) -> float:
        """Seconds since the video started."""
        return time.monotonic() - self.t_start

    # ---------- zoom (rendered later by ffmpeg) ----------
    def zoom_begin(self, box: tuple[float, float, float, float]) -> None:
        if self._zoom_open:
            self.zoom_end()
        self._zoom_open = (self.now(), box)

    def zoom_end(self, linger: float = 0.8) -> None:
        if not self._zoom_open:
            return
        t0, box = self._zoom_open
        self.zooms.append(ZoomEvent(t0, self.now() + linger, box))
        self._zoom_open = None

    # ---------- overlay ----------
    async def _overlay(self, expr: str, *args: Any) -> Any:
        try:
            await self.page.evaluate("() => window.__dh || null")
            return await self.page.evaluate(expr, *args) if args else await self.page.evaluate(expr)
        except Exception as e:  # noqa: BLE001  (navigation in progress etc.)
            log.debug("overlay call failed: %s", e)
            return None

    async def ensure_cursor(self) -> None:
        if not self.animate:
            return
        x, y = self.cursor
        if x < 0:
            x, y = settings.viewport_width * 0.5, settings.viewport_height * 0.6
            self.cursor = (x, y)
        await self._overlay("([x, y]) => window.__dh && window.__dh.showCursor(x, y)", [x, y])

    async def move_to(self, x: float, y: float) -> None:
        """Move the visible cursor and the real mouse to (x, y)."""
        if self.animate:
            await self.ensure_cursor()
            cx, cy = self.cursor
            dist = math.hypot(x - cx, y - cy)
            ms = int(min(1100, max(350, dist * 1.1)))
            steps = max(5, ms // 40)
            anim = self._overlay(
                "([x, y, ms]) => window.__dh && window.__dh.moveCursor(x, y, ms)", [x, y, ms]
            )
            await asyncio.gather(anim, self.page.mouse.move(x, y, steps=steps))
        else:
            await self.page.mouse.move(x, y)
        self.cursor = (x, y)

    async def ripple(self) -> None:
        if self.animate:
            x, y = self.cursor
            await self._overlay("([x, y]) => window.__dh && window.__dh.ripple(x, y)", [x, y])

    async def highlight(self, box: tuple[float, float, float, float] | None, label: str = "") -> None:
        if box is None:
            await self._overlay("() => window.__dh && window.__dh.clearHighlight()")
            return
        x, y, w, h = box
        await self._overlay(
            "([r, l]) => window.__dh && window.__dh.highlight(r, l)",
            [{"x": x, "y": y, "width": w, "height": h}, label],
        )

    async def snapshot(self, limit: int = 150) -> dict[str, Any]:
        for _ in range(3):
            try:
                snap = await self.page.evaluate("(n) => window.__dh.snapshot(n)", limit)
                if snap:
                    return snap
            except Exception:  # noqa: BLE001
                await self.page.wait_for_timeout(500)
        return {"url": self.page.url, "title": "", "elements": [], "headings": [], "text": ""}

    async def screenshot(self, path: Path | None = None, full_page: bool = False) -> bytes:
        data = await self.page.screenshot(full_page=full_page, type="png")
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return data

    async def settle(self, ms: int = 400) -> None:
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=settings.nav_timeout_ms)
            await self.page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:  # noqa: BLE001
            pass
        await self.page.wait_for_timeout(ms)
        if self.animate:
            await self.ensure_cursor()

    async def close(self) -> Path | None:
        """Close the context; returns the recorded video path if any."""
        self.zoom_end(linger=0.0)
        video = self.page.video
        await self.context.close()
        if video:
            try:
                return Path(await video.path())
            except Exception:  # noqa: BLE001
                return None
        return None


async def new_session(
    *,
    video_dir: Path | None = None,
    animate: bool = False,
    storage_state: str | None = None,
    viewport: tuple[int, int] | None = None,
) -> Session:
    browser = await manager.browser()
    vw, vh = viewport or (settings.viewport_width, settings.viewport_height)
    kw: dict[str, Any] = {
        "viewport": {"width": vw, "height": vh},
        "locale": settings.locale,
        "ignore_https_errors": settings.ignore_https_errors,
        "device_scale_factor": 1,
    }
    if video_dir:
        video_dir.mkdir(parents=True, exist_ok=True)
        kw["record_video_dir"] = str(video_dir)
        kw["record_video_size"] = {"width": vw, "height": vh}
    if storage_state and Path(storage_state).exists():
        kw["storage_state"] = storage_state
    ctx = await browser.new_context(**kw)
    ctx.set_default_timeout(settings.action_timeout_ms)
    ctx.set_default_navigation_timeout(settings.nav_timeout_ms)
    await ctx.add_init_script(OVERLAY_JS)
    page = await ctx.new_page()
    s = Session(context=ctx, page=page, video_dir=video_dir, animate=animate)
    page.on("console", lambda m: s.console_errors.append(m.text[:300]) if m.type == "error" else None)
    page.on("pageerror", lambda e: s.page_errors.append(str(e)[:300]))
    page.on(
        "response",
        lambda r: s.failed_requests.append(f"{r.status} {r.request.method} {r.url[:200]}") if r.status >= 400 else None,
    )
    return s


async def box_of(loc: Locator) -> tuple[float, float, float, float] | None:
    try:
        await loc.scroll_into_view_if_needed(timeout=3000)
    except Exception:  # noqa: BLE001
        pass
    b = await loc.bounding_box()
    if not b:
        return None
    return (b["x"], b["y"], b["width"], b["height"])
