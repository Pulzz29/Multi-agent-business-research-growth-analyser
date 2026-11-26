"""
Render a saved sector-comparison snapshot as PDF (reportlab) or DOCX
(python-docx). Rendering reads only the snapshot: no data-provider or AI calls.
"""

import io
from datetime import datetime
from xml.sax.saxutils import escape


def fmt_metric(value, unit, currency):
    if value is None:
        return "n/a"
    if unit == "%":
        return f"{value:.1f}%"
    if unit == "x":
        return f"{value:.2f}x"
    if unit == "currency":
        if abs(value) >= 1e12:
            return f"{currency} {value / 1e12:,.2f} tn"
        return f"{currency} {value / 1e9:,.1f} bn"
    return f"{value:g}"


def _date(text):
    from app.services.pdf_common import format_timestamp

    return format_timestamp(text)


def report_model(snapshot):
    """Plain view model shared by the PDF and DOCX renderers."""
    ranking = snapshot["ranking"]
    narrative = snapshot.get("narrative") or {}
    definitions = snapshot["metric_definitions"]
    used = ranking["method"]["metrics_used"]
    companies = {c["symbol"]: c for c in snapshot["companies"]}
    rows = []
    for item in ranking["companies"]:
        company = companies.get(item["symbol"], {})
        text = (narrative.get("companies") or {}).get(item["symbol"]) or {}
        inputs = company.get("inputs") or {}
        rows.append({
            "rank": item["rank"],
            "symbol": item["symbol"],
            "name": item["name"],
            "score": item["score"],
            "ranked": item["ranked"],
            "not_ranked_reason": item["not_ranked_reason"],
            "coverage": item["coverage"],
            "values": {
                m: fmt_metric((company.get("metrics") or {}).get(m), definitions[m].get("unit"),
                              snapshot["currency"])
                for m in used
            },
            "imputed": item.get("imputed_metrics") or [],
            "why": text.get("why_it_ranks") or item.get("explanation"),
            "strengths": text.get("strengths") or [],
            "weaknesses": text.get("weaknesses") or [],
            "text_source": text.get("source", "deterministic"),
            "fiscal_period_end": inputs.get("fiscal_period_end"),
            "statement_provider": inputs.get("statement_provider"),
            "quote_provider": (inputs.get("quote") or {}).get("provider"),
            "quote_fetched_at": (inputs.get("quote") or {}).get("fetched_at"),
        })
    custom = (snapshot.get("comparison_type") or "sector") == "custom"
    selected = snapshot.get("selected_companies") or []
    if custom:
        title = (
            f"Custom comparison: {snapshot['sector_label']}, {len(selected)} companies"
            f" — {snapshot['market_label']}"
        )
    else:
        title = f"{snapshot['sector_label']} sector comparison — {snapshot['market_label']}"
    return {
        "title": title,
        "custom": custom,
        "selected_line": (
            "Selected companies: " + "; ".join(f"{c.get('name') or c['symbol']} ({c['symbol']})" for c in selected)
            if custom else None
        ),
        "relative_note": snapshot.get("relative_note") if custom else None,
        "median_note": (
            "* missing value replaced by the median of the selected companies for scoring." if custom
            else "* missing value replaced by the sector median for scoring."
        ),
        "generated": _date(snapshot["generated_at_utc"]),
        "comparison_id": snapshot["comparison_id"],
        "currency": snapshot["currency"],
        "sources": ", ".join(f"{k}: {v}" for k, v in (snapshot.get("data_sources") or {}).items()),
        "overview": narrative.get("overview"),
        "overview_source": narrative.get("overview_source", "deterministic"),
        "ai_label": narrative.get("label"),
        "narrative_status": narrative.get("status"),
        "status": ranking["status"],
        "metrics": [(m, definitions[m]["label"]) for m in used],
        "weights": ranking["method"]["weights_applied"],
        "rows": rows,
        "methodology": snapshot.get("methodology") or [],
        "disclaimers": snapshot.get("disclaimers") or [],
        "ties": ranking.get("ties") or [],
    }


# -------------------------------------------------------------------- PDF

def render_pdf(snapshot):
    from reportlab.graphics.charts.barcharts import HorizontalBarChart
    from reportlab.graphics.shapes import Drawing
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
    )

    m = report_model(snapshot)
    styles = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=9, leading=12)
    small = ParagraphStyle("small", parent=body, fontSize=7.5, leading=9.5, textColor=colors.HexColor("#56637a"))
    cell = ParagraphStyle("cell", parent=body, fontSize=7.5, leading=9)
    h1, h2 = styles["Title"], styles["Heading2"]

    def p(text, style=body):
        return Paragraph(escape(str(text or "")), style)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=landscape(A4), leftMargin=14 * mm, rightMargin=14 * mm,
        topMargin=12 * mm, bottomMargin=12 * mm, title=m["title"], author="InsightIQ",
    )
    story = [
        p(m["title"], h1),
        p(f"Generated {m['generated']} · Comparison {m['comparison_id']} · Currency {m['currency']}", small),
        p(f"Data sources — {m['sources']}", small),
        Spacer(1, 6),
    ]
    if m["custom"]:
        story += [p(m["selected_line"]), p(m["relative_note"]), Spacer(1, 4)]
    story += [
        p("Overview", h2),
        p(m["overview"]),
        p(m["ai_label"] if m["overview_source"] == "ai" else
          "Deterministic overview generated from the scoring model.", small),
        Spacer(1, 6),
    ]

    ranked = [r for r in m["rows"] if r["ranked"]]
    if ranked:
        story.append(p("Ranking scores", h2))
        drawing = Drawing(720, 26 * len(ranked) + 30)
        chart = HorizontalBarChart()
        chart.x, chart.y = 150, 15
        chart.width, chart.height = 520, 26 * len(ranked)
        ordered = list(reversed(ranked))
        chart.data = [[r["score"] for r in ordered]]
        chart.categoryAxis.categoryNames = [f"{r['rank']}. {r['symbol']}" for r in ordered]
        chart.valueAxis.valueMin, chart.valueAxis.valueMax, chart.valueAxis.valueStep = 0, 100, 20
        chart.bars[0].fillColor = colors.HexColor("#2952e3")
        chart.barLabelFormat = "%.1f"
        chart.barLabels.nudge = 12
        drawing.add(chart)
        story.append(drawing)

    story.append(p("Ranking", h2))
    rank_rows = [["Rank", "Company", "Symbol", "Score", "Data coverage", "Fiscal year end", "Sources"]]
    for r in m["rows"]:
        rank_rows.append([
            str(r["rank"]) if r["ranked"] else "—",
            Paragraph(escape(r["name"] or ""), cell),
            r["symbol"],
            f"{r['score']:.2f}" if r["ranked"] else "not ranked",
            f"{(r['coverage'] or 0) * 100:.0f}%",
            r["fiscal_period_end"] or "n/a",
            Paragraph(escape(f"{r['statement_provider'] or '—'} / {r['quote_provider'] or '—'}"), cell),
        ])
    story.append(_table(rank_rows, Table, TableStyle, colors, [16 * mm, 70 * mm, 24 * mm, 22 * mm, 26 * mm, 28 * mm, 40 * mm]))

    story.append(p("Comparison table", h2))
    header = ["Company"] + [label for _, label in m["metrics"]]
    table_rows = [[Paragraph(escape(h), cell) for h in header]]
    for r in m["rows"]:
        table_rows.append(
            [Paragraph(escape(r["symbol"]), cell)]
            + [Paragraph(escape(r["values"][key] + (" *" if key in r["imputed"] else "")), cell)
               for key, _ in m["metrics"]]
        )
    width = (269 * mm - 26 * mm) / max(1, len(m["metrics"]))
    story.append(_table(table_rows, Table, TableStyle, colors, [26 * mm] + [width] * len(m["metrics"])))
    story.append(p(m["median_note"], small))

    story.append(PageBreak())
    story.append(p("Company assessments", h2))
    for r in m["rows"]:
        block = [
            p(f"{'#' + str(r['rank']) if r['ranked'] else 'Not ranked'} · {r['name']} ({r['symbol']})",
              styles["Heading3"]),
            p(r["why"]),
        ]
        if r["strengths"]:
            block.append(p("Strengths: " + "; ".join(r["strengths"])))
        if r["weaknesses"]:
            block.append(p("Weaknesses: " + "; ".join(r["weaknesses"])))
        block.append(p("AI-written text, validated against the comparison data." if r["text_source"] == "ai"
                       else "Deterministic explanation from the score breakdown.", small))
        story.append(KeepTogether(block))

    story.append(p("Methodology", h2))
    for line in m["methodology"]:
        story.append(p("• " + line))
    story.append(p("Disclaimers", h2))
    for line in m["disclaimers"]:
        story.append(p("• " + line, small))

    from app.services.pdf_common import page_decorator

    footer = page_decorator(f"InsightIQ · {m['title']} · generated {m['generated']}")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()


def _table(rows, Table, TableStyle, colors, widths):
    # Shared with the company research report PDF.
    from app.services.pdf_common import styled_table

    return styled_table(rows, widths)


# ------------------------------------------------------------------- DOCX

def render_docx(snapshot):
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.shared import Pt

    m = report_model(snapshot)
    document = Document()
    section = document.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width, section.page_height = section.page_height, section.page_width
    document.styles["Normal"].font.size = Pt(9.5)

    document.add_heading(m["title"], level=0)
    document.add_paragraph(
        f"Generated {m['generated']} · Comparison {m['comparison_id']} · Currency {m['currency']}")
    document.add_paragraph(f"Data sources — {m['sources']}")
    if m["custom"]:
        document.add_paragraph(m["selected_line"])
        document.add_paragraph(m["relative_note"])

    document.add_heading("Overview", level=1)
    document.add_paragraph(m["overview"] or "")
    document.add_paragraph(m["ai_label"] if m["overview_source"] == "ai"
                           else "Deterministic overview generated from the scoring model.")

    document.add_heading("Ranking", level=1)
    table = document.add_table(rows=1, cols=5)
    table.style = "Light Grid Accent 1"
    for i, h in enumerate(["Rank", "Company", "Symbol", "Score", "Data coverage"]):
        table.rows[0].cells[i].text = h
    for r in m["rows"]:
        cells = table.add_row().cells
        cells[0].text = str(r["rank"]) if r["ranked"] else "—"
        cells[1].text = r["name"] or ""
        cells[2].text = r["symbol"]
        cells[3].text = f"{r['score']:.2f}" if r["ranked"] else "not ranked"
        cells[4].text = f"{(r['coverage'] or 0) * 100:.0f}%"

    document.add_heading("Comparison table", level=1)
    table = document.add_table(rows=1, cols=1 + len(m["metrics"]))
    table.style = "Light Grid Accent 1"
    table.rows[0].cells[0].text = "Company"
    for i, (_, label) in enumerate(m["metrics"], start=1):
        table.rows[0].cells[i].text = label
    for r in m["rows"]:
        cells = table.add_row().cells
        cells[0].text = r["symbol"]
        for i, (key, _) in enumerate(m["metrics"], start=1):
            cells[i].text = r["values"][key] + (" *" if key in r["imputed"] else "")
    document.add_paragraph(m["median_note"] + " The PDF version includes the score chart.")

    document.add_heading("Company assessments", level=1)
    for r in m["rows"]:
        document.add_heading(
            f"{'#' + str(r['rank']) if r['ranked'] else 'Not ranked'} · {r['name']} ({r['symbol']})", level=2)
        document.add_paragraph(r["why"] or "")
        for s in r["strengths"]:
            document.add_paragraph(f"Strength: {s}", style="List Bullet")
        for w in r["weaknesses"]:
            document.add_paragraph(f"Weakness: {w}", style="List Bullet")

    document.add_heading("Methodology", level=1)
    for line in m["methodology"]:
        document.add_paragraph(line, style="List Bullet")
    document.add_heading("Disclaimers", level=1)
    for line in m["disclaimers"]:
        document.add_paragraph(line, style="List Bullet")

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def render(snapshot, fmt):
    if fmt == "pdf":
        return render_pdf(snapshot), "application/pdf", "pdf"
    if fmt == "docx":
        return (render_docx(snapshot),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx")
    raise ValueError("Unsupported report format.")
