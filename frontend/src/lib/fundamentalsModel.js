// View model for the Fundamentals panel (pure; unit-tested).
import { fiscalLabel, formatMoney, formatPerShare } from "./format.js";

export function formatFundamental(metric) {
  if (!metric?.available || metric.value === null || metric.value === undefined) return "Not available";
  const v = Number(metric.value);
  if (!Number.isFinite(v)) return "Not available";
  if (metric.unit === "currency") return formatMoney(v, metric.currency || "USD");
  if (metric.unit === "per_share") return formatPerShare(v, metric.currency || "USD");
  if (metric.unit === "pct") return `${v.toFixed(2)}%`;
  return `${v.toFixed(2)}×`;
}

/** Groups in the backend's order; each metric with its display value and period. */
export function buildFundamentalsView(data, market) {
  if (!data) return { groups: [], available: 0, total: 0, period: null };
  const period = data.period_end ? fiscalLabel(data.period_end, market, "annual") : null;
  const groups = (data.groups || []).map((g) => ({
    key: g.key,
    label: g.label,
    metrics: (data.metrics || [])
      .filter((m) => m.group === g.key)
      .map((m) => ({
        ...m,
        display: formatFundamental(m),
        source: m.group === "valuation" && m.unit !== "per_share" && m.key !== "ev_to_ebitda" ? "quote" : "statements",
      })),
  }));
  return {
    groups: groups.filter((g) => g.metrics.length),
    available: data.available_count ?? (data.metrics || []).filter((m) => m.available).length,
    total: (data.metrics || []).length,
    period,
    statementCurrency: data.statement_currency,
    quoteCurrency: data.quote_currency,
    statementProvider: data.statement_provider,
    quoteProvider: data.quote_provider,
  };
}
