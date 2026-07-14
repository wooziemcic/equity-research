from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def build_pdf_report(path: Path, sections: list[dict[str, Any]]) -> None:
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="MemoTitle", parent=styles["Heading1"], fontName="Helvetica-Bold", fontSize=15, leading=17, textColor=colors.HexColor("#A10F1B"), spaceAfter=5, alignment=TA_LEFT))
    styles.add(ParagraphStyle(name="MemoHeading", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=10, leading=12, textColor=colors.HexColor("#222222"), spaceBefore=4, spaceAfter=2))
    styles.add(ParagraphStyle(name="MemoBody", parent=styles["BodyText"], fontName="Helvetica", fontSize=9.5, leading=11.5, spaceAfter=3))
    doc = SimpleDocTemplate(str(path), pagesize=LETTER, rightMargin=36, leftMargin=36, topMargin=32, bottomMargin=32)
    story: list[Any] = []
    for section in sections:
        title = section.get("title")
        if title:
            story.append(Paragraph(escape(str(title)), styles["MemoTitle"] if not story else styles["MemoHeading"]))
        for paragraph in section.get("paragraphs", []):
            story.append(Paragraph(escape(str(paragraph)).replace("\n", "<br/>"), styles["MemoBody"]))
        for table in section.get("tables", []):
            rows = table.get("rows", [])
            if not rows:
                continue
            report_table = Table(rows, repeatRows=1)
            report_table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EDF5")),
                        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ]
                )
            )
            story.append(report_table)
            story.append(Spacer(1, 10))
        if section.get("page_break"):
            story.append(PageBreak())
    doc.build(story)
