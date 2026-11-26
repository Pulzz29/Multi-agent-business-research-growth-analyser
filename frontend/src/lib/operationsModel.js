// Pure helpers for the Operations panel (unit-tested with node --test).

const KIND_LABELS = {
  api: "API requests",
  db: "Database statements",
  workflow: "Research workflow",
  agent: "Agents",
  financial_calculation: "Financial calculations",
  forecast: "Forecasting",
  model_call: "Model calls",
  validation: "Validation",
  pdf: "PDF rendering",
};

export function ms(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  const n = Number(value);
  return n >= 1000 ? `${(n / 1000).toFixed(2)} s` : `${n.toFixed(n < 10 ? 2 : 0)} ms`;
}

export function latencyRows(latency) {
  const order = Object.keys(KIND_LABELS);
  return (latency?.by_kind || [])
    .map((row) => ({ ...row, label: KIND_LABELS[row.kind] || row.kind }))
    .sort((a, b) => (order.indexOf(a.kind) + 1 || 99) - (order.indexOf(b.kind) + 1 || 99));
}

export function tokenRows(tokens) {
  return (tokens?.models || []).map((m) => ({
    ...m,
    label: `${m.provider === "azure_openai" ? "Azure OpenAI" : m.provider === "groq" ? "Groq" : m.provider} · ${m.model}`,
    sources: m.sources || [],
  }));
}
