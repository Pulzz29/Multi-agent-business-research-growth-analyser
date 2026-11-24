// View model for the growth analysis returned by the backend
// (backend/app/services/growth_analysis.py). Pure functions, no React, so the
// same rows feed the company pages, the report and the tests.

export const GROWTH_ROWS = [
  "revenue_qoq",
  "revenue_yoy",
  "net_income_yoy",
  "eps_yoy",
  "revenue_annual_yoy",
  "net_income_annual_yoy",
  "eps_annual_yoy",
  "revenue_cagr_3y",
  "revenue_cagr_5y",
  "net_income_cagr_3y",
  "net_income_cagr_5y",
];

const STATUS_TEXT = {
  ok: null,
  not_meaningful: "n/a",
  insufficient_history: "Insufficient history",
  unavailable: "n/a",
};

const TREND_TEXT = {
  accelerating: "Accelerating",
  stable: "Stable",
  decelerating: "Decelerating",
  insufficient_history: "Insufficient history",
};

function finite(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function pctText(value) {
  const n = finite(value);
  if (n === null) return null;
  return `${n > 0 ? "+" : ""}${n.toFixed(1)}%`;
}

export function growthRow(metric) {
  if (!metric) return null;
  const value = metric.status === "ok" ? pctText(metric.value_pct) : null;
  const period =
    metric.current && metric.base ? `${metric.current.period_end} vs ${metric.base.period_end}` : null;
  return {
    key: metric.key,
    label: metric.label,
    definition: metric.definition,
    status: metric.status,
    valueText: value ?? STATUS_TEXT[metric.status] ?? "n/a",
    reason: metric.status === "ok" ? null : metric.reason,
    period,
    tone: value === null ? "neutral" : metric.value_pct > 0 ? "positive" : metric.value_pct < 0 ? "negative" : "neutral",
  };
}

export function growthRows(growth) {
  const metrics = growth?.metrics || {};
  return GROWTH_ROWS.map((key) => growthRow(metrics[key])).filter(Boolean);
}

export function trendView(trend) {
  if (!trend) return null;
  const label = TREND_TEXT[trend.label] || trend.label;
  const tone =
    trend.label === "accelerating" ? "positive" : trend.label === "decelerating" ? "warning" : "neutral";
  const detail =
    trend.label === "insufficient_history"
      ? trend.reason
      : `Latest ${trend.basis} YoY ${pctText(trend.latest_pct)} vs ${pctText(trend.previous_average_pct)} average of the previous two (${trend.difference_pp > 0 ? "+" : ""}${trend.difference_pp.toFixed(1)} pp).`;
  return { label, tone, rule: trend.rule, detail };
}

// YoY growth series for the growth chart: quarterly when available.
export function yoyChartSeries(growth, metric = "revenue") {
  const series = growth?.yoy_series?.[metric] || {};
  const quarterly = (series.quarterly || []).filter((p) => p.status === "ok");
  const annual = (series.annual || []).filter((p) => p.status === "ok");
  const basis = quarterly.length >= 2 ? "quarterly" : annual.length >= 2 ? "annual" : null;
  const points = basis === "quarterly" ? quarterly : basis === "annual" ? annual : [];
  return {
    basis,
    points: points.map((p) => ({ period_end: p.period_end, value_pct: p.value_pct, base: p.base_period_end })),
    excluded: (series[basis || "quarterly"] || []).filter((p) => p.status !== "ok").length,
  };
}
