"""
COP Engine — PDF Export
=======================
Genererar PDF-versioner av veckoscheman.
Använder reportlab för snabb, ren PDF-generering.
"""

import io
import json
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from db import get_db

pdf_router = APIRouter(prefix="/export", tags=["Export"])


def _generate_schedule_pdf(schedule_data: dict) -> bytes:
    """Generera en PDF av ett veckoschema."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer,
        PageBreak
    )

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "CopTitle",
        parent=styles["Heading1"],
        fontSize=18,
        textColor=colors.HexColor("#0f172a"),
        spaceAfter=6,
    )
    subtitle_style = ParagraphStyle(
        "CopSubtitle",
        parent=styles["Normal"],
        fontSize=10,
        textColor=colors.HexColor("#64748b"),
        spaceAfter=12,
    )
    cell_style = ParagraphStyle(
        "CopCell",
        parent=styles["Normal"],
        fontSize=7,
        leading=9,
    )

    elements = []

    # Header
    clinic_id = schedule_data.get("clinic_id", "Okänd klinik")
    schedule_id = schedule_data.get("schedule_id", "")[:12]
    start_date = schedule_data.get("start_date", "")
    num_weeks = schedule_data.get("num_weeks", 1)
    created = schedule_data.get("created_at", "")[:16]

    elements.append(Paragraph(f"COP Veckoschema — {clinic_id.title()}", title_style))
    elements.append(Paragraph(
        f"Schema-ID: {schedule_id} | Start: {start_date} | Veckor: {num_weeks} | Genererat: {created}",
        subtitle_style,
    ))

    # Build the schedule grid
    raw_schedule = schedule_data.get("raw_schedule") or schedule_data.get("schedule", {})

    if not raw_schedule:
        elements.append(Paragraph("Inget schemainnehåll tillgängligt.", styles["Normal"]))
    else:
        # Determine days
        total_days = num_weeks * 7
        try:
            base = datetime.strptime(start_date, "%Y-%m-%d")
        except (ValueError, TypeError):
            base = datetime.now()

        day_headers = []
        for d in range(total_days):
            dt = base + timedelta(days=d)
            day_headers.append(dt.strftime("%a %d/%m"))

        # Build per-week tables
        for week_idx in range(num_weeks):
            week_start = week_idx * 7
            week_days = day_headers[week_start:week_start + 7]

            header_row = ["Läkare"] + week_days
            table_data = [header_row]

            for doctor_id, assignments in raw_schedule.items():
                row = [Paragraph(str(doctor_id)[:20], cell_style)]
                for day_offset in range(7):
                    day_idx = week_start + day_offset
                    func = ""
                    if isinstance(assignments, dict):
                        func = assignments.get(str(day_idx), assignments.get(day_idx, ""))
                    row.append(Paragraph(str(func) if func else "—", cell_style))
                table_data.append(row)

            col_widths = [55 * mm] + [30 * mm] * 7
            table = Table(table_data, colWidths=col_widths, repeatRows=1)

            # Style
            style_commands = [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e293b")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTSIZE", (0, 0), (-1, 0), 8),
                ("FONTSIZE", (0, 1), (-1, -1), 7),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("ALIGN", (0, 0), (0, -1), "LEFT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ]

            # Highlight weekends (Saturday=col 6, Sunday=col 7)
            for row_idx in range(1, len(table_data)):
                style_commands.append(
                    ("BACKGROUND", (6, row_idx), (7, row_idx), colors.HexColor("#fef3c7"))
                )

            table.setStyle(TableStyle(style_commands))

            if week_idx > 0:
                elements.append(Spacer(1, 8 * mm))
            elements.append(Paragraph(f"Vecka {week_idx + 1}", styles["Heading3"]))
            elements.append(Spacer(1, 3 * mm))
            elements.append(table)

    # Statistics section
    stats = schedule_data.get("statistics", {})
    if stats:
        elements.append(Spacer(1, 10 * mm))
        elements.append(Paragraph("Statistik", styles["Heading3"]))
        stat_lines = []
        if "solve_time_ms" in schedule_data:
            stat_lines.append(f"Lösningstid: {schedule_data['solve_time_ms']} ms")
        if "objective_value" in schedule_data:
            stat_lines.append(f"Optimeringsvärde: {schedule_data.get('objective_value', 'N/A')}")
        if stats.get("total_violations") is not None:
            stat_lines.append(f"ATL-brott: {stats['total_violations']}")
        if stat_lines:
            elements.append(Paragraph(" | ".join(stat_lines), subtitle_style))

    # Footer
    elements.append(Spacer(1, 10 * mm))
    elements.append(Paragraph(
        f"Genererat av COP Engine — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        ParagraphStyle("Footer", parent=styles["Normal"], fontSize=7, textColor=colors.HexColor("#94a3b8")),
    ))

    doc.build(elements)
    return buffer.getvalue()


@pdf_router.get("/schedule/{schedule_id}/pdf")
async def export_schedule_pdf(schedule_id: str):
    """Exportera ett schema som PDF."""
    db = get_db()
    schedule = await db.get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schema hittades inte")

    pdf_bytes = _generate_schedule_pdf(schedule)

    filename = f"cop_schema_{schedule_id[:12]}_{datetime.now().strftime('%Y%m%d')}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@pdf_router.get("/schedules/latest/pdf")
async def export_latest_schedule_pdf(clinic_id: Optional[str] = None):
    """Exportera senaste schemat som PDF."""
    db = get_db()
    schedules = await db.list_schedules(clinic_id=clinic_id, limit=1)
    if not schedules:
        raise HTTPException(status_code=404, detail="Inga scheman hittade")

    # Get full schedule with raw data
    full = await db.get_schedule(schedules[0]["schedule_id"])
    if not full:
        raise HTTPException(status_code=404, detail="Schema hittades inte")

    pdf_bytes = _generate_schedule_pdf(full)
    filename = f"cop_schema_senaste_{datetime.now().strftime('%Y%m%d')}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
