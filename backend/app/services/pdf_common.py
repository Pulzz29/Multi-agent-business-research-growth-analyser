"""Shared reportlab helpers for InsightIQ PDF reports (sector and company)."""

BRAND = "#2952e3"
SECONDARY = "#0b8f7f"
ACCENT = "#6d3fd8"
NEGATIVE = "#c0392b"
MUTED = "#56637a"
GRID = "#cfd6e1"
HEADER_FILL = "#e8eeff"
ROW_ALT = "#f5f6f8"


def styled_table(rows, widths):
    """Readable table: shaded header repeated on every page, light grid,
    alternating rows."""
    from reportlab.lib import colors
    from reportlab.platypus import Table, TableStyle

    table = Table(rows, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(HEADER_FILL)),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor(GRID)),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor(ROW_ALT)]),
    ]))
    return table


def format_timestamp(text):
    """The one timestamp format of every InsightIQ PDF: the persisted UTC time
    ('26 Nov 2026 10:00 UTC'). Missing metadata is stated, never invented."""
    from datetime import datetime, timezone

    try:
        value = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return "not recorded"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%d %b %Y %H:%M UTC")


def page_decorator(label):
    """onPage callback drawing a footer with the report label and page number."""
    from reportlab.lib import colors

    def draw(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor(MUTED))
        width, _ = doc.pagesize
        y = doc.bottomMargin / 2
        canvas.drawString(doc.leftMargin, y, label)
        canvas.drawRightString(width - doc.rightMargin, y, f"Page {doc.page}")
        canvas.restoreState()

    return draw
