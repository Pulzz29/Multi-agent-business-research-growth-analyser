"""
Fundamentals of one market company, computed deterministically from stored
provider statements and the latest stored quote (no provider or AI calls).

* Statement metrics use the latest annual period that has revenue and net
  income, and the prior annual period 300-430 days earlier for growth and
  averages (the rules of sector_comparison.company_metrics, reused here).
* Quote metrics (market cap, P/E, P/B, dividend yield) are the provider's
  reported values from the latest stored quote.
* Every metric carries its formula, inputs, reporting period and currency.
  A metric whose inputs are missing, non-positive where that makes it
  meaningless, or in different currencies is returned unavailable with the
  reason; nothing is estimated.
"""

from decimal import Decimal

from app.services import fiscal
from app.services import market_storage as storage
from app.services import sector_comparison as sc

GROUPS = (
    ("valuation", "Valuation"),
    ("profitability", "Profitability"),
    ("returns", "Returns"),
    ("liquidity", "Liquidity and leverage"),
    ("growth", "Growth"),
    ("cash_flow", "Cash flow"),
)


def _metric(key, label, group, unit, formula, value=None, reason=None, inputs=None, period=None, currency=None):
    available = value is not None
    return {
        "key": key, "label": label, "group": group, "unit": unit, "formula": formula,
        "value": None if value is None else round(float(value), 6),
        "available": available,
        "reason": None if available else (reason or "Required inputs are not in the stored provider data."),
        "inputs": {k: (None if v is None else str(v)) for k, v in (inputs or {}).items()},
        "period_end": period,
        "currency": currency,
    }


def _ratio(numerator, denominator, positive_denominator=True):
    if numerator is None or denominator is None or denominator == 0:
        return None
    if positive_denominator and denominator <= 0:
        return None
    return numerator / denominator


def _growth(current, prior):
    if current is None or prior is None or prior <= 0:
        return None
    return (current - prior) / prior * 100


def compute(market, symbol, as_of=None):
    periods = storage.statement_values(symbol, "annual", as_of=as_of)
    base, base_inputs = sc.company_metrics(symbol, as_of=as_of)
    quote = storage.latest_quote(symbol, as_of=as_of)
    latest = base_inputs.get("fiscal_period_end")
    prior = base_inputs.get("prior_period_end")
    stmt_currency = base_inputs.get("statement_currency")
    quote_currency = (quote or {}).get("currency")

    def v(period, metric):
        row = (periods.get(period) or {}).get(metric) if period else None
        return sc._dec(row["value_text"]) if row else None

    def cur(metric):
        return v(latest, metric)

    revenue, net_income, operating = cur("revenue"), cur("net_income"), cur("operating_income")
    gross = cur("gross_profit")
    ocf, capex = cur("operating_cash_flow"), cur("capital_expenditure")
    cash, debt, equity = cur("cash"), cur("total_debt"), cur("total_equity")
    ca, cl, inventory = cur("current_assets"), cur("current_liabilities"), cur("inventory")
    interest, ebitda = cur("interest_expense"), cur("ebitda")
    if ebitda is None and operating is not None and cur("depreciation_amortization") is not None:
        ebitda = operating + abs(cur("depreciation_amortization"))
    market_cap = sc._dec((quote or {}).get("market_cap"))
    period_label = fiscal.period_label(latest, market) if latest else None
    no_period = "No complete annual statement (revenue and net income) is stored yet."

    def stmt(key, label, group, unit, formula, value, inputs, reason=None):
        return _metric(key, label, group, unit, formula, value,
                       reason or (no_period if not latest else None), inputs, latest, stmt_currency)

    def quote_metric(key, label, unit, formula):
        number = sc._dec((quote or {}).get(key))
        reason = "No stored quote." if not quote else "The provider did not report this value."
        return _metric(key, label, "valuation", unit, formula, number, reason,
                       {"provider": (quote or {}).get("provider")}, None, quote_currency)

    metrics = [
        quote_metric("market_cap", "Market capitalisation", "currency", "Provider-reported market value of equity"),
        quote_metric("pe_ratio", "P/E (trailing)", "ratio", "Share price / trailing EPS (provider)"),
        quote_metric("pb_ratio", "P/B", "ratio", "Share price / book value per share (provider)"),
    ]

    # EV/EBITDA only when market cap and statements share a currency.
    ev_reason = None
    ev_value = None
    if market_cap is None:
        ev_reason = "Market capitalisation is not available."
    elif stmt_currency and quote_currency and stmt_currency != quote_currency:
        ev_reason = (f"Market cap is in {quote_currency} but statements are in {stmt_currency}; "
                     "no conversion is applied.")
    elif ebitda is None:
        ev_reason = "EBITDA (or operating income and depreciation) is not reported."
    elif debt is None or cash is None:
        ev_reason = "Total debt or cash is not reported."
    elif ebitda <= 0:
        ev_reason = "EBITDA is zero or negative; the multiple is not meaningful."
    else:
        ev_value = (market_cap + debt - cash) / ebitda
    metrics.append(_metric("ev_to_ebitda", "EV / EBITDA", "valuation", "ratio",
                           "(Market cap + total debt - cash) / EBITDA", ev_value, ev_reason,
                           {"market_cap": market_cap, "total_debt": debt, "cash": cash, "ebitda": ebitda},
                           latest, stmt_currency))
    metrics.append(stmt("eps_diluted", "EPS (diluted)", "valuation", "per_share",
                        "Diluted earnings per share (reported)", cur("eps_diluted"), {}))
    dividend = quote_metric("dividend_yield_pct", "Dividend yield", "pct", "Trailing annual dividend / price")
    metrics.append(dividend)

    metrics += [
        stmt("gross_margin_pct", "Gross margin", "profitability", "pct", "Gross profit / revenue",
             None if gross is None or not revenue else gross / abs(revenue) * 100,
             {"gross_profit": gross, "revenue": revenue}),
        stmt("operating_margin_pct", "Operating margin", "profitability", "pct", "Operating income / revenue",
             base.get("operating_margin_pct"), {"operating_income": operating, "revenue": revenue}),
        stmt("net_margin_pct", "Net margin", "profitability", "pct", "Net income / revenue",
             base.get("net_margin_pct"), {"net_income": net_income, "revenue": revenue}),
        stmt("roe_pct", "Return on equity", "returns", "pct",
             f"Net income / {base_inputs.get('roe_basis', 'average equity')}", base.get("roe_pct"),
             {"net_income": net_income, "total_equity": equity},
             None if latest else None),
        stmt("roa_pct", "Return on assets", "returns", "pct", "Net income / average total assets",
             base.get("roa_pct"), {"net_income": net_income, "total_assets": cur("total_assets")}),
        stmt("current_ratio", "Current ratio", "liquidity", "ratio", "Current assets / current liabilities",
             _ratio(ca, cl), {"current_assets": ca, "current_liabilities": cl}),
        stmt("quick_ratio", "Quick ratio", "liquidity", "ratio",
             "(Current assets - inventory) / current liabilities",
             _ratio(ca - (inventory or Decimal(0)), cl) if ca is not None and inventory is not None else None,
             {"current_assets": ca, "inventory": inventory, "current_liabilities": cl},
             None if inventory is not None or ca is None else "Inventory is not reported separately."),
        stmt("debt_to_equity", "Debt to equity", "liquidity", "ratio", "Total debt / shareholders' equity",
             base.get("debt_to_equity"), {"total_debt": debt, "total_equity": equity}),
        stmt("interest_coverage", "Interest coverage", "liquidity", "ratio", "Operating income / interest expense",
             _ratio(operating, abs(interest) if interest is not None else None),
             {"operating_income": operating, "interest_expense": interest}),
        stmt("revenue_growth_pct", "Revenue growth (YoY)", "growth", "pct", "Annual revenue vs prior fiscal year",
             base.get("revenue_growth_pct"), {"revenue": revenue, "prior_revenue": v(prior, "revenue")},
             None if prior or not latest else "No prior fiscal year is stored."),
        stmt("earnings_growth_pct", "Earnings growth (YoY)", "growth", "pct",
             "Annual net income vs prior fiscal year (positive base only)",
             _growth(net_income, v(prior, "net_income")),
             {"net_income": net_income, "prior_net_income": v(prior, "net_income")},
             None if not latest else ("No prior fiscal year is stored." if not prior else
                                      "Prior net income is zero or negative; growth is not meaningful.")),
        stmt("ocf_growth_pct", "Operating cash flow growth (YoY)", "growth", "pct",
             "Annual operating cash flow vs prior fiscal year (positive base only)",
             _growth(ocf, v(prior, "operating_cash_flow")),
             {"operating_cash_flow": ocf, "prior_operating_cash_flow": v(prior, "operating_cash_flow")}),
        stmt("operating_cash_flow", "Operating cash flow", "cash_flow", "currency", "Reported operating cash flow",
             ocf, {}),
        stmt("free_cash_flow", "Free cash flow", "cash_flow", "currency",
             "Operating cash flow - capital expenditure",
             None if ocf is None or capex is None else ocf - abs(capex),
             {"operating_cash_flow": ocf, "capital_expenditure": capex},
             None if not latest or capex is not None else "Capital expenditure is not reported."),
    ]
    return {
        "market": market,
        "symbol": symbol,
        "period_end": latest,
        "period_label": period_label,
        "prior_period_end": prior,
        "statement_currency": stmt_currency,
        "quote_currency": quote_currency,
        "statement_provider": base_inputs.get("statement_provider"),
        "quote_provider": (quote or {}).get("provider"),
        "quote_fetched_at": (quote or {}).get("fetched_at"),
        "groups": [{"key": k, "label": label} for k, label in GROUPS],
        "metrics": metrics,
        "available_count": sum(1 for m in metrics if m["available"]),
    }


SERIES_METRICS = (
    ("revenue", "Revenue"), ("net_income", "Net income"), ("operating_income", "Operating income"),
    ("operating_cash_flow", "Operating cash flow"), ("eps_diluted", "EPS (diluted)"),
)


def financial_series(market, symbol):
    """Stored annual and quarterly statement series for charts (reported values only)."""
    out = {"market": market, "symbol": symbol, "annual": {}, "quarterly": {}, "currencies": []}
    currencies = set()
    for period_type in ("annual", "quarterly"):
        periods = storage.statement_values(symbol, period_type)
        for metric, label in SERIES_METRICS:
            points = []
            for end in sorted(periods):
                row = periods[end].get(metric)
                value = sc._dec(row["value_text"]) if row else None
                if value is None:
                    continue
                currencies.add(row.get("currency"))
                points.append({"period_end": end, "label": fiscal.period_label(end, market, period_type),
                               "value": float(value), "currency": row.get("currency"),
                               "provider": row.get("provider")})
            out[period_type][metric] = {"label": label, "points": points}
    out["currencies"] = sorted(c for c in currencies if c)
    return out
