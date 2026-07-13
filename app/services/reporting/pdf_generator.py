from __future__ import annotations

from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def build_pdf_report(path: Path, sections: list[dict[str, Any]]) -> None:
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(str(path), pagesize=LETTER, rightMargin=54, leftMargin=54, topMargin=54, bottomMargin=54)
    story: list[Any] = []
    for section in sections:
        title = section.get("title")
        if title:
            story.append(Paragraph(str(title), styles["Heading1"]))
            story.append(Spacer(1, 8))
        for paragraph in section.get("paragraphs", []):
            story.append(Paragraph(str(paragraph), styles["BodyText"]))
            story.append(Spacer(1, 6))
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
