"""
Fiscal-period labels per market.

India (IN): the fiscal year runs April-March and is named after the year in
which it ends (FY2025 = 1 Apr 2024 - 31 Mar 2025); Q1 = Apr-Jun, Q2 = Jul-Sep,
Q3 = Oct-Dec, Q4 = Jan-Mar. US: providers report each company's own fiscal
calendar, so periods are labelled by their end date.
"""

from datetime import date


def _date(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def indian_fiscal_year(period_end):
    d = _date(period_end)
    if d is None:
        return None
    return d.year if d.month <= 3 else d.year + 1


def indian_quarter(period_end):
    d = _date(period_end)
    if d is None:
        return None
    return {4: 1, 5: 1, 6: 1, 7: 2, 8: 2, 9: 2, 10: 3, 11: 3, 12: 3, 1: 4, 2: 4, 3: 4}[d.month]


def period_label(period_end, market, period_type="annual"):
    """'FY2025' / 'Q4 FY2025' for India; 'FY ending 2025-09-30' / 'Quarter ending ...' otherwise."""
    d = _date(period_end)
    if d is None:
        return str(period_end or "")
    if market == "IN":
        fy = indian_fiscal_year(d)
        if period_type == "quarterly":
            return f"Q{indian_quarter(d)} FY{fy}"
        return f"FY{fy}"
    if period_type == "quarterly":
        return f"Quarter ending {d.isoformat()}"
    return f"FY ending {d.isoformat()}"
