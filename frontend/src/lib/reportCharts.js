// Chart view models for the research report, built only from the frozen
// per-report dataset (GET /companies/{t}/reports/{run}/insights). Pure
// functions: every chart either gets complete props or an explicit empty state
// with the reason, never a half-drawn chart.

import { yoyChartSeries } from "./growthModel.js";

export const MIN_POINTS = 2;
const SYMBOLS = { USD: "$", INR: "₹" };

function finite(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function empty(reason) {
  return { empty: true, title: "Not enough data to chart", reason };
}

function notEnough(have, what = "periods") {
  return empty(`Need at least ${MIN_POINTS} ${what}, have ${have}.`);
}

// Values are stored as reported (raw currency units); charts use billions.
export function moneyUnit(currency) {
  return { divisor: 1e9, label: `${currency || "USD"} billions`, symbol: SYMBOLS[currency] ?? `${currency || ""} ` };
}

export function formatMoney(value, currency) {
  const n = finite(value);
  if (n === null) return "Unavailable";
  const { symbol } = moneyUnit(currency);
  const b = n / 1e9;
  const digits = Math.abs(b) >= 100 ? 0 : Math.abs(b) >= 10 ? 1 : 2;
  return `${b < 0 ? "-" : ""}${symbol}${Math.abs(b).toFixed(digits)}B`;
}

export function formatPctValue(value) {
  const n = finite(value);
  return n === null ? "Unavailable" : `${n.toFixed(1)}%`;
}

function quarterLabel(iso) {
  const [y, m] = String(iso).split("-").map(Number);
  return `Q${Math.floor((m - 1) / 3) + 1} ${String(y).slice(2)}`;
}

function yearLabel(iso) {
  return `FY ${String(iso).slice(0, 4)}`;
}

// Which period toggles have enough data for a table.
export function basisOptions(table) {
  return ["quarterly", "annual"].filter((b) => (table?.[b] || []).length >= MIN_POINTS);
}

export function revenueNetIncomeChart(insights, basis) {
  const rows = insights?.revenue_net_income?.[basis] || [];
  const usable = rows.filter((r) => finite(r.revenue) !== null || finite(r.net_income) !== null);
  if (usable.length < MIN_POINTS) return notEnough(usable.length, basis === "annual" ? "fiscal years" : "quarters");
  const currency = insights.currency;
  const unit = moneyUnit(currency);
  return {
    empty: false,
    yTitle: `${unit.label}`,
    xTitle: basis === "annual" ? "Fiscal year end" : "Quarter end",
    legend: [
      { id: "rev", label: "Revenue", tone: "primary" },
      { id: "ni", label: "Net income", tone: "secondary" },
    ],
    data: usable.map((r) => ({
      key: r.period_end,
      label: basis === "annual" ? yearLabel(r.period_end) : quarterLabel(r.period_end),
      fullLabel: `${basis === "annual" ? "Fiscal year" : "Quarter"} ending ${r.period_end}`,
      values: [
        { id: "rev", label: "Revenue", tone: "primary", value: finite(r.revenue) === null ? NaN : r.revenue / unit.divisor, title: `Revenue ${formatMoney(r.revenue, currency)}` },
        { id: "ni", label: "Net income", tone: "secondary", value: finite(r.net_income) === null ? NaN : r.net_income / unit.divisor, title: `Net income ${formatMoney(r.net_income, currency)}` },
      ],
    })),
  };
}

export function marginChart(insights, basis) {
  const margins = insights?.margins || {};
  const rows = margins[basis] || [];
  const lines = [
    { id: "gross", key: "gross_margin_pct", label: "Gross margin", tone: "accent" },
    { id: "operating", key: "operating_margin_pct", label: "Operating margin", tone: "secondary" },
    { id: "net", key: "net_margin_pct", label: "Net margin", tone: "primary" },
  ]
    .map((line) => ({ ...line, points: rows.filter((r) => finite(r[line.key]) !== null).map((r) => ({ x: r.period_end, y: r[line.key] })) }))
    .filter((line) => line.points.length >= MIN_POINTS);
  if (!lines.length) return notEnough(Math.max(0, ...["gross_margin_pct", "operating_margin_pct", "net_margin_pct"].map((k) => rows.filter((r) => finite(r[k]) !== null).length)), basis === "annual" ? "fiscal years" : "quarters");
  const notes = [];
  if (!margins.gross_margin_available) notes.push(margins.gross_margin_reason || "Gross margin is not available.");
  return {
    empty: false,
    yTitle: "Margin, % of revenue",
    xTitle: basis === "annual" ? "Fiscal year end" : "Quarter end",
    series: lines.map(({ id, label, tone, points }) => ({ id, label, tone, points })),
    notes,
  };
}

export function growthChart(insights) {
  const growth = insights?.growth;
  const revenue = yoyChartSeries(growth, "revenue");
  const netIncome = yoyChartSeries(growth, "net_income");
  if (!revenue.basis) {
    const reason = growth?.metrics?.revenue_yoy?.reason || "No year-over-year growth values can be computed.";
    return empty(reason);
  }
  const niByEnd = Object.fromEntries((netIncome.basis === revenue.basis ? netIncome.points : []).map((p) => [p.period_end, p]));
  const points = revenue.points.slice(-8);
  const label = revenue.basis === "annual" ? yearLabel : quarterLabel;
  const notes = [];
  if (revenue.excluded) notes.push(`${revenue.excluded} period(s) omitted: growth from a zero or negative base is not meaningful.`);
  if (netIncome.basis !== revenue.basis) notes.push("Net profit growth is not available on the same basis.");
  return {
    empty: false,
    basis: revenue.basis,
    yTitle: "Year-over-year growth, %",
    xTitle: revenue.basis === "annual" ? "Fiscal year end" : "Quarter end",
    legend: [
      { id: "rev", label: "Revenue YoY", tone: "primary" },
      { id: "ni", label: "Net profit YoY", tone: "secondary" },
    ],
    data: points.map((p) => ({
      key: p.period_end,
      label: label(p.period_end),
      fullLabel: `${p.period_end} vs ${p.base}`,
      values: [
        { id: "rev", label: "Revenue YoY", tone: "primary", value: p.value_pct, title: `Revenue ${formatPctValue(p.value_pct)} YoY` },
        { id: "ni", label: "Net profit YoY", tone: "secondary", value: niByEnd[p.period_end]?.value_pct ?? NaN, title: niByEnd[p.period_end] ? `Net profit ${formatPctValue(niByEnd[p.period_end].value_pct)} YoY` : "Net profit YoY not meaningful or unavailable" },
      ],
    })),
    notes,
  };
}

export function ratioRows(insights) {
  const ratios = insights?.ratios;
  const rows = (ratios?.rows || []).map((row) => {
    const company = finite(row.company);
    const median = finite(row.sector_median);
    const scale = Math.max(Math.abs(company ?? 0), Math.abs(median ?? 0)) || 1;
    const fmt = (v) => (v === null ? "Unavailable" : row.unit === "%" ? `${v.toFixed(1)}%` : `${v.toFixed(2)}×`);
    return {
      key: row.key,
      label: row.label,
      companyText: fmt(company),
      medianText: median === null ? "No sector data" : `${fmt(median)} (median of ${row.sector_companies})`,
      companyWidth: company === null ? 0 : Math.round((Math.abs(company) / scale) * 100),
      medianWidth: median === null ? 0 : Math.round((Math.abs(median) / scale) * 100),
      available: company !== null || median !== null,
    };
  });
  if (!rows.some((r) => r.available)) return { ...empty("No ratios could be computed from stored data."), rows: [] };
  return { empty: false, rows, sector: ratios.sector, basis: ratios.basis, note: ratios.sector_note };
}

export function priceView(insights) {
  const price = insights?.price || {};
  const quote = price.latest_quote;
  return {
    available: Boolean(price.available),
    reason: price.reason || "Price history is not stored.",
    latest: quote && finite(Number(quote.price)) !== null
      ? { text: `${SYMBOLS[quote.currency] ?? ""}${Number(quote.price).toLocaleString("en-US", { maximumFractionDigits: 2 })}`, provider: quote.provider, fetchedAt: quote.fetched_at }
      : null,
  };
}
