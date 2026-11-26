import { useCallback, useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { Card, EmptyState, ErrorState, Skeleton } from "./ui.jsx";
import { api } from "../lib/api.js";
import { buildFundamentalsView } from "../lib/fundamentalsModel.js";
import { relativeTime } from "../lib/format.js";

/**
 * Fundamentals (US and Indian companies): valuation, profitability, returns,
 * liquidity/leverage, growth and cash flow, computed by the backend from the
 * stored provider statements and quote. Unavailable values say why.
 */
export function FundamentalsPanel({ token, market, symbol, onAuthError }) {
  const [state, setState] = useState({ loading: true, error: "", status: 0, data: null });
  const [loadingData, setLoadingData] = useState(false);

  const load = useCallback(
    (signal) =>
      api
        .fundamentals(token, market, symbol, signal)
        .then((data) => setState({ loading: false, error: "", status: 200, data }))
        .catch((err) => {
          if (err?.name === "AbortError") return;
          if (err.status === 401) onAuthError?.();
          setState({ loading: false, error: err.message, status: err.status, data: null });
        }),
    [token, market, symbol, onAuthError]
  );

  useEffect(() => {
    const controller = new AbortController();
    setState({ loading: true, error: "", status: 0, data: null });
    load(controller.signal);
    return () => controller.abort();
  }, [load]);

  async function loadCompanyData() {
    setLoadingData(true);
    try {
      await api.companyData(token, market, symbol, "auto");
      await load();
    } catch (err) {
      setState({ loading: false, error: err.message, status: err.status, data: null });
    } finally {
      setLoadingData(false);
    }
  }

  if (state.loading) return <Card title="Fundamentals"><Skeleton lines={6} /></Card>;
  if (state.status === 404) {
    return (
      <Card title="Fundamentals">
        <EmptyState title="Market data for this company is not loaded yet">
          Fundamentals use the market data provider's statements and quote.{" "}
          <button type="button" className="btn btn-ghost btn-sm" onClick={loadCompanyData} disabled={loadingData}>
            <RefreshCw size={14} className={loadingData ? "spin" : ""} aria-hidden="true" /> {loadingData ? "Loading…" : "Load market data"}
          </button>
        </EmptyState>
      </Card>
    );
  }
  if (state.error) return <ErrorState message={state.error} onRetry={() => load()} />;

  const view = buildFundamentalsView(state.data, market);
  const currencyNote =
    view.statementCurrency && view.quoteCurrency && view.statementCurrency !== view.quoteCurrency
      ? ` Statements are in ${view.statementCurrency}; the quote is in ${view.quoteCurrency}.`
      : view.statementCurrency
        ? ` Amounts in ${view.statementCurrency}.`
        : "";

  return (
    <div className="tab-panel">
      <Card
        title="Fundamentals"
        subtitle={`${view.available} of ${view.total} metrics available · ${view.period ? `latest annual period ${view.period}` : "no complete annual period stored"}${currencyNote}`}
      >
        <p className="small muted">
          Ratios are computed by InsightIQ from {view.statementProvider || "provider"} statements; market cap, P/E, P/B and dividend yield come from the
          latest {view.quoteProvider || "provider"} quote{state.data?.quote_fetched_at ? ` (${relativeTime(state.data.quote_fetched_at)})` : ""}. Nothing is
          estimated: a value that cannot be computed from reported data is shown as not available, with the reason.
        </p>
      </Card>
      {view.groups.map((group) => (
        <Card key={group.key} title={group.label}>
          <div className="fund-grid">
            {group.metrics.map((m) => (
              <div key={m.key} className={`fund-metric${m.available ? "" : " unavailable"}`}>
                <div className="small muted">{m.label}</div>
                <div className="fund-value">{m.display}</div>
                <div className="small muted" title={m.formula}>
                  {m.available ? m.formula : m.reason}
                </div>
              </div>
            ))}
          </div>
        </Card>
      ))}
      {!view.groups.length && <EmptyState title="No fundamentals available" />}
    </div>
  );
}
