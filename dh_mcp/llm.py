"""Minimal async client for an OpenAI-compatible endpoint (vLLM / Qwen)."""
from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any

import httpx

from .config import settings

log = logging.getLogger("dh.llm")

_THINK_RE = re.compile(r"<think>.*?</think>", re.S)


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self) -> None:
        self._model: str | None = settings.llm_model or None
        self._client = httpx.AsyncClient(
            base_url=settings.llm_base_url,
            timeout=httpx.Timeout(settings.llm_timeout, connect=15),
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            trust_env=False,  # intranet endpoint: never go through a proxy
        )

    async def model(self) -> str:
        if self._model:
            return self._model
        r = await self._client.get("/models")
        r.raise_for_status()
        data = r.json().get("data") or []
        if not data:
            raise LLMError("LLM endpoint returned no models; set LLM_MODEL explicitly")
        self._model = data[0]["id"]
        log.info("auto-detected model: %s", self._model)
        return self._model

    async def health(self) -> dict[str, Any]:
        try:
            m = await self.model()
            txt = await self.chat([{"role": "user", "content": "回覆 OK 兩個字母即可"}], max_tokens=16)
            return {"ok": True, "model": m, "reply": txt[:50]}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "base_url": settings.llm_base_url}

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        vision: bool = False,
    ) -> str:
        model = settings.vision_model if (vision and settings.vision_model) else await self.model()
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens or settings.llm_max_tokens,
            "temperature": settings.llm_temperature if temperature is None else temperature,
        }
        if not settings.llm_enable_thinking:
            # vLLM / SGLang honour this for Qwen3 chat templates; harmless elsewhere.
            body["chat_template_kwargs"] = {"enable_thinking": False}
        r = await self._client.post("/chat/completions", json=body)
        if r.status_code == 400 and "chat_template_kwargs" in body:
            body.pop("chat_template_kwargs")
            r = await self._client.post("/chat/completions", json=body)
        if r.status_code >= 400:
            raise LLMError(f"LLM HTTP {r.status_code}: {r.text[:500]}")
        msg = r.json()["choices"][0]["message"]
        content = msg.get("content") or ""
        return _THINK_RE.sub("", content).strip()

    async def chat_json(
        self,
        system: str,
        user: str,
        *,
        images: list[bytes] | None = None,
        max_tokens: int | None = None,
        retries: int = 2,
    ) -> Any:
        """Ask for JSON and parse it robustly; retries with the parse error as feedback."""
        content: Any = user
        if images:
            content = [{"type": "text", "text": user}] + [image_part(b) for b in images]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system + "\n\n只輸出 JSON，不要輸出任何其他文字或 markdown 說明。"},
            {"role": "user", "content": content},
        ]
        last_err = ""
        for _ in range(retries + 1):
            txt = await self.chat(messages, max_tokens=max_tokens, vision=bool(images))
            try:
                return extract_json(txt)
            except ValueError as e:
                last_err = str(e)
                messages.append({"role": "assistant", "content": txt[:4000]})
                messages.append({"role": "user", "content": f"上面的輸出不是合法 JSON（{e}）。請只輸出修正後的 JSON。"})
        raise LLMError(f"LLM did not return valid JSON: {last_err}")

    async def describe_image(self, image: bytes, question: str) -> str:
        return await self.chat(
            [{"role": "user", "content": [{"type": "text", "text": question}, image_part(image)]}],
            vision=True,
            max_tokens=1024,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def image_part(png: bytes) -> dict[str, Any]:
    b64 = base64.b64encode(png).decode()
    mime = "image/jpeg" if png[:3] == b"\xff\xd8\xff" else "image/png"
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def extract_json(text: str) -> Any:
    text = _THINK_RE.sub("", text).strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # find the first balanced {...} or [...]
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start = text.find(open_c)
        while start != -1:
            depth, in_str, esc = 0, False, False
            for i in range(start, len(text)):
                c = text[i]
                if in_str:
                    if esc:
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == '"':
                        in_str = False
                elif c == '"':
                    in_str = True
                elif c == open_c:
                    depth += 1
                elif c == close_c:
                    depth -= 1
                    if depth == 0:
                        cand = text[start : i + 1]
                        try:
                            return json.loads(cand)
                        except json.JSONDecodeError:
                            cand2 = re.sub(r",\s*([}\]])", r"\1", cand)  # trailing commas
                            try:
                                return json.loads(cand2)
                            except json.JSONDecodeError:
                                break
            start = text.find(open_c, start + 1)
    raise ValueError("no JSON object found")


_llm: LLM | None = None


def get_llm() -> LLM:
    global _llm
    if _llm is None:
        _llm = LLM()
    return _llm
