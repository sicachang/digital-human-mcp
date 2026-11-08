"""Offline text-to-speech for tutorial narration.

Engines (TTS_ENGINE):
  piper  - local neural TTS (default in the Docker image, Mandarin voice bundled)
  openai - any OpenAI-compatible /audio/speech endpoint on your intranet (e.g. CosyVoice/GPT-SoVITS gateways)
  espeak - espeak-ng (robotic, always available as a fallback)
  none   - no voice; subtitles only
"""
from __future__ import annotations

import asyncio
import glob
import logging
import os
import shutil
import subprocess
import wave
from pathlib import Path

import httpx

from .config import settings

log = logging.getLogger("dh.tts")
SAMPLE_RATE = 22050

_piper_voice = None
_t2s = None


def _to_simplified(text: str) -> str:
    """Piper/espeak Mandarin dictionaries pronounce Simplified Chinese more reliably."""
    global _t2s
    if os.environ.get("TTS_T2S", "1") != "1":
        return text
    try:
        if _t2s is None:
            import opencc  # type: ignore

            _t2s = opencc.OpenCC("t2s")
        return _t2s.convert(text)
    except Exception:  # noqa: BLE001
        return text


def _piper_model_path() -> str | None:
    p = os.environ.get("PIPER_MODEL", "")
    if p and Path(p).exists():
        return p
    found = sorted(glob.glob("/opt/voices/*.onnx"))
    return found[0] if found else None


def available_engine() -> str:
    eng = settings.tts_engine
    if eng == "piper" and not _piper_model_path():
        log.warning("piper voice model not found; falling back to espeak")
        eng = "espeak"
    if eng == "espeak" and not shutil.which("espeak-ng"):
        eng = "none"
    if eng == "openai" and not settings.tts_base_url:
        eng = "none"
    return eng


def _normalize(src: Path, dst: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-ac", "1", "-ar", str(SAMPLE_RATE),
         "-sample_fmt", "s16", str(dst)],
        check=True,
    )


def _synth_piper(text: str, out: Path) -> None:
    global _piper_voice
    from piper import PiperVoice  # type: ignore

    if _piper_voice is None:
        _piper_voice = PiperVoice.load(_piper_model_path())
    raw = out.with_suffix(".raw.wav")
    cfg = None
    try:
        from piper import SynthesisConfig  # type: ignore

        cfg = SynthesisConfig(length_scale=float(os.environ.get("TTS_LENGTH_SCALE", "1.0")))
    except Exception:  # noqa: BLE001
        pass
    with wave.open(str(raw), "wb") as wf:
        if cfg is not None:
            _piper_voice.synthesize_wav(_to_simplified(text), wf, syn_config=cfg)
        else:
            _piper_voice.synthesize_wav(_to_simplified(text), wf)
    _normalize(raw, out)
    raw.unlink(missing_ok=True)


def _synth_espeak(text: str, out: Path) -> None:
    raw = out.with_suffix(".raw.wav")
    subprocess.run(
        ["espeak-ng", "-v", settings.espeak_voice, "-s", os.environ.get("ESPEAK_SPEED", "165"),
         "-w", str(raw), _to_simplified(text)],
        check=True,
    )
    _normalize(raw, out)
    raw.unlink(missing_ok=True)


async def _synth_openai(text: str, out: Path) -> None:
    body = {"model": settings.tts_model, "input": text, "response_format": "wav"}
    if settings.tts_voice:
        body["voice"] = settings.tts_voice
    async with httpx.AsyncClient(timeout=120, trust_env=False) as c:
        r = await c.post(f"{settings.tts_base_url}/audio/speech", json=body,
                         headers={"Authorization": f"Bearer {settings.tts_api_key}"})
        r.raise_for_status()
    raw = out.with_suffix(".raw")
    raw.write_bytes(r.content)
    await asyncio.to_thread(_normalize, raw, out)
    raw.unlink(missing_ok=True)


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


async def synthesize(text: str, out: Path, engine: str | None = None) -> float | None:
    """Synthesize `text` to a mono 22.05 kHz wav. Returns duration in seconds, or None if no voice."""
    engine = engine or available_engine()
    text = (text or "").strip()
    if not text or engine == "none":
        return None
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        if engine == "piper":
            await asyncio.to_thread(_synth_piper, text, out)
        elif engine == "espeak":
            await asyncio.to_thread(_synth_espeak, text, out)
        elif engine == "openai":
            await _synth_openai(text, out)
        else:
            return None
        return wav_duration(out)
    except Exception as e:  # noqa: BLE001
        log.warning("TTS (%s) failed: %s", engine, e)
        if engine != "espeak" and shutil.which("espeak-ng"):
            return await synthesize(text, out, "espeak")
        return None


def build_track(clips: list[tuple[float, Path]], total: float, out: Path) -> Path:
    """Place clips (start_seconds, wav) on a silent track of `total` seconds."""
    n_total = int((total + 0.5) * SAMPLE_RATE)
    buf = bytearray(n_total * 2)
    for start, path in clips:
        if start < 0 or not path.exists():
            continue
        with wave.open(str(path), "rb") as w:
            frames = w.readframes(w.getnframes())
        off = int(start * SAMPLE_RATE) * 2
        end = min(len(buf), off + len(frames))
        if off < len(buf):
            buf[off:end] = frames[: end - off]
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(bytes(buf))
    return out
