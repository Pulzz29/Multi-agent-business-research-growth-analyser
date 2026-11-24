import { useEffect, useState } from "react";
import { TrendingUp } from "lucide-react";
import { api } from "../lib/api.js";
import { growthRows, trendView } from "../lib/growthModel.js";
import { Badge, Card, EmptyState, ErrorState, Notice, Skeleton } from "./ui.jsx";

// Renders one growth-analysis result (computed by the backend from stored
// data). Every value that cannot be computed shows "n/a" or "Insufficient
// history" with the reason, never a blank or a misleading percentage.
export function GrowthAnalysisView({ growth, compact = false }) {
  if (!growth) return <EmptyState title="Growth analysis is not available for this company." />;
  const rows = growthRows(growth);
  const trend = trendView(growth.trend);
  const stale = growth.staleness || {};
  return (
    <div className="growth">
      <div className="growth-head">
        {trend && (
          <Badge tone={trend.tone} title={trend.rule}>
            Trend: {trend.label}
          </Badge>
        )}
        <span className="small muted">
          Source: {growth.source || "stored data"}
          {growth.currency ? ` · ${growth.currency}` : ""}
          {growth.as_of ? ` · data as of ${growth.as_of}` : ""}
        </span>
      </div>
      {trend?.detail && <p className="small muted growth-trend-detail">{trend.detail}</p>}
      {(stale.notes || []).map((note) => (
        <Notice key={note} tone="warning">
          {note}
        </Notice>
      ))}
      <p className="growth-readout">{growth.readout}</p>
      <div className="table-scroll">
        <table className="data-table growth-table">
          <thead>
            <tr>
              <th scope="col">Metric</th>
              <th scope="col" className="num">
                Value
              </th>
              {!compact && <th scope="col">Periods compared</th>}
              <th scope="col">Note</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.key}>
                <th scope="row" title={row.definition}>
                  {row.label}
                </th>
                <td className={`num tone-${row.tone}`}>{row.valueText}</td>
                {!compact && <td className="small">{row.period || "—"}</td>}
                <td className="small muted">{row.reason || ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <details className="evidence">
        <summary>How these are calculated</summary>
        <div className="evidence-body small">
          <ul className="plain-list">
            {Object.entries(growth.definitions || {}).map(([key, text]) => (
              <li key={key}>{text}</li>
            ))}
            <li>A zero or negative base (a loss) gives "n/a" with the absolute change instead of a percentage.</li>
            <li>Quarterly YoY needs at least five quarters; CAGR over N years needs N + 1 fiscal years.</li>
            {trend?.rule && <li>{trend.rule}</li>}
          </ul>
        </div>
      </details>
    </div>
  );
}

// Card that loads the current growth analysis for a company page.
export function GrowthAnalysisCard({ token, ticker }) {
  const [state, setState] = useState({ loading: true, error: null, growth: null });
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setState({ loading: true, error: null, growth: null });
    api
      .growth(token, ticker, controller.signal)
      .then((body) => setState({ loading: false, error: null, growth: body.growth }))
      .catch((error) => {
        if (error.name === "AbortError") return;
        setState({ loading: false, error: error.message || "Growth analysis unavailable.", growth: null });
      });
    return () => controller.abort();
  }, [token, ticker, attempt]);

  return (
    <Card
      title="Growth analysis"
      subtitle="Revenue, net profit and EPS growth computed by InsightIQ from stored data"
      action={<TrendingUp size={16} aria-hidden="true" />}
    >
      {state.loading ? (
        <Skeleton lines={5} />
      ) : state.error ? (
        <ErrorState message={state.error} onRetry={() => setAttempt((n) => n + 1)} />
      ) : (
        <GrowthAnalysisView growth={state.growth} />
      )}
    </Card>
  );
}
