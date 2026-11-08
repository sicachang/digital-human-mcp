"""Step DSL + executor with self-healing element resolution.

A step is a dict, e.g.
  {"action": "click", "target": {"role": "button", "name": "登入"}, "narration": "點擊登入按鈕"}

Actions:
  goto(value=url) | click | dblclick | hover | fill(value) | type(value) | clear | select(value)
  check | uncheck | press(value=key) | scroll(value=up|down|top|bottom) | wait(value=seconds)
  highlight (point at / explain an area, no interaction) | narrate (speech only)
  assert_visible | assert_not_visible | assert_text(value) | assert_url(value) | assert_value(value)
  verify(expect="natural language expectation", judged by the vision model)
  upload(value=file path)

Target keys (any subset, tried in this order): selector, testid, role+name, label, placeholder, text, dh_id
Optional step keys: narration, title, description, zoom (bool), screenshot (bool), timeout_ms, optional (bool)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Locator

from .browser import Session, box_of
from .config import settings
from .llm import get_llm

log = logging.getLogger("dh.steps")

INTERACTIVE = {"click", "dblclick", "hover", "fill", "type", "clear", "select", "check", "uncheck", "upload", "highlight"}
NEEDS_TARGET = INTERACTIVE | {"assert_visible", "assert_not_visible", "assert_value"}
TYPING = {"fill", "type"}


class StepError(Exception):
    pass


@dataclass
class StepResult:
    index: int
    action: str
    ok: bool
    message: str = ""
    healed: bool = False
    resolved_target: dict[str, Any] | None = None
    screenshot: str | None = None
    url: str = ""
    t_start: float = 0.0
    t_end: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


def describe_step(step: dict[str, Any]) -> str:
    t = step.get("target") or {}
    tdesc = ", ".join(f"{k}={v}" for k, v in t.items() if v not in (None, ""))
    s = f"{step.get('action')} [{tdesc}]"
    if step.get("value") not in (None, ""):
        s += f" value={step.get('value')!r}"
    if step.get("narration"):
        s += f" // {step['narration']}"
    return s


def _candidates(page: Any, t: dict[str, Any]) -> list[tuple[str, Locator]]:
    c: list[tuple[str, Locator]] = []
    if t.get("selector"):
        c.append(("selector", page.locator(t["selector"])))
    if t.get("testid"):
        c.append(("testid", page.get_by_test_id(t["testid"])))
    if t.get("role"):
        try:
            if t.get("name"):
                c.append(("role", page.get_by_role(t["role"], name=t["name"], exact=True)))
                c.append(("role~", page.get_by_role(t["role"], name=t["name"])))
            else:
                c.append(("role", page.get_by_role(t["role"])))
        except Exception:  # noqa: BLE001  (invalid role name)
            pass
    if t.get("label"):
        c.append(("label", page.get_by_label(t["label"], exact=True)))
        c.append(("label~", page.get_by_label(t["label"])))
    if t.get("placeholder"):
        c.append(("placeholder", page.get_by_placeholder(t["placeholder"])))
    name_or_text = t.get("text") or (t.get("name") if not t.get("role") else None)
    if name_or_text:
        c.append(("text", page.get_by_text(name_or_text, exact=True)))
        c.append(("text~", page.get_by_text(name_or_text)))
        c.append(("title", page.get_by_title(name_or_text)))
    if t.get("dh_id"):
        c.append(("dh_id", page.locator(f'[data-dh-id="{t["dh_id"]}"]')))
    return c


async def _first_visible(loc: Locator) -> Locator | None:
    try:
        n = await loc.count()
    except Exception:  # noqa: BLE001
        return None
    for i in range(min(n, 8)):
        item = loc.nth(i)
        try:
            if await item.is_visible():
                return item
        except Exception:  # noqa: BLE001
            continue
    return None


def element_to_target(el: dict[str, Any]) -> dict[str, Any]:
    """Turn a snapshot element into a stable semantic target (for replay)."""
    t: dict[str, Any] = {}
    if el.get("testid"):
        t["testid"] = el["testid"]
    role = el.get("role") or {
        "a": "link", "button": "button", "select": "combobox", "textarea": "textbox",
    }.get(el.get("tag", ""))
    if el.get("tag") == "input":
        role = {"checkbox": "checkbox", "radio": "radio", "submit": "button", "button": "button"}.get(
            el.get("type", ""), "textbox"
        )
    label = el.get("aria") or el.get("label")
    if label:
        t["label"] = label
    if el.get("placeholder"):
        t["placeholder"] = el["placeholder"]
    if el.get("text") and len(el["text"]) <= 40:
        t["text"] = el["text"]
        if role in ("button", "link", "tab", "menuitem"):
            t["role"], t["name"] = role, el["text"]
    if el.get("name") and el.get("tag") in ("input", "select", "textarea"):
        t["selector"] = f'{el["tag"]}[name="{el["name"]}"]'
    return t


HEAL_SYSTEM = """你是網頁 UI 自動化專家。根據「要執行的步驟」與「目前頁面可互動元素清單」，選出最符合的元素。
回覆 JSON：{"dh_id": <數字或 null>, "reason": "簡短理由"}。
若頁面上沒有合適元素（例如需要先開啟選單或切換頁籤），dh_id 回 null，並在 reason 說明需要先做什麼。"""


def compact_elements(snap: dict[str, Any], limit: int = 120) -> str:
    lines = []
    for e in snap.get("elements", [])[:limit]:
        parts = [f"#{e['id']}", e["tag"]]
        for k in ("role", "type", "text", "aria", "label", "placeholder", "name", "title", "testid", "href", "value"):
            v = e.get(k)
            if v:
                parts.append(f'{k}="{v}"')
        if e.get("options"):
            parts.append("options=" + "|".join(e["options"]))
        if e.get("disabled"):
            parts.append("disabled")
        if e.get("checked") is not None:
            parts.append(f"checked={e['checked']}")
        if not e.get("in_view"):
            parts.append("(offscreen)")
        lines.append(" ".join(parts))
    return "\n".join(lines)


async def heal_target(sess: Session, step: dict[str, Any]) -> tuple[Locator | None, dict[str, Any] | None, str]:
    snap = await sess.snapshot()
    if not snap.get("elements"):
        return None, None, "頁面上沒有可互動元素"
    user = (
        f"要執行的步驟：{describe_step(step)}\n"
        f"頁面：{snap.get('title')} {snap.get('url')}\n標題：{' / '.join(snap.get('headings', []))}\n"
        f"可互動元素：\n{compact_elements(snap)}"
    )
    images = None
    if settings.use_vision:
        images = [await sess.page.screenshot(type="jpeg", quality=70)]
    try:
        ans = await get_llm().chat_json(HEAL_SYSTEM, user, images=images, max_tokens=300)
    except Exception as e:  # noqa: BLE001
        return None, None, f"self-heal LLM error: {e}"
    dh_id = ans.get("dh_id") if isinstance(ans, dict) else None
    if not dh_id:
        return None, None, (ans or {}).get("reason", "LLM 找不到元素")
    loc = sess.page.locator(f'[data-dh-id="{dh_id}"]').first
    el = next((e for e in snap["elements"] if e["id"] == int(dh_id)), None)
    return loc, (element_to_target(el) if el else None), ans.get("reason", "")


async def resolve(sess: Session, step: dict[str, Any], heal: bool = True) -> tuple[Locator, bool, dict[str, Any] | None]:
    t = step.get("target") or {}
    if isinstance(t, str):
        t = {"text": t}
    for _kind, loc in _candidates(sess.page, t):
        v = await _first_visible(loc)
        if v is not None:
            return v, False, None
    # wait a little for late-rendering UIs, then retry once
    await sess.page.wait_for_timeout(1200)
    for _kind, loc in _candidates(sess.page, t):
        v = await _first_visible(loc)
        if v is not None:
            return v, False, None
    if heal:
        loc, new_t, reason = await heal_target(sess, step)
        if loc is not None and await _first_visible(loc) is not None:
            log.info("self-healed step %s -> %s (%s)", describe_step(step), new_t, reason)
            return loc, True, new_t
        raise StepError(f"找不到目標元素 {t}；AI 判斷：{reason}")
    raise StepError(f"找不到目標元素 {t}")


VERIFY_SYSTEM = """你是嚴謹的 UI 測試驗證員。根據截圖與頁面文字判斷「預期結果」是否成立。
回覆 JSON：{"pass": true/false, "reason": "依據截圖看到的具體證據"}"""


async def verify_expectation(sess: Session, expect: str) -> tuple[bool, str]:
    snap = await sess.snapshot(limit=40)
    user = f"預期結果：{expect}\n目前網址：{snap.get('url')}\n頁面文字（節錄）：{snap.get('text', '')[:1200]}\n提示訊息：{snap.get('alerts')}"
    images = [await sess.page.screenshot(type="jpeg", quality=75)] if settings.use_vision else None
    ans = await get_llm().chat_json(VERIFY_SYSTEM, user, images=images, max_tokens=400)
    return bool(ans.get("pass")), str(ans.get("reason", ""))


def zoom_wanted(step: dict[str, Any], box: tuple[float, float, float, float] | None) -> bool:
    if box is None:
        return False
    z = step.get("zoom")
    if z is not None:
        return bool(z)
    # auto: zoom when typing or when explaining a small area
    _, _, w, h = box
    small = w * h < (settings.viewport_width * settings.viewport_height) * 0.08
    return step.get("action") in TYPING | {"select", "highlight"} and small


async def execute_step(sess: Session, step: dict[str, Any], index: int, *, heal: bool = True,
                       on_point: Any = None) -> StepResult:
    """on_point(box): optional async callback fired after the cursor reached the target, before interacting."""
    action = (step.get("action") or "").lower().strip()
    value = step.get("value")
    res = StepResult(index=index, action=action, ok=True, t_start=sess.now())
    page = sess.page
    try:
        if action == "goto":
            url = str(value or (step.get("target") or {}).get("url") or "")
            await page.goto(url, wait_until="domcontentloaded")
            await sess.settle(600)
        elif action == "wait":
            await page.wait_for_timeout(int(float(value or 1) * 1000))
        elif action == "narrate":
            pass
        elif action == "press":
            if step.get("target"):
                loc, res.healed, res.resolved_target = await resolve(sess, step, heal)
                await loc.press(str(value or "Enter"))
            else:
                await page.keyboard.press(str(value or "Enter"))
            await sess.settle(300)
        elif action == "scroll":
            v = str(value or "down").lower()
            delta = {"down": 500, "up": -500}.get(v)
            if v in ("top", "bottom"):
                await page.evaluate("v => window.scrollTo({top: v === 'top' ? 0 : document.body.scrollHeight, behavior: 'smooth'})", v)
            elif step.get("target"):
                loc, res.healed, res.resolved_target = await resolve(sess, step, heal)
                await loc.scroll_into_view_if_needed()
            else:
                await page.mouse.wheel(0, delta or 500)
            await page.wait_for_timeout(700)
        elif action == "assert_url":
            if str(value) not in page.url:
                raise StepError(f"網址不含 {value!r}，實際為 {page.url}")
        elif action == "assert_text":
            if step.get("target"):
                loc, res.healed, res.resolved_target = await resolve(sess, step, heal)
                txt = await loc.inner_text()
            else:
                await page.wait_for_timeout(300)
                txt = await page.locator("body").inner_text()
            if str(value) not in txt:
                try:
                    await page.get_by_text(str(value)).first.wait_for(state="visible", timeout=step.get("timeout_ms", 5000))
                except Exception as e:  # noqa: BLE001
                    raise StepError(f"頁面未出現文字 {value!r}") from e
        elif action == "assert_not_visible":
            t = step.get("target") or {}
            for _k, loc in _candidates(page, t):
                if await _first_visible(loc) is not None:
                    raise StepError(f"元素不應可見但可見：{t}")
        elif action == "verify":
            ok, reason = await verify_expectation(sess, str(step.get("expect") or value or step.get("narration") or ""))
            res.extra["reason"] = reason
            if not ok:
                raise StepError(f"AI 驗證未通過：{reason}")
        elif action in NEEDS_TARGET:
            loc, res.healed, res.resolved_target = await resolve(sess, step, heal)
            box = await box_of(loc)
            if box:
                cx, cy = box[0] + box[2] / 2, box[1] + box[3] / 2
                if action in TYPING:  # point at the start of the field, like a person would
                    cx = box[0] + min(box[2] * 0.25, 60)
                zoom = sess.animate and zoom_wanted(step, box)
                if zoom:
                    sess.zoom_begin(box)
                if action != "assert_not_visible":
                    await sess.move_to(cx, cy)
                if sess.animate:
                    await sess.highlight(box, step.get("label", ""))
                    await page.wait_for_timeout(350)
            res.extra["box"] = box
            if on_point is not None:
                await on_point(box)
            await _do_interaction(sess, loc, action, value, step)
            if box and zoom and action in ("click", "dblclick", "check", "uncheck"):
                sess.zoom_end(linger=0.3)  # the page may change after a click; release the zoom early
            if sess.animate:
                await sess.highlight(None)
        else:
            raise StepError(f"不支援的 action: {action}")
    except StepError as e:
        res.ok, res.message = False, str(e)
    except Exception as e:  # noqa: BLE001
        res.ok, res.message = False, f"{type(e).__name__}: {str(e).splitlines()[0][:300]}"
    res.url = page.url
    res.t_end = sess.now()
    return res


async def _do_interaction(sess: Session, loc: Locator, action: str, value: Any, step: dict[str, Any]) -> None:
    page = sess.page
    delay = settings.typing_delay_ms if sess.animate else 0
    if action == "highlight":
        await page.wait_for_timeout(600)
    elif action == "hover":
        await loc.hover()
    elif action in ("click", "dblclick"):
        await sess.ripple()
        if action == "click":
            await loc.click()
        else:
            await loc.dblclick()
        await sess.settle(400)
    elif action in TYPING:
        await sess.ripple()
        await loc.click()
        if action == "fill":
            await loc.fill("")
        text = "" if value is None else str(value)
        if delay:
            await loc.press_sequentially(text, delay=delay)
        else:
            await loc.fill(text) if action == "fill" else await loc.press_sequentially(text)
        if step.get("submit"):
            await loc.press("Enter")
            await sess.settle(400)
    elif action == "clear":
        await loc.fill("")
    elif action == "select":
        await sess.ripple()
        tag = await loc.evaluate("e => e.tagName.toLowerCase()")
        if tag == "select":
            try:
                await loc.select_option(label=str(value))
            except Exception:  # noqa: BLE001
                await loc.select_option(value=str(value))
        else:  # custom dropdown: open it, then click the option text
            await loc.click()
            await page.wait_for_timeout(400)
            opt = page.get_by_role("option", name=str(value))
            if await opt.count() == 0:
                opt = page.get_by_text(str(value), exact=True)
            target = opt.first
            b = await box_of(target)
            if b:
                await sess.move_to(b[0] + b[2] / 2, b[1] + b[3] / 2)
                await sess.ripple()
            await target.click()
        await page.wait_for_timeout(300)
    elif action in ("check", "uncheck"):
        await sess.ripple()
        await (loc.check() if action == "check" else loc.uncheck())
    elif action == "upload":
        await loc.set_input_files(str(value))
    elif action == "assert_visible":
        pass
    elif action == "assert_value":
        actual = await loc.input_value()
        if str(value) != actual:
            raise StepError(f"欄位值應為 {value!r}，實際為 {actual!r}")


def normalize_steps(steps: Any) -> list[dict[str, Any]]:
    """Accept loosely-shaped LLM output and coerce into step dicts."""
    out = []
    for s in steps or []:
        if not isinstance(s, dict) or not s.get("action"):
            continue
        s = dict(s)
        s["action"] = re.sub(r"[^a-z_]", "", str(s["action"]).lower())
        if isinstance(s.get("target"), str):
            s["target"] = {"text": s["target"]}
        out.append(s)
    return out
