"""Post-production with ffmpeg: smooth zoom-ins, burned subtitles, narration track, mp4 output."""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .browser import ZoomEvent
from .config import settings
from .tts import build_track

log = logging.getLogger("dh.video")
FPS = 25
RAMP = 0.6  # seconds for zoom in / out


@dataclass
class Cue:
    start: float
    end: float
    text: str


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=False,
    )
    try:
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception:  # noqa: BLE001
        # webm from Playwright sometimes lacks a duration header; decode to find it
        out = subprocess.run(
            ["ffmpeg", "-i", str(path), "-f", "null", "-"], capture_output=True, text=True, check=False
        )
        times = re.findall(r"time=(\d+):(\d+):([\d.]+)", out.stderr)
        if times:
            h, m, s = times[-1]
            return int(h) * 3600 + int(m) * 60 + float(s)
        return 0.0


# ---------------------------------------------------------------- subtitles
def split_caption(text: str, max_chars: int = 24) -> list[str]:
    parts = [p for p in re.split(r"(?<=[。！？!?；;，,、])", text.strip()) if p.strip()]
    chunks: list[str] = []
    cur = ""
    for p in parts:
        if len(cur) + len(p) <= max_chars or not cur:
            cur += p
        else:
            chunks.append(cur)
            cur = p
    if cur:
        chunks.append(cur)
    # hard-wrap anything still too long
    out = []
    for c in chunks:
        while len(c) > max_chars * 1.6:
            out.append(c[:max_chars])
            c = c[max_chars:]
        out.append(c)
    return [c.strip() for c in out if c.strip()]


def expand_cues(cues: list[Cue]) -> list[Cue]:
    """Split long narration into readable subtitle lines, timed proportionally to length."""
    out: list[Cue] = []
    for c in cues:
        chunks = split_caption(c.text)
        total = sum(len(x) for x in chunks) or 1
        t = c.start
        for ch in chunks:
            d = (c.end - c.start) * len(ch) / total
            out.append(Cue(t, t + d, ch))
            t += d
    return out


def _ts(t: float, sep: str = ".") -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    if sep == ",":
        return f"{int(h):02d}:{int(m):02d}:{s:06.3f}".replace(".", ",")
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def write_srt(cues: list[Cue], path: Path) -> Path:
    lines = []
    for i, c in enumerate(cues, 1):
        lines += [str(i), f"{_ts(c.start, ',')} --> {_ts(c.end, ',')}", c.text, ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _ass_escape(s: str) -> str:
    return s.replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\n", "\\N")


def write_ass(cues: list[Cue], path: Path, w: int, h: int, title: str = "", title_secs: float = 3.5) -> Path:
    font = os.environ.get("SUBTITLE_FONT", "Noto Sans CJK TC")
    fs = max(22, int(h * 0.045))
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,{font},{fs},&H00FFFFFF,&H00FFFFFF,&H64000000,&H64000000,1,0,0,0,100,100,0,0,3,10,0,2,60,60,{int(h * 0.05)},1
Style: Title,{font},{int(fs * 1.5)},&H00FFFFFF,&H00FFFFFF,&H90302010,&H90302010,1,0,0,0,100,100,0,0,3,24,0,5,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    if title:
        ev.append(f"Dialogue: 1,{_ts(0)},{_ts(title_secs)},Title,,0,0,0,,{{\\fad(300,400)}}{_ass_escape(title)}")
    for c in cues:
        ev.append(f"Dialogue: 0,{_ts(c.start)},{_ts(c.end)},Sub,,0,0,0,,{{\\fad(120,120)}}{_ass_escape(c.text)}")
    path.write_text(header + "\n".join(ev) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------- zoom
def plan_zooms(zooms: list[ZoomEvent], w: int, h: int, shift: float) -> list[tuple[float, float, float, float, float]]:
    """Return non-overlapping (t0, t1, factor, cx, cy) segments in output time."""
    segs: list[list[float]] = []
    for z in sorted(zooms, key=lambda z: z.t0):
        x, y, bw, bh = z.box
        rw = max(bw * 2.0, w / 2.6)
        rh = max(bh * 3.0, h / 2.6)
        f = max(1.25, min(2.2, min(w / rw, h / rh)))
        t0, t1 = z.t0 - shift - 0.15, z.t1 - shift
        if t1 - t0 < 0.4:
            t1 = t0 + 0.4
        cx, cy = x + bw / 2, y + bh / 2
        if segs and t0 < segs[-1][1]:
            prev = segs[-1]
            if abs(prev[3] - cx) < w * 0.2 and abs(prev[4] - cy) < h * 0.2:
                prev[1] = max(prev[1], t1)  # same area: extend
                continue
            prev[1] = max(prev[0] + 0.4, t0)
        segs.append([max(0.0, t0), t1, f, cx, cy])
    return [tuple(s) for s in segs]  # type: ignore[misc]


def zoompan_filter(segs: list[tuple[float, float, float, float, float]], w: int, h: int, k: int) -> str:
    T = f"(in/{FPS})"

    def accum(with_center: bool) -> str:
        parts = ["st(1,1)", f"st(2,{w * k / 2})", f"st(3,{h * k / 2})"]
        for t0, t1, f, cx, cy in segs:
            parts.append(f"st(0,clip(min(({T}-{t0:.3f})/{RAMP},({t1:.3f}-{T})/{RAMP}),0,1))")
            parts.append("st(0,ld(0)*ld(0)*(3-2*ld(0)))")
            parts.append(f"st(1,ld(1)+{f - 1:.3f}*ld(0))")
            if with_center:
                parts.append(f"st(2,ld(2)+{(cx * k) - w * k / 2:.2f}*ld(0))")
                parts.append(f"st(3,ld(3)+{(cy * k) - h * k / 2:.2f}*ld(0))")
        return ";".join(parts)

    z = accum(False) + ";ld(1)"
    x = accum(True) + ";clip(ld(2)-iw/ld(1)/2,0,iw-iw/ld(1))"
    y = accum(True) + ";clip(ld(3)-ih/ld(1)/2,0,ih-ih/ld(1))"
    return f"zoompan=z='{z}':x='{x}':y='{y}':d=1:s={w}x{h}:fps={FPS}"


# ---------------------------------------------------------------- render
def render_tutorial_video(
    *,
    raw_video: Path,
    out_dir: Path,
    cues: list[Cue],
    zooms: list[ZoomEvent],
    audio_clips: list[tuple[float, Path]],
    trim_start: float,
    title: str,
    name: str = "tutorial",
) -> dict[str, str]:
    w, h = settings.viewport_width, settings.viewport_height
    dur = probe_duration(raw_video)
    total = max(0.5, dur - trim_start)
    shifted = [Cue(c.start - trim_start, c.end - trim_start, c.text) for c in cues]
    subs = expand_cues(shifted)
    srt = write_srt(subs, out_dir / f"{name}.srt")
    ass = write_ass(subs, out_dir / f"{name}.ass", w, h, title=title)

    segs = plan_zooms(zooms, w, h, trim_start)
    k = 2 if segs else 1
    chain = [f"[0:v]trim=start={trim_start:.3f},setpts=PTS-STARTPTS,fps={FPS}"]
    if segs:
        chain.append(f"scale={w * k}:{h * k}:flags=lanczos")
        chain.append(zoompan_filter(segs, w, h, k))
    chain.append(f"ass=filename='{ass.as_posix()}'")
    chain.append("format=yuv420p[v]")
    fscript = out_dir / f"{name}.filter.txt"
    fscript.write_text(",".join(chain), encoding="utf-8")

    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw_video)]
    has_audio = bool(audio_clips)
    if has_audio:
        track = build_track([(s - trim_start, p) for s, p in audio_clips], total, out_dir / f"{name}.narration.wav")
        cmd += ["-i", str(track)]
    cmd += ["-i", str(srt), "-filter_complex_script", str(fscript), "-map", "[v]"]
    if has_audio:
        cmd += ["-map", "1:a", "-c:a", "aac", "-b:a", "128k"]
    sub_idx = 2 if has_audio else 1
    cmd += ["-map", f"{sub_idx}:s", "-c:s", "mov_text", "-metadata:s:s:0", "language=chi",
            "-c:v", "libx264", "-preset", os.environ.get("X264_PRESET", "veryfast"), "-crf", "21",
            "-r", str(FPS), "-t", f"{total:.3f}", "-movflags", "+faststart", str(out_dir / f"{name}.mp4")]
    log.info("ffmpeg: %s", " ".join(cmd))
    p = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg 失敗：{p.stderr[-1500:]}")
    return {"mp4": str(out_dir / f"{name}.mp4"), "srt": str(srt), "ass": str(ass)}


def webm_to_mp4(src: Path, dst: Path) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "23", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(dst)],
        check=True,
    )
    return dst
