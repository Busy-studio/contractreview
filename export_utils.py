from __future__ import annotations

import html
import io
import re
from datetime import datetime
from typing import Dict, List

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt, RGBColor
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


NAVY = "003B70"
TEXT = "1D2939"
MUTED = "667085"
BORDER = "D0D5DD"

CLASS_STYLES = {
    "legal": {
        "label": "법적 위험",
        "bg": "FEE4E2",
        "fg": "B42318",
        "icon": "●",
    },
    "precedent": {
        "label": "판례상·분쟁 위험",
        "bg": "FEF0C7",
        "fg": "B54708",
        "icon": "●",
    },
    "internal": {
        "label": "내부규정 검토",
        "bg": "EAF2FF",
        "fg": "175CD3",
        "icon": "●",
    },
    "negotiation": {
        "label": "협상 권고",
        "bg": "FFF7D6",
        "fg": "8A6D00",
        "icon": "●",
    },
}

SECTION_STYLES = {
    "original": {"label": "검토 대상 원문", "bg": "F2F4F7", "fg": "344054"},
    "reason": {"label": "문제되는 이유", "bg": "FFF1F0", "fg": "912018"},
    "basis": {"label": "근거", "bg": "EFF8FF", "fg": "175CD3"},
    "revision": {"label": "조항 변경 예시", "bg": "ECFDF3", "fg": "027A48"},
}

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\([^\)]+\)")


def _strip_inline_markup(text: str) -> str:
    text = str(text or "")
    text = re.sub(r"<chg>(.*?)</chg>", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"<span[^>]*>(.*?)</span>", r"\1", text, flags=re.DOTALL | re.IGNORECASE)
    text = _HTML_TAG_RE.sub("", text)
    text = _MARKDOWN_LINK_RE.sub(r"\1", text)
    text = text.replace("**", "").replace("__", "").replace(chr(96), "")
    for icon in ("📌", "📄", "⚠️", "⚠", "⚖️", "⚖", "✏️", "✏", "🔎", "ℹ️", "ℹ", "🔴", "🟠", "🔵", "🟡"):
        text = text.replace(icon, "")
    text = html.unescape(text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _classification_key(text: str) -> str:
    plain = _strip_inline_markup(text)
    if "법적 위험" in plain:
        return "legal"
    if "판례상" in plain or "분쟁 위험" in plain:
        return "precedent"
    if "내부규정" in plain:
        return "internal"
    return "negotiation"


def _section_key(text: str) -> str | None:
    plain = _strip_inline_markup(text)
    if "원문" in plain:
        return "original"
    if "문제되는 이유" in plain or "검토 의견" in plain:
        return "reason"
    if "근거" in plain:
        return "basis"
    if "변경 예시" in plain or "수정" in plain:
        return "revision"
    return None


def parse_review_report(report_text: str) -> Dict:
    report = {
        "summary": [],
        "items": [],
        "notice": "본 결과는 내부 검토 참고용이며 최종 법률자문을 대체하지 않습니다.",
    }
    mode = None
    current_item = None
    current_section = None

    for raw in str(report_text or "").splitlines():
        stripped = raw.strip()
        if not stripped or stripped in {"---", "***", "___"}:
            continue

        if stripped.startswith("## "):
            title = _strip_inline_markup(stripped[3:])
            if "검토 요약" in title:
                mode = "summary"
                current_item = None
                current_section = None
            elif "주요 검토조항" in title or "독소조항 검토" in title or "상세 검토" in title:
                mode = "items"
                current_item = None
                current_section = None
            elif "주의" in title:
                mode = "notice"
                current_item = None
                current_section = None
            continue

        if mode == "summary" and stripped.startswith("- "):
            text = _strip_inline_markup(stripped[2:])
            if ":" in text:
                label, value = text.split(":", 1)
                report["summary"].append((label.strip(), value.strip()))
            else:
                report["summary"].append(("", text))
            continue

        if mode == "items" and stripped.startswith("### "):
            header = _strip_inline_markup(stripped[4:])
            classification = _classification_key(header)
            title = header.split("|", 1)[0].strip()
            current_item = {
                "title": title,
                "classification": classification,
                "sections": {"original": [], "reason": [], "basis": [], "revision": []},
            }
            report["items"].append(current_item)
            current_section = None
            continue

        if mode == "items" and stripped.startswith("#### "):
            current_section = _section_key(stripped[5:])
            continue

        if mode == "items" and current_item is not None:
            text = stripped[2:] if stripped.startswith("- ") else stripped
            text = _strip_inline_markup(text)
            if current_section and text:
                current_item["sections"][current_section].append(text)
            continue

        if mode == "notice":
            text = _strip_inline_markup(stripped.lstrip("- ").strip())
            if text and "내부 검토 참고용" in text:
                report["notice"] = text

    return report


def render_review_html(report_text: str) -> str:
    data = parse_review_report(report_text)

    def esc(value: str) -> str:
        return html.escape(str(value or ""), quote=True)

    summary_html = ""
    for label, value in data["summary"]:
        summary_html += (
            '<div class="cr-summary-item">'
            f'<div class="cr-summary-label">{esc(label)}</div>'
            f'<div class="cr-summary-value">{esc(value)}</div>'
            "</div>"
        )

    items_html = ""
    for item in data["items"]:
        cls = CLASS_STYLES[item["classification"]]
        sections_html = ""
        for key in ("original", "reason", "basis", "revision"):
            lines = item["sections"].get(key, [])
            if not lines:
                continue
            sec = SECTION_STYLES[key]
            bullets = "".join(f"<li>{esc(line)}</li>" for line in lines)
            sections_html += (
                f'<div class="cr-section cr-{key}">'
                f'<div class="cr-section-title">{esc(sec["label"])}</div>'
                f"<ul>{bullets}</ul>"
                "</div>"
            )

        items_html += (
            '<div class="cr-card">'
            '<div class="cr-card-head">'
            f'<div class="cr-card-title">{esc(item["title"])}</div>'
            f'<span class="cr-badge cr-{item["classification"]}">{esc(cls["label"])}</span>'
            "</div>"
            f"{sections_html}"
            "</div>"
        )

    return f"""
<style>
.cr-report {{
  font-family: "Pretendard", "Noto Sans KR", "Apple SD Gothic Neo", "Malgun Gothic", sans-serif;
  color: #1D2939;
}}
.cr-hero {{
  background: linear-gradient(135deg, #003B70 0%, #0B5A94 100%);
  color: white;
  border-radius: 16px;
  padding: 22px 24px;
  margin: 4px 0 18px 0;
}}
.cr-hero-title {{ font-size: 23px; font-weight: 800; margin: 0 0 4px 0; }}
.cr-hero-sub {{ opacity: .86; font-size: 13px; }}
.cr-summary {{
  display: grid;
  grid-template-columns: repeat(2, minmax(0,1fr));
  gap: 10px;
  margin-bottom: 22px;
}}
.cr-summary-item {{
  background: #F8FAFC;
  border: 1px solid #E4E7EC;
  border-radius: 12px;
  padding: 12px 14px;
}}
.cr-summary-label {{ color: #667085; font-size: 12px; font-weight: 700; margin-bottom: 4px; }}
.cr-summary-value {{ color: #101828; font-size: 14px; line-height: 1.55; font-weight: 600; }}
.cr-card {{
  background: white;
  border: 1px solid #D0D5DD;
  border-radius: 14px;
  overflow: hidden;
  margin: 0 0 18px 0;
  box-shadow: 0 2px 8px rgba(16,24,40,.06);
}}
.cr-card-head {{
  display:flex; align-items:center; justify-content:space-between; gap:12px;
  padding: 14px 16px; background:#F8FAFC; border-bottom:1px solid #E4E7EC;
}}
.cr-card-title {{ font-size: 16px; font-weight: 800; color:#003B70; }}
.cr-badge {{ white-space:nowrap; border-radius:999px; padding:5px 10px; font-size:12px; font-weight:800; }}
.cr-legal {{ background:#FEE4E2; color:#B42318; }}
.cr-precedent {{ background:#FEF0C7; color:#B54708; }}
.cr-internal {{ background:#EAF2FF; color:#175CD3; }}
.cr-negotiation {{ background:#FFF7D6; color:#8A6D00; }}
.cr-section {{ margin: 12px 14px; padding: 12px 14px; border-radius: 10px; border-left: 4px solid; }}
.cr-section-title {{ font-size: 13px; font-weight: 800; margin-bottom: 7px; }}
.cr-section ul {{ margin: 0; padding-left: 20px; }}
.cr-section li {{ margin: 4px 0; line-height: 1.55; font-size: 13.5px; }}
.cr-original {{ background:#F2F4F7; border-color:#98A2B3; }}
.cr-reason {{ background:#FFF1F0; border-color:#F97066; }}
.cr-basis {{ background:#EFF8FF; border-color:#53B1FD; }}
.cr-revision {{ background:#ECFDF3; border-color:#32D583; }}
.cr-notice {{
  margin-top: 16px; padding: 11px 13px; border-radius: 10px;
  background:#F9FAFB; border:1px solid #EAECF0; color:#667085; font-size:12px;
}}
@media (max-width: 720px) {{ .cr-summary {{ grid-template-columns:1fr; }} .cr-card-head {{ align-items:flex-start; flex-direction:column; }} }}
</style>
<div class="cr-report">
  <div class="cr-hero">
    <div class="cr-hero-title">계약 검토 결과</div>
    <div class="cr-hero-sub">주요 검토조항과 수정 권고안을 중심으로 정리했습니다.</div>
  </div>
  <div class="cr-summary">{summary_html}</div>
  {items_html}
  <div class="cr-notice">※ {esc(data["notice"])}</div>
</div>
"""


def _set_docx_font(style, name: str, size_pt: float, bold: bool | None = None) -> None:
    style.font.name = name
    style.font.size = Pt(size_pt)
    if bold is not None:
        style.font.bold = bold
    style._element.rPr.rFonts.set(qn("w:eastAsia"), name)


def _set_cell_shading(cell, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = tc_pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        tc_pr.append(shd)
    shd.set(qn("w:fill"), fill)


def _set_cell_margins(cell, top=120, start=140, bottom=120, end=140) -> None:
    tc = cell._tc
    tc_pr = tc.get_or_add_tcPr()
    tc_mar = tc_pr.first_child_found_in("w:tcMar")
    if tc_mar is None:
        tc_mar = OxmlElement("w:tcMar")
        tc_pr.append(tc_mar)
    for m, v in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        node = tc_mar.find(qn(f"w:{m}"))
        if node is None:
            node = OxmlElement(f"w:{m}")
            tc_mar.append(node)
        node.set(qn("w:w"), str(v))
        node.set(qn("w:type"), "dxa")


def _set_run(run, size=10.0, bold=False, color=TEXT):
    run.font.name = "맑은 고딕"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), "맑은 고딕")
    run.font.size = Pt(size)
    run.bold = bold
    run.font.color.rgb = RGBColor.from_string(color)


def _add_docx_section(doc: Document, key: str, lines: List[str]) -> None:
    if not lines:
        return
    style = SECTION_STYLES[key]
    table = doc.add_table(rows=1 + len(lines), cols=1)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = True

    head = table.cell(0, 0)
    _set_cell_shading(head, style["bg"])
    _set_cell_margins(head, top=90, bottom=60)
    p = head.paragraphs[0]
    p.paragraph_format.space_after = Pt(0)
    r = p.add_run(style["label"])
    _set_run(r, size=9.5, bold=True, color=style["fg"])

    for idx, line in enumerate(lines, start=1):
        cell = table.cell(idx, 0)
        _set_cell_shading(cell, style["bg"])
        _set_cell_margins(cell, top=30, bottom=65)
        p = cell.paragraphs[0]
        p.paragraph_format.space_after = Pt(0)
        p.paragraph_format.left_indent = Mm(1.5)
        r = p.add_run("• " + line)
        _set_run(r, size=9.5, bold=False, color=TEXT)

    doc.add_paragraph("").paragraph_format.space_after = Pt(1)


def build_docx_bytes(report_text: str, title: str = "계약 검토 결과") -> bytes:
    data = parse_review_report(report_text)
    doc = Document()
    section = doc.sections[0]
    section.top_margin = Mm(16)
    section.bottom_margin = Mm(16)
    section.left_margin = Mm(18)
    section.right_margin = Mm(18)

    styles = doc.styles
    _set_docx_font(styles["Normal"], "맑은 고딕", 9.5, False)
    _set_docx_font(styles["Title"], "맑은 고딕", 18, True)

    hero = doc.add_table(rows=1, cols=1)
    hero.alignment = WD_TABLE_ALIGNMENT.CENTER
    hero_cell = hero.cell(0, 0)
    _set_cell_shading(hero_cell, NAVY)
    _set_cell_margins(hero_cell, top=240, bottom=220, start=220, end=220)
    p = hero_cell.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    p.paragraph_format.space_after = Pt(3)
    r = p.add_run(title)
    _set_run(r, size=17, bold=True, color="FFFFFF")
    p2 = hero_cell.add_paragraph()
    p2.paragraph_format.space_after = Pt(0)
    r2 = p2.add_run(f"계약 검토 보고서  ·  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    _set_run(r2, size=8.5, bold=False, color="E6EEF5")

    doc.add_paragraph("").paragraph_format.space_after = Pt(1)

    if data["summary"]:
        table = doc.add_table(rows=len(data["summary"]), cols=2)
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        table.autofit = True
        for row_idx, (label, value) in enumerate(data["summary"]):
            left, right = table.rows[row_idx].cells
            _set_cell_shading(left, "F2F4F7")
            _set_cell_shading(right, "FFFFFF")
            _set_cell_margins(left)
            _set_cell_margins(right)
            left.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            right.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            lp = left.paragraphs[0]
            rp = right.paragraphs[0]
            lp.paragraph_format.space_after = Pt(0)
            rp.paragraph_format.space_after = Pt(0)
            _set_run(lp.add_run(label or "요약"), size=9, bold=True, color=MUTED)
            _set_run(rp.add_run(value), size=9.5, bold=True if row_idx < 3 else False, color=TEXT)

    doc.add_paragraph("").paragraph_format.space_after = Pt(3)

    for item in data["items"]:
        cls = CLASS_STYLES[item["classification"]]
        head = doc.add_table(rows=1, cols=2)
        head.alignment = WD_TABLE_ALIGNMENT.CENTER
        head.columns[0].width = Mm(135)
        head.columns[1].width = Mm(35)

        title_cell, badge_cell = head.rows[0].cells
        _set_cell_shading(title_cell, "F8FAFC")
        _set_cell_shading(badge_cell, cls["bg"])
        _set_cell_margins(title_cell, top=130, bottom=130)
        _set_cell_margins(badge_cell, top=130, bottom=130)
        title_cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
        badge_cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER

        p = title_cell.paragraphs[0]
        p.paragraph_format.space_after = Pt(0)
        _set_run(p.add_run(item["title"]), size=11, bold=True, color=NAVY)

        bp = badge_cell.paragraphs[0]
        bp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        bp.paragraph_format.space_after = Pt(0)
        _set_run(bp.add_run(cls["label"]), size=8.5, bold=True, color=cls["fg"])

        for key in ("original", "reason", "basis", "revision"):
            _add_docx_section(doc, key, item["sections"].get(key, []))

        doc.add_paragraph("").paragraph_format.space_after = Pt(4)

    notice = doc.add_table(rows=1, cols=1)
    ncell = notice.cell(0, 0)
    _set_cell_shading(ncell, "F9FAFB")
    _set_cell_margins(ncell)
    np = ncell.paragraphs[0]
    np.paragraph_format.space_after = Pt(0)
    _set_run(np.add_run("※ " + data["notice"]), size=8.3, bold=False, color=MUTED)

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def _register_pdf_fonts() -> tuple[str, str]:
    body_font = "HYSMyeongJo-Medium"
    try:
        pdfmetrics.getFont(body_font)
    except KeyError:
        pdfmetrics.registerFont(UnicodeCIDFont(body_font))
    return body_font, body_font


def _pdf_safe(text: str) -> str:
    return html.escape(_strip_inline_markup(text), quote=False)


def _pdf_section_table(key: str, lines: List[str], body_font: str, width: float):
    if not lines:
        return None
    style = SECTION_STYLES[key]
    label_style = ParagraphStyle(
        f"{key}Label",
        fontName=body_font,
        fontSize=9.3,
        leading=13,
        textColor=colors.HexColor("#" + style["fg"]),
        spaceAfter=0,
    )
    body_style = ParagraphStyle(
        f"{key}Body",
        fontName=body_font,
        fontSize=8.8,
        leading=13.5,
        textColor=colors.HexColor("#" + TEXT),
        spaceAfter=0,
    )
    rows = [[Paragraph(_pdf_safe(style["label"]), label_style)]]
    for line in lines:
        rows.append([Paragraph("- " + _pdf_safe(line), body_style)])
    table = Table(rows, colWidths=[width], repeatRows=1, splitByRow=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#" + style["bg"])),
                ("BOX", (0, 0), (-1, -1), 0.35, colors.HexColor("#D0D5DD")),
                ("INNERGRID", (0, 0), (-1, -1), 0, colors.white),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
                ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return table


def build_pdf_bytes(report_text: str, title: str = "계약 검토 결과") -> bytes:
    data = parse_review_report(report_text)
    body_font, heading_font = _register_pdf_fonts()
    out = io.BytesIO()

    doc = SimpleDocTemplate(
        out,
        pagesize=A4,
        rightMargin=16 * mm,
        leftMargin=16 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title=title,
        author="PNU 계약/협약서 검토",
    )
    usable_width = A4[0] - 32 * mm

    title_style = ParagraphStyle(
        "KTitle",
        fontName=heading_font,
        fontSize=16,
        leading=21,
        textColor=colors.white,
        alignment=TA_LEFT,
    )
    meta_style = ParagraphStyle(
        "KMetaWhite",
        fontName=body_font,
        fontSize=7.8,
        leading=11,
        textColor=colors.HexColor("#E6EEF5"),
    )
    body_style = ParagraphStyle(
        "KBody",
        fontName=body_font,
        fontSize=8.8,
        leading=13.5,
        textColor=colors.HexColor("#" + TEXT),
    )
    label_style = ParagraphStyle(
        "KLabel",
        fontName=body_font,
        fontSize=8.2,
        leading=12,
        textColor=colors.HexColor("#" + MUTED),
    )
    item_style = ParagraphStyle(
        "KItemTitle",
        fontName=heading_font,
        fontSize=10.5,
        leading=15,
        textColor=colors.HexColor("#" + NAVY),
    )
    badge_style = ParagraphStyle(
        "KBadge",
        fontName=body_font,
        fontSize=8,
        leading=11,
        alignment=1,
    )

    hero = Table(
        [[
            [
                Paragraph(_pdf_safe(title), title_style),
                Spacer(1, 2 * mm),
                Paragraph(
                    _pdf_safe(f"계약 검토 보고서  ·  {datetime.now().strftime('%Y-%m-%d %H:%M')}"),
                    meta_style,
                ),
            ]
        ]],
        colWidths=[usable_width],
    )
    hero.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#" + NAVY)),
                ("LEFTPADDING", (0, 0), (-1, -1), 13),
                ("RIGHTPADDING", (0, 0), (-1, -1), 13),
                ("TOPPADDING", (0, 0), (-1, -1), 12),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
            ]
        )
    )

    story = [hero, Spacer(1, 5 * mm)]

    if data["summary"]:
        rows = []
        for label, value in data["summary"]:
            rows.append([
                Paragraph(_pdf_safe(label or "요약"), label_style),
                Paragraph(_pdf_safe(value), body_style),
            ])
        summary = Table(rows, colWidths=[34 * mm, usable_width - 34 * mm], splitByRow=1)
        summary.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F2F4F7")),
                    ("BACKGROUND", (1, 0), (1, -1), colors.white),
                    ("BOX", (0, 0), (-1, -1), 0.35, colors.HexColor("#D0D5DD")),
                    ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#EAECF0")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 7),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        story += [summary, Spacer(1, 6 * mm)]

    for item in data["items"]:
        cls = CLASS_STYLES[item["classification"]]
        badge = Paragraph(
            f'<font color="#{cls["fg"]}">{_pdf_safe(cls["label"])}</font>',
            badge_style,
        )
        head = Table(
            [[Paragraph(_pdf_safe(item["title"]), item_style), badge]],
            colWidths=[usable_width - 38 * mm, 38 * mm],
        )
        head.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#F8FAFC")),
                    ("BACKGROUND", (1, 0), (1, 0), colors.HexColor("#" + cls["bg"])),
                    ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#D0D5DD")),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 7),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ]
            )
        )
        story.append(head)
        story.append(Spacer(1, 2.2 * mm))

        for key in ("original", "reason", "basis", "revision"):
            sec = _pdf_section_table(key, item["sections"].get(key, []), body_font, usable_width)
            if sec is not None:
                story.append(sec)
                story.append(Spacer(1, 2 * mm))
        story.append(Spacer(1, 3.5 * mm))

    notice_style = ParagraphStyle(
        "KNotice",
        fontName=body_font,
        fontSize=7.8,
        leading=11,
        textColor=colors.HexColor("#" + MUTED),
    )
    notice = Table(
        [[Paragraph(_pdf_safe("※ " + data["notice"]), notice_style)]],
        colWidths=[usable_width],
    )
    notice.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F9FAFB")),
                ("BOX", (0, 0), (-1, -1), 0.35, colors.HexColor("#EAECF0")),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    story.append(notice)

    doc.build(story)
    return out.getvalue()
