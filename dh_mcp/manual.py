"""Build the illustrated user manual (Markdown, HTML, PDF, DOCX) from a recorded tutorial."""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .browser import manager

log = logging.getLogger("dh.manual")
_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=select_autoescape(["html", "j2"])
)


def op_text(step: dict[str, Any]) -> str:
    a = step.get("action", "")
    t = step.get("target") or {}
    name = t.get("name") or t.get("text") or t.get("label") or t.get("placeholder") or t.get("testid") or ""
    v = step.get("value")
    return {
        "click": f"點擊「{name}」", "dblclick": f"雙擊「{name}」", "hover": f"滑鼠移到「{name}」",
        "fill": f"在「{name}」輸入 {v}", "type": f"在「{name}」輸入 {v}", "select": f"在「{name}」選擇 {v}",
        "check": f"勾選「{name}」", "uncheck": f"取消勾選「{name}」", "press": f"按下 {v}",
        "goto": f"開啟 {v}", "highlight": f"查看「{name}」",
    }.get(a, "")


def build_manual(doc: dict[str, Any], out_dir: Path, name: str = "manual") -> dict[str, str]:
    """doc = {title, audience, url, intro, prerequisites, tips, video, sections:[{title, description, steps:[...]}]}
    each step: {title, text, image (relative path), op, error}"""
    out: dict[str, str] = {}
    ctx = dict(doc)
    ctx.setdefault("generated", dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
    ctx.setdefault("audience", "一般使用者")
    html = _env.get_template("manual.html.j2").render(**ctx)
    hp = out_dir / f"{name}.html"
    hp.write_text(html, encoding="utf-8")
    out["html"] = str(hp)

    md = [f"# {doc['title']}", "", f"> 適用對象：{ctx['audience']}　系統網址：{doc.get('url', '')}", ""]
    if doc.get("intro"):
        md += [doc["intro"], ""]
    if doc.get("video"):
        md += [f"教學影片：[{doc['video']}]({doc['video']})", ""]
    if doc.get("prerequisites"):
        md += ["## 開始之前", ""] + [f"- {p}" for p in doc["prerequisites"]] + [""]
    for i, s in enumerate(doc.get("sections", []), 1):
        md += [f"## {i}. {s['title']}", ""]
        if s.get("description"):
            md += [s["description"], ""]
        for st in s.get("steps", []):
            md += [f"### 步驟 {st['number']}：{st['title']}", "", st.get("text", ""), ""]
            if st.get("op"):
                md += [f"*操作：{st['op']}*", ""]
            if st.get("error"):
                md += [f"> ⚠ 自動執行時發生問題：{st['error']}", ""]
            if st.get("image"):
                md += [f"![{st['title']}]({st['image']})", ""]
    if doc.get("tips"):
        md += ["## 小提醒", ""] + [f"- {t}" for t in doc["tips"]] + [""]
    mp = out_dir / f"{name}.md"
    mp.write_text("\n".join(md), encoding="utf-8")
    out["markdown"] = str(mp)

    try:
        out["docx"] = str(_build_docx(doc, out_dir, name))
    except Exception as e:  # noqa: BLE001
        log.warning("docx build failed: %s", e)
    return out


async def html_to_pdf(html_path: Path, pdf_path: Path) -> Path:
    browser = await manager.browser()
    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.goto(html_path.resolve().as_uri(), wait_until="load")
    await page.pdf(path=str(pdf_path), format="A4", print_background=True,
                   margin={"top": "14mm", "bottom": "14mm", "left": "10mm", "right": "10mm"},
                   display_header_footer=True, header_template="<span></span>",
                   footer_template='<div style="font-size:9px;width:100%;text-align:center;color:#888">'
                                   '<span class="pageNumber"></span> / <span class="totalPages"></span></div>')
    await ctx.close()
    return pdf_path


def _build_docx(doc: dict[str, Any], out_dir: Path, name: str) -> Path:
    from docx import Document  # type: ignore
    from docx.oxml.ns import qn  # type: ignore
    from docx.shared import Cm, Pt  # type: ignore

    d = Document()
    style = d.styles["Normal"]
    style.font.name = "Microsoft JhengHei"
    style.element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft JhengHei")
    style.font.size = Pt(11)
    d.add_heading(doc["title"], 0)
    d.add_paragraph(f"適用對象：{doc.get('audience', '一般使用者')}　系統網址：{doc.get('url', '')}")
    if doc.get("intro"):
        d.add_paragraph(doc["intro"])
    if doc.get("prerequisites"):
        d.add_heading("開始之前", 1)
        for p in doc["prerequisites"]:
            d.add_paragraph(p, style="List Bullet")
    for i, s in enumerate(doc.get("sections", []), 1):
        d.add_heading(f"{i}. {s['title']}", 1)
        if s.get("description"):
            d.add_paragraph(s["description"])
        for st in s.get("steps", []):
            d.add_heading(f"步驟 {st['number']}：{st['title']}", 2)
            d.add_paragraph(st.get("text", ""))
            if st.get("op"):
                d.add_paragraph(f"操作：{st['op']}").runs[0].italic = True
            if st.get("image"):
                img = out_dir / st["image"]
                if img.exists():
                    d.add_picture(str(img), width=Cm(16))
    if doc.get("tips"):
        d.add_heading("小提醒", 1)
        for t in doc["tips"]:
            d.add_paragraph(t, style="List Bullet")
    p = out_dir / f"{name}.docx"
    d.save(str(p))
    return p
