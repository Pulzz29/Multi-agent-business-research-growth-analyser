"""
Company research report as PDF.

Built from three saved inputs only: the research report, its frozen insights
dataset (the same numbers the web charts show) and the AI brief sidecar when
one exists. Charts are drawn server-side as reportlab vector graphics, so the
output does not depend on a browser session and is identical every time.
No data-provider or AI calls are made.
"""

import io
import re
from datetime import datetime
from xml.sax.saxutils import escape

from app.services import pdf_common
from app.services.executive_summary import RESEARCH_SECTION_TITLES

SUMMARY_TITLES = {
    "financial_position": "Financial position",
    "news_developments": "News developments",
    "risks_and_opportunities": "Risks and opportunities",
    "monitoring_priorities": "Monitoring priorities",
    "evidence_limitations": "Evidence limitations",
}
DISCLAIMER = (
    "Research support only; not investment advice. Figures are computed by InsightIQ from SEC "
    "filings or stored provider statements as of the data date shown. AI-generated text was "
    "checked against the report's evidence (figures, evidence IDs, language) but is not "
    "independently verified analysis. News items are publisher reports, not verified facts."
)


def _when(text):
    """A persisted timestamp as '26 Nov 2026 10:00 UTC'; never the current time."""
    from app.services.pdf_common import format_timestamp

    return format_timestamp(text)


def safe_filename(company, generated_at):
    name = re.sub(r"[^A-Za-z0-9]+", "_", str(company or "Company")).strip("_")[:60] or "Company"
    day = str(generated_at or "")[:10] or "undated"  # no invented date for reports without a timestamp
    return f"{name}_Research_Report_{day}.pdf"


def _quarter(iso):
    y, m = str(iso)[:4], int(str(iso)[5:7])
    return f"Q{(m - 1) // 3 + 1} {y[2:]}"


def _year(iso):
    return f"FY {str(iso)[:4]}"


def _pick_basis(table):
    if len(table.get("quarterly") or []) >= 2:
        return "quarterly"
    if len(table.get("annual") or []) >= 2:
        return "annual"
    return None


# ------------------------------------------------------------------ charts

def _chart_frame(title, width, height):
    from reportlab.graphics.shapes import Drawing, String
    from reportlab.lib import colors

    d = Drawing(width, height)
    d.add(String(0, height - 12, title, fontName="Helvetica-Bold", fontSize=10,
                 fillColor=colors.HexColor("#1b2333")))
    return d


def _legend(d, items, y):
    from reportlab.graphics.shapes import Rect, String
    from reportlab.lib import colors

    x = 0
    for label, color in items:
        d.add(Rect(x, y, 10, 6, fillColor=colors.HexColor(color), strokeColor=None))
        d.add(String(x + 14, y, label, fontName="Helvetica", fontSize=7.5))
        x += 14 + 5.2 * len(label) + 14


def _axis_title(d, text, x, y):
    from reportlab.graphics.shapes import String
    from reportlab.lib import colors

    d.add(String(x, y, text, fontName="Helvetica", fontSize=7.5, fillColor=colors.HexColor(pdf_common.MUTED)))


def bar_chart(title, categories, series, y_title, width=480, height=210):
    """series: [(label, color, [values or None])]."""
    from reportlab.graphics.charts.barcharts import VerticalBarChart
    from reportlab.lib import colors

    d = _chart_frame(title, width, height)
    _legend(d, [(label, color) for label, color, _ in series], height - 28)
    _axis_title(d, y_title, 0, height - 42)
    chart = VerticalBarChart()
    chart.x, chart.y = 46, 30
    chart.width, chart.height = width - 60, height - 84
    # None leaves a gap (reportlab draws no bar); nothing is shown as zero.
    chart.data = [list(values) for _, _, values in series]
    values = [v for _, _, vals in series for v in vals if v is not None]
    low, high = min([0] + values), max([0] + values)
    pad = (high - low) * 0.1 or 1
    chart.valueAxis.valueMin = low - (pad if low < 0 else 0)
    chart.valueAxis.valueMax = high + pad
    chart.valueAxis.labels.fontSize = 7
    chart.categoryAxis.categoryNames = categories
    chart.categoryAxis.labels.fontSize = 7
    chart.categoryAxis.labels.angle = 30 if len(categories) > 8 else 0
    chart.categoryAxis.labels.dy = -6 if len(categories) > 8 else -2
    chart.categoryAxis.labelAxisMode = "low"  # labels below the plot, clear of negative bars
    chart.barSpacing = 1
    chart.groupSpacing = 6
    for i, (_, color, _) in enumerate(series):
        chart.bars[i].fillColor = colors.HexColor(color)
        chart.bars[i].strokeColor = None
    d.add(chart)
    return d


def line_chart(title, categories, series, y_title, width=480, height=200):
    from reportlab.graphics.charts.linecharts import HorizontalLineChart
    from reportlab.graphics.widgets.markers import makeMarker
    from reportlab.lib import colors

    d = _chart_frame(title, width, height)
    _legend(d, [(label, color) for label, color, _ in series], height - 28)
    _axis_title(d, y_title, 0, height - 42)
    chart = HorizontalLineChart()
    chart.x, chart.y = 46, 30
    chart.width, chart.height = width - 60, height - 84
    chart.data = [[(v if v is not None else None) for v in values] for _, _, values in series]
    values = [v for _, _, vals in series for v in vals if v is not None]
    chart.valueAxis.valueMin = min([0] + values)
    chart.valueAxis.valueMax = max(values + [1]) * 1.1
    chart.valueAxis.labels.fontSize = 7
    chart.categoryAxis.categoryNames = categories
    chart.categoryAxis.labels.fontSize = 7
    chart.categoryAxis.labels.angle = 30 if len(categories) > 8 else 0
    chart.categoryAxis.labels.dy = -6 if len(categories) > 8 else -2
    for i, (_, color, _) in enumerate(series):
        chart.lines[i].strokeColor = colors.HexColor(color)
        chart.lines[i].strokeWidth = 1.6
        chart.lines[i].symbol = makeMarker("Circle", size=2.5)
    d.add(chart)
    return d


def chart_specs(insights):
    """(title, drawing-or-None, caption) for each chart, from the frozen dataset."""
    currency = insights.get("currency") or "USD"
    out = []

    price = insights.get("price") or {}
    quote = price.get("latest_quote") or {}
    caption = price.get("reason") or "Price history is not stored."
    if quote.get("price") is not None:
        caption += f" Latest stored quote: {currency} {quote['price']} ({quote.get('provider')}, {_when(quote.get('fetched_at'))})."
    out.append(("Price history", None, caption))

    fin = insights.get("revenue_net_income") or {}
    basis = _pick_basis(fin)
    if basis:
        rows = fin[basis]
        label = _quarter if basis == "quarterly" else _year
        out.append(("Revenue and net income", bar_chart(
            f"Revenue and net income ({basis})", [label(r["period_end"]) for r in rows],
            [("Revenue", pdf_common.BRAND, [None if r.get("revenue") is None else r["revenue"] / 1e9 for r in rows]),
             ("Net income", pdf_common.SECONDARY, [None if r.get("net_income") is None else r["net_income"] / 1e9 for r in rows])],
            f"{currency} billions"), f"{basis.capitalize()} periods; values as reported, in {currency} billions."))
    else:
        out.append(("Revenue and net income", None, "Not enough data to chart (need at least 2 periods)."))

    margins = insights.get("margins") or {}
    mbasis = _pick_basis(margins)
    if mbasis:
        rows = margins[mbasis]
        label = _quarter if mbasis == "quarterly" else _year
        series = []
        for key, name, color in (("gross_margin_pct", "Gross margin", pdf_common.ACCENT),
                                 ("operating_margin_pct", "Operating margin", pdf_common.SECONDARY),
                                 ("net_margin_pct", "Net margin", pdf_common.BRAND)):
            values = [r.get(key) for r in rows]
            if sum(v is not None for v in values) >= 2:
                series.append((name, color, values))
        if series:
            note = "" if margins.get("gross_margin_available") else " " + (margins.get("gross_margin_reason") or "")
            out.append(("Margin trends", line_chart(
                f"Margin trends ({mbasis})", [label(r["period_end"]) for r in rows], series, "% of revenue"),
                f"Margins as a percentage of revenue.{note}"))
        else:
            out.append(("Margin trends", None, "Not enough data to chart."))
    else:
        out.append(("Margin trends", None, "Not enough data to chart (need at least 2 periods)."))

    growth = insights.get("growth") or {}
    yoy = (growth.get("yoy_series") or {})
    rev = yoy.get("revenue") or {}
    ni = yoy.get("net_income") or {}
    gbasis = "quarterly" if len([p for p in rev.get("quarterly") or [] if p["status"] == "ok"]) >= 2 else (
        "annual" if len([p for p in rev.get("annual") or [] if p["status"] == "ok"]) >= 2 else None)
    if gbasis:
        points = [p for p in rev[gbasis] if p["status"] == "ok"][-8:]
        ni_by = {p["period_end"]: p for p in ni.get(gbasis) or [] if p["status"] == "ok"}
        label = _quarter if gbasis == "quarterly" else _year
        out.append(("Growth rates", bar_chart(
            f"Year-over-year growth ({gbasis})", [label(p["period_end"]) for p in points],
            [("Revenue YoY", pdf_common.BRAND, [p["value_pct"] for p in points]),
             ("Net profit YoY", pdf_common.SECONDARY, [(ni_by.get(p["period_end"]) or {}).get("value_pct") for p in points])],
            "YoY growth, %"), "A missing bar means growth was not meaningful (zero or negative base) or not available."))
    else:
        reason = ((growth.get("metrics") or {}).get("revenue_yoy") or {}).get("reason") or "No YoY growth can be computed."
        out.append(("Growth rates", None, f"Not enough data to chart. {reason}"))
    return out


# ------------------------------------------------------------------ render

def render_pdf(report, insights, brief, company_name):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer

    styles = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=9, leading=12.5)
    small = ParagraphStyle("small", parent=body, fontSize=7.5, leading=9.5, textColor=colors.HexColor(pdf_common.MUTED))
    cell = ParagraphStyle("cell", parent=body, fontSize=7.5, leading=9)
    h1 = styles["Title"]
    # Headings stay on the same page as the content that follows them.
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], keepWithNext=1)
    h3 = ParagraphStyle("h3", parent=styles["Heading3"], keepWithNext=1)

    def p(text, style=body):
        return Paragraph(escape(str(text or "")), style)

    ticker = report["ticker"]
    currency = insights.get("currency") or report.get("currency") or "USD"
    title = f"{company_name} ({ticker}) — Research report"
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm,
                            topMargin=14 * mm, bottomMargin=16 * mm, title=title, author="InsightIQ")
    story = [
        p(title, h1),
        p(f"Report generated {_when(report.get('generated_at_utc'))} · Data as of {_when(insights.get('data_as_of'))} · "
          f"Run {report['run_id'][:8]} · Values in {currency} · Source: {insights.get('data_source') or 'stored data'}", small),
        Spacer(1, 8),
    ]

    # AI summary and thorough research.
    completed = brief if brief and brief.get("status") == "completed" else None
    story.append(p("AI summary", h2))
    if completed:
        story.append(p(f"Generated on {_when(completed.get('generated_at_utc'))} · based on data as of "
                       f"{_when(completed.get('data_as_of') or insights.get('data_as_of'))} · AI-generated, "
                       "not investment advice.", small))
        for s in completed.get("sections") or []:
            story.append(KeepTogether([p(SUMMARY_TITLES.get(s["section"], s["section"]), h3), p(s["text"])]))
        research = completed.get("research") or {}
        story.append(p("Thorough research", h2))
        if research.get("sections"):
            for s in research["sections"]:
                story.append(KeepTogether([p(s.get("title") or RESEARCH_SECTION_TITLES.get(s["section"]), h3), p(s["text"])]))
            for w in research.get("withheld") or []:
                story.append(p(f"Withheld by validation — {w.get('title') or w['section']}: {w['reason']}", small))
        else:
            story.append(p("Thorough research has not been generated for this report yet.", small))
    else:
        story.append(p("AI summary not yet generated. The charts, metrics and sources below are complete "
                       "without it; download again once it has been generated.", small))

    # Charts.
    story.append(p("Charts", h2))
    for heading, drawing, caption in chart_specs(insights):
        if drawing is None:
            story.append(KeepTogether([p(heading, h3), p(caption, small)]))
        else:
            story.append(KeepTogether([drawing, p(caption, small), Spacer(1, 8)]))

    # Key metrics and growth table.
    growth = insights.get("growth") or {}
    story.append(p("Key metrics and growth", h2))
    if growth.get("readout"):
        story.append(p(growth["readout"], ParagraphStyle("readout", parent=body, keepWithNext=1)))
    trend = growth.get("trend") or {}
    if trend.get("label"):
        story.append(p(f"Trend: {trend['label'].replace('_', ' ')}. {trend.get('rule') or ''}", small))
    rows = [[Paragraph("<b>Metric</b>", cell), Paragraph("<b>Value</b>", cell),
             Paragraph("<b>Periods compared</b>", cell), Paragraph("<b>Note</b>", cell)]]
    for key in ("revenue_qoq", "revenue_yoy", "net_income_yoy", "eps_yoy", "revenue_annual_yoy",
                "net_income_annual_yoy", "revenue_cagr_3y", "revenue_cagr_5y", "net_income_cagr_3y",
                "net_income_cagr_5y"):
        m = (growth.get("metrics") or {}).get(key)
        if not m:
            continue
        value = f"{m['value_pct']:+.1f}%" if m.get("status") == "ok" and m.get("value_pct") is not None else (
            "Insufficient history" if m.get("status") == "insufficient_history" else "n/a")
        periods = f"{m['current']['period_end']} vs {m['base']['period_end']}" if m.get("current") and m.get("base") else "—"
        rows.append([Paragraph(escape(m.get("label") or key), cell), Paragraph(escape(value), cell),
                     Paragraph(escape(periods), cell), Paragraph(escape(m.get("reason") or ""), cell)])
    story.append(pdf_common.styled_table(rows, [48 * mm, 24 * mm, 40 * mm, 66 * mm]))

    ratios = insights.get("ratios") or {}
    story.append(Spacer(1, 6))
    story.append(p(f"Key ratios vs {ratios.get('sector') or 'sector'} median", h3))
    rrows = [[Paragraph("<b>Ratio</b>", cell), Paragraph("<b>Company</b>", cell),
              Paragraph("<b>Sector median</b>", cell), Paragraph("<b>Companies</b>", cell)]]
    for r in ratios.get("rows") or []:
        fmt = (lambda v: "n/a" if v is None else (f"{v:.1f}%" if r["unit"] == "%" else f"{v:.2f}x"))
        rrows.append([Paragraph(escape(r["label"]), cell), Paragraph(fmt(r.get("company")), cell),
                      Paragraph(fmt(r.get("sector_median")), cell), Paragraph(str(r.get("sector_companies") or 0), cell)])
    story.append(pdf_common.styled_table(rrows, [60 * mm, 36 * mm, 40 * mm, 42 * mm]))
    if ratios.get("basis"):
        story.append(p(f"{ratios['basis']}. {ratios.get('sector_note') or ''}", small))

    # Sources.
    story.append(p("News sources", h2))
    sources = insights.get("sources") or []
    if sources:
        for i, s in enumerate(sources, 1):
            url = escape(s.get("url") or "")
            story.append(Paragraph(
                f"{i}. {escape(s.get('title') or '')} — {escape(s.get('publisher') or 'Unknown publisher')}, "
                f"{escape(str(s.get('published_at') or '')[:10])}. <link href=\"{url}\" color=\"#2952e3\">{url}</link>",
                small))
    else:
        story.append(p("No verified company-specific news was used in this report.", small))

    story.append(p("Disclaimer", h2))
    story.append(p(DISCLAIMER, small))

    footer = pdf_common.page_decorator(f"InsightIQ · {company_name} ({ticker}) research report · "
                                       f"generated {_when(report.get('generated_at_utc'))} · "
                                       f"data as of {_when(insights.get('data_as_of'))}")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()


def company_name(ticker):
    from app.services import market_storage
    from app.services.company_profile import display_name

    stored = market_storage.get_company(ticker) or {}
    return stored.get("name") or display_name(ticker) or ticker


def build(ticker, run_id, root=None):
    """(pdf_bytes, filename) for a saved report. No external calls."""
    from app.services import report_history, report_insights

    doc = report_history.read_report(ticker, run_id)
    report = doc["report"]
    insights = report_insights.load_or_build(report, root)
    name = company_name(ticker)
    return render_pdf(report, insights, doc.get("summary"), name), safe_filename(name, report.get("generated_at_utc"))
