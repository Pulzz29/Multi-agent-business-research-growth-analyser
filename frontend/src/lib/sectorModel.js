// View model for a saved sector-comparison snapshot (exact backend JSON).
// Nothing here computes a ranking: scores, ranks and breakdowns come from the
// backend's deterministic model; this file only formats them for display.

export const MARKET_LABELS = { US: "United States", IN: "India" };

const list = (value) => (Array.isArray(value) ? value : []);

export function formatMetric(value, unit, currency) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  const n = Number(value);
  if (unit === "%") return `${n.toFixed(1)}%`;
  if (unit === "x") return `${n.toFixed(2)}×`;
  if (unit === "currency") {
    const symbol = currency === "INR" ? "₹" : currency === "USD" ? "$" : `${currency} `;
    if (Math.abs(n) >= 1e12) return `${symbol}${(n / 1e12).toFixed(2)}T`;
    if (Math.abs(n) >= 1e9) return `${symbol}${(n / 1e9).toFixed(1)}B`;
    if (Math.abs(n) >= 1e7 && currency === "INR") return `${symbol}${(n / 1e7).toFixed(0)} Cr`;
    return `${symbol}${n.toLocaleString("en-US")}`;
  }
  return String(n);
}

export function buildComparisonModel(snapshot) {
  if (!snapshot || typeof snapshot !== "object" || !snapshot.ranking) return null;
  const ranking = snapshot.ranking;
  const definitions = snapshot.metric_definitions || {};
  const used = list(ranking.method?.metrics_used);
  const narrative = snapshot.narrative || {};
  const texts = narrative.companies || {};
  const byCompany = Object.fromEntries(list(snapshot.companies).map((c) => [c.symbol, c]));

  const metrics = used.map((key) => ({
    key,
    label: definitions[key]?.label || key,
    unit: definitions[key]?.unit,
    direction: definitions[key]?.direction,
    weight: ranking.method?.weights_applied?.[key] ?? null,
  }));

  const rows = list(ranking.companies).map((item) => {
    const company = byCompany[item.symbol] || {};
    const text = texts[item.symbol] || {};
    const breakdown = Object.fromEntries(list(item.breakdown).map((b) => [b.metric, b]));
    return {
      symbol: item.symbol,
      name: item.name,
      rank: item.rank,
      score: item.score,
      ranked: Boolean(item.ranked),
      notRankedReason: item.not_ranked_reason,
      coverage: item.coverage,
      dataStatus: company.data_status || "unknown",
      inputs: company.inputs || {},
      values: Object.fromEntries(
        metrics.map((m) => [
          m.key,
          {
            raw: company.metrics?.[m.key] ?? null,
            display: formatMetric(company.metrics?.[m.key], m.unit, snapshot.currency),
            score: breakdown[m.key]?.normalized_score ?? null,
            imputed: Boolean(breakdown[m.key]?.imputed),
            contribution: breakdown[m.key]?.contribution ?? null,
          },
        ])
      ),
      why: text.why_it_ranks || item.explanation || "",
      strengths: list(text.strengths),
      weaknesses: list(text.weaknesses),
      textSource: text.source || "deterministic",
      breakdown: list(item.breakdown),
    };
  });

  const ranked = rows.filter((r) => r.ranked);
  const scoreBars = ranked.map((r) => ({
    key: r.symbol,
    label: r.symbol,
    fullLabel: `${r.rank}. ${r.name}`,
    values: [{ id: "score", label: "Composite score", value: r.score, tone: "primary", title: `${r.symbol}: ${r.score}` }],
  }));

  // Radar: top companies' normalised metric scores (0-100).
  const radar = {
    axes: metrics.map((m) => ({ key: m.key, label: m.label })),
    series: ranked.slice(0, 4).map((r, i) => ({
      id: r.symbol,
      label: `${r.rank}. ${r.symbol}`,
      tone: ["primary", "secondary", "forecast", "accent"][i],
      values: metrics.map((m) => r.values[m.key].score ?? 0),
    })),
  };

  // Grouped bars of three key profitability/return metrics' normalised scores.
  const keyMetrics = metrics.filter((m) => ["net_margin_pct", "roe_pct", "revenue_growth_pct"].includes(m.key)).slice(0, 3);
  const tones = ["primary", "secondary", "forecast"];
  const metricBars = ranked.map((r) => ({
    key: r.symbol,
    label: r.symbol,
    fullLabel: r.name,
    values: keyMetrics.map((m, i) => ({
      id: m.key,
      label: `${m.label} (${r.values[m.key].display})`,
      value: r.values[m.key].score,
      tone: tones[i],
    })),
  }));

  return {
    id: snapshot.comparison_id,
    market: snapshot.market,
    marketLabel: snapshot.market_label || MARKET_LABELS[snapshot.market] || snapshot.market,
    currency: snapshot.currency,
    sectorKey: snapshot.sector_key,
    sectorLabel: snapshot.sector_label,
    generatedAt: snapshot.generated_at_utc,
    status: ranking.status,
    ties: list(ranking.ties),
    dataSources: snapshot.data_sources || {},
    metrics,
    droppedMetrics: list(ranking.method?.metrics_dropped).map((d) => ({
      ...d,
      label: definitions[d.metric]?.label || d.metric,
    })),
    rows,
    rankedCount: ranked.length,
    scoreBars,
    radar,
    metricBars,
    metricBarLegend: keyMetrics.map((m, i) => ({ id: m.key, label: `${m.label} (relative score)`, tone: tones[i] })),
    overview: narrative.overview || "",
    overviewSource: narrative.overview_source || "deterministic",
    narrativeStatus: narrative.status || "unavailable",
    narrativeReason: narrative.status_reason || null,
    narrativeLabel: narrative.label || null,
    withheld: list(narrative.withheld),
    methodology: list(snapshot.methodology),
    disclaimers: list(snapshot.disclaimers),
  };
}
