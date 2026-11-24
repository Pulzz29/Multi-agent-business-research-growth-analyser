"""
Per-report chart and metrics dataset ("insights").

For each saved research report, the numbers behind its charts, growth
analysis, ratios and news sources are computed once from stored data as it was
when the report was generated (point-in-time: SEC facts filed by then, provider
rows stored by then) and frozen in reports/research_runs/insights/{run_id}.json.
The charts, the AI research brief and the PDF all read this one file, so they
always agree. Nothing here calls an external provider or the AI service.
"""

import hashlib
import json
import logging
import os
import statistics
import threading
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from app.services import growth_analysis
from app.services import report_history

logger = logging.getLogger("insightiq.report_insights")

INSIGHTS_VERSION = 1
MAX_QUARTERS = 12
MAX_YEARS = 6
PRICE_UNAVAILABLE = (
    "Price history is not stored for this company: InsightIQ stores the latest quote "
    "only, so no price or volume chart can be drawn from stored data."
)
RATIO_KEYS = ("pe_ratio", "roe_pct", "net_margin_pct", "operating_margin_pct", "debt_to_equity")
RATIO_LABELS = {
    "pe_ratio": ("P/E ratio", "x"),
    "roe_pct": ("Return on equity", "%"),
    "net_margin_pct": ("Net margin", "%"),
    "operating_margin_pct": ("Operating margin", "%"),
    "debt_to_equity": ("Debt-to-equity", "x"),
}

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


class InsightsError(RuntimeError):
    pass


def insights_directory(root=None):
    return report_history.reports_directory(root) / "insights"


def _lock(run_id):
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(run_id, threading.Lock())


def _dec(value):
    try:
        number = Decimal(str(value))
    except Exception:
        return None
    return number if number.is_finite() else None


def _ratio_pct(numerator, denominator):
    n, d = _dec(numerator), _dec(denominator)
    if n is None or d is None or d <= 0:
        return None
    return round(float(n / d * 100), 2)


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


# ----------------------------------------------------------- period tables

def _merge(*named_series):
    """{name: [points]} -> sorted rows {period_end, name: value, ...}."""
    rows = {}
    for name, points in named_series:
        for p in points or []:
            value = _dec(p.get("value"))
            if value is not None and p.get("period_end"):
                rows.setdefault(p["period_end"], {"period_end": p["period_end"]})[name] = float(value)
    return [rows[k] for k in sorted(rows)]


def _margins(rows):
    out = []
    for row in rows:
        revenue = row.get("revenue")
        out.append({
            "period_end": row["period_end"],
            "gross_margin_pct": _ratio_pct(row.get("gross_profit"), revenue),
            "operating_margin_pct": _ratio_pct(row.get("operating_income"), revenue),
            "net_margin_pct": _ratio_pct(row.get("net_income"), revenue) if revenue and revenue > 0 else None,
        })
    return out


def _annual_from_quarters(points):
    by_year = {}
    for p in points or []:
        if p.get("fiscal_year_start") and p.get("fiscal_quarter"):
            by_year.setdefault(p["fiscal_year_start"], []).append(p)
    out = []
    for _, qs in sorted(by_year.items()):
        if sorted(q["fiscal_quarter"] for q in qs) == [1, 2, 3, 4]:
            out.append({"period_end": max(q["period_end"] for q in qs),
                        "value": str(sum(Decimal(str(q["value"])) for q in qs))})
    return out


def financial_tables(series):
    """Revenue/net income and margin tables, quarterly and annual."""
    q = {k: (series.get(k) or {}).get("quarterly") or [] for k in
         ("revenue", "net_income", "operating_income", "gross_profit")}
    a = {}
    for k in ("revenue", "net_income", "operating_income", "gross_profit"):
        annual = (series.get(k) or {}).get("annual")
        a[k] = annual if annual else _annual_from_quarters(q[k])
    quarterly = _merge(*q.items())[-MAX_QUARTERS:]
    annual = _merge(*a.items())[-MAX_YEARS:]
    gross_any = any(r.get("gross_profit") is not None for r in quarterly + annual)
    return {
        "revenue_net_income": {
            "quarterly": [{k: r.get(k) for k in ("period_end", "revenue", "net_income")} for r in quarterly],
            "annual": [{k: r.get(k) for k in ("period_end", "revenue", "net_income")} for r in annual],
        },
        "margins": {
            "quarterly": _margins(quarterly),
            "annual": _margins(annual),
            "gross_margin_available": gross_any,
            "gross_margin_reason": None if gross_any else
                "Gross profit is not stored for this company (no gross profit or cost of revenue "
                "in its filings or provider statements).",
        },
    }


# ----------------------------------------------------------------- ratios

def _sector_of(market, symbol):
    from app.services import market_registry, market_storage

    stored = market_storage.get_company(symbol) or {}
    if stored.get("sector_key"):
        return stored["sector_key"]
    for key, sector in market_registry.market_config(market)["sectors"].items():
        if any(c["symbol"] == symbol for c in sector["companies"]):
            return key
    return None


def _sec_ratios(ticker, series, as_of):
    """Ratios from SEC data for companies without provider statements:
    trailing-four-quarter margins and ROE on the latest reported equity."""
    from app.database.sqlite_manager import connect

    def ttm(name):
        points = ((series.get(name) or {}).get("quarterly") or [])[-4:]
        if len(points) < 4:
            return None
        return sum(Decimal(str(p["value"])) for p in points)

    revenue, net_income, operating = ttm("revenue"), ttm("net_income"), ttm("operating_income")
    with connect() as conn:
        row = conn.execute(
            """SELECT value_text, period_end FROM sec_facts
               WHERE company_ticker = ? AND concept = 'StockholdersEquity' AND unit = 'USD'
                 AND filed_at <= ? ORDER BY period_end DESC, filed_at DESC LIMIT 1""",
            (ticker, str(as_of)[:10]),
        ).fetchone()
    equity = _dec(row["value_text"]) if row else None
    return {
        "net_margin_pct": _ratio_pct(net_income, revenue),
        "operating_margin_pct": _ratio_pct(operating, revenue),
        "roe_pct": _ratio_pct(net_income, equity) if equity and equity > 0 else None,
        "debt_to_equity": None,
        "pe_ratio": None,
    }, {"basis": "SEC filings, trailing four quarters; ROE on latest reported equity; "
                 "debt is not stored from SEC filings, so debt-to-equity is not available",
        "equity_period_end": row["period_end"] if row else None}


def ratios_snapshot(ticker, market, research_profile, series, as_of):
    from app.services import market_storage, sector_comparison

    has_statements = bool(market_storage.statement_values(ticker, "annual", as_of=as_of))
    if has_statements:
        metrics, inputs = sector_comparison.company_metrics(ticker, as_of=as_of)
        basis = f"Latest fiscal year in stored {inputs.get('statement_provider') or 'provider'} statements"
    else:
        metrics, extra = _sec_ratios(ticker, series, as_of) if research_profile == "sec_validated" else ({}, {})
        quote = market_storage.latest_quote(ticker, as_of=as_of)
        if quote and _dec(quote.get("pe_ratio")) is not None:
            metrics["pe_ratio"] = float(_dec(quote["pe_ratio"]))
        basis = extra.get("basis") or "No stored statements"

    sector_key = _sector_of(market, ticker)
    medians = {}
    members_used = []
    if sector_key:
        from app.services import market_registry

        _, sector, members = market_registry.sector_members(market, sector_key)
        values = {k: [] for k in RATIO_KEYS}
        for member in members:
            symbol = member["symbol"]
            if not market_storage.statement_values(symbol, "annual", as_of=as_of):
                continue
            member_metrics, _ = sector_comparison.company_metrics(symbol, as_of=as_of)
            members_used.append(symbol)
            for k in RATIO_KEYS:
                v = member_metrics.get(k)
                if v is not None and not (k == "pe_ratio" and v <= 0):
                    values[k].append(v)
        medians = {k: {"median": round(statistics.median(v), 2), "companies": len(v)} if v else None
                   for k, v in values.items()}
        sector_label = sector["label"]
    else:
        sector_label = None

    rows = []
    for key in RATIO_KEYS:
        label, unit = RATIO_LABELS[key]
        value = metrics.get(key)
        rows.append({"key": key, "label": label, "unit": unit,
                     "company": None if value is None else round(float(value), 2),
                     "sector_median": (medians.get(key) or {}).get("median"),
                     "sector_companies": (medians.get(key) or {}).get("companies", 0)})
    return {
        "basis": basis,
        "sector": sector_label,
        "sector_key": sector_key,
        "sector_members_with_data": members_used,
        "sector_note": ("Sector median of the sector's companies (including this one) from their stored "
                        "provider statements as of the report date; companies without stored statements "
                        "are excluded."
                        if sector_key else "No sector is mapped for this company."),
        "rows": rows,
    }


# ------------------------------------------------------------------ build

def _sources(report):
    news = report.get("news") or {}
    seen, out = set(), []
    for article in (news.get("approved_primary_articles") or []) + (news.get("selected_articles") or []):
        url = article.get("source_url") or article.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        out.append({"evidence_id": article.get("evidence_id"), "title": article.get("title"),
                    "publisher": article.get("publisher") or article.get("source_name"),
                    "url": url, "published_at": article.get("published_at")})
    return out


def _price(ticker, as_of):
    from app.services import market_storage

    quote = market_storage.latest_quote(ticker, as_of=as_of)
    latest = None
    if quote:
        latest = {k: quote.get(k) for k in ("price", "currency", "market_cap", "pe_ratio", "provider", "fetched_at")}
    return {"available": False, "reason": PRICE_UNAVAILABLE, "latest_quote": latest}


def build_insights(report):
    ticker = report["ticker"]
    as_of = report.get("generated_at_utc")
    if not as_of:
        raise InsightsError("The report has no generation time.")
    profile = report.get("research_profile") or "sec_validated"
    market = report.get("market") or ("IN" if ticker.endswith((".NS", ".BO")) else "US")
    growth, series = growth_analysis.company_growth(ticker, as_of=as_of)
    tables = financial_tables(series)
    currency = growth.get("currency") or report.get("currency") or "USD"
    dataset = {
        "insights_version": INSIGHTS_VERSION,
        "run_id": report["run_id"],
        "ticker": ticker,
        "market": market,
        "currency": currency,
        "units": f"{currency}, as reported (not scaled)",
        "research_profile": profile,
        "data_as_of": as_of,
        "data_source": growth.get("source"),
        **tables,
        "growth": growth,
        "ratios": ratios_snapshot(ticker, market, profile, series, as_of),
        "price": _price(ticker, as_of),
        "sources": _sources(report),
    }
    dataset["dataset_sha256"] = hashlib.sha256(_canonical(dataset).encode("utf-8")).hexdigest()
    return dataset


def _write_once(path, record):
    """Create the file only if it does not exist (never overwritten)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temp.write_text(json.dumps(record, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
    try:
        os.link(temp, path)  # atomic create; fails if the file exists
    except FileExistsError:
        pass
    finally:
        temp.unlink(missing_ok=True)


def load_or_build(report, root=None):
    """The frozen dataset for an already-loaded saved report."""
    run_id = report["run_id"]
    path = insights_directory(root) / f"{run_id}.json"
    with _lock(run_id):
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        record = build_insights(report)
        record["computed_at_utc"] = datetime.now(timezone.utc).isoformat()
        _write_once(path, record)
        logger.info("report_insights frozen run=%s ticker=%s sha=%s",
                    run_id, report["ticker"], record["dataset_sha256"][:12])
        return json.loads(path.read_text(encoding="utf-8"))


def get_insights(ticker, run_id, root=None):
    """The frozen dataset for one saved report, computed on first use."""
    doc = report_history.read_report(ticker, run_id)
    return load_or_build(doc["report"], root)
