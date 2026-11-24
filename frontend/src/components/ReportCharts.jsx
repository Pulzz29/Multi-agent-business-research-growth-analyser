import { useEffect, useState } from "react";
import { api } from "../lib/api.js";
import {
  basisOptions,
  growthChart,
  marginChart,
  priceView,
  ratioRows,
  revenueNetIncomeChart,
} from "../lib/reportCharts.js";
import { formatDateTime } from "../lib/format.js";
import { BarChart, LineChart } from "./charts.jsx";
import { GrowthAnalysisView } from "./GrowthAnalysis.jsx";
import { Card, EmptyState, ErrorState, Skeleton } from "./ui.jsx";

const BASIS_LABEL = { quarterly: "Quarterly", annual: "Annual" };

function ChartEmpty({ view }) {
  return (
    <EmptyState title={view.title}>
      <span className="small">{view.reason}</span>
    </EmptyState>
  );
}

function BasisToggle({ options, value, onChange, label }) {
  if (options.length < 2) return null;
  return (
    <div className="segmented" role="group" aria-label={label}>
      {options.map((o) => (
        <button key={o} type="button" className={value === o ? "is-active" : ""} aria-pressed={value === o} onClick={() => onChange(o)}>
          {BASIS_LABEL[o]}
        </button>
      ))}
    </div>
  );
}

function Panel({ title, sub, action, children }) {
  return (
    <section className="chart-panel">
      <div className="panel-head" style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "flex-start" }}>
        <div>
          <h4>{title}</h4>
          {sub && <p className="chart-sub">{sub}</p>}
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}

function pctAxis(v) {
  return `${Math.round(v)}%`;
}

function billionsAxis(v) {
  return Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(1);
}

export function ReportCharts({ token, ticker, runId }) {
  const [state, setState] = useState({ loading: true, error: null, data: null });
  const [attempt, setAttempt] = useState(0);
  const [finBasis, setFinBasis] = useState(null);
  const [marginBasis, setMarginBasis] = useState(null);

  useEffect(() => {
    const controller = new AbortController();
    setState({ loading: true, error: null, data: null });
    api
      .reportInsights(token, ticker, runId, controller.signal)
      .then((data) => setState({ loading: false, error: null, data }))
      .catch((error) => {
        if (error.name === "AbortError") return;
        setState({ loading: false, error: error.message || "Report charts are unavailable.", data: null });
      });
    return () => controller.abort();
  }, [token, ticker, runId, attempt]);

  const header = (
    <>
      <Card id="charts" eyebrow="Charts" title="Financial charts" subtitle="Computed by InsightIQ from stored data as it was when this report was generated">
        {state.loading ? (
          <div className="report-charts">
            {[0, 1, 2, 3].map((i) => (
              <section key={i} className="chart-panel">
                <Skeleton lines={6} />
              </section>
            ))}
          </div>
        ) : state.error ? (
          <ErrorState message={state.error} onRetry={() => setAttempt((n) => n + 1)} />
        ) : (
          <ChartsBody data={state.data} finBasis={finBasis} setFinBasis={setFinBasis} marginBasis={marginBasis} setMarginBasis={setMarginBasis} />
        )}
      </Card>
      <Card id="growth" eyebrow="Growth analysis" title="Growth analysis" subtitle="The same figures feed the charts, the AI research and the PDF">
        {state.loading ? <Skeleton lines={5} /> : state.error ? <ErrorState message={state.error} onRetry={() => setAttempt((n) => n + 1)} /> : <GrowthAnalysisView growth={state.data.growth} />}
      </Card>
    </>
  );
  return header;
}

function ChartsBody({ data, finBasis, setFinBasis, marginBasis, setMarginBasis }) {
  const finOptions = basisOptions(data.revenue_net_income);
  const fin = finBasis && finOptions.includes(finBasis) ? finBasis : finOptions[0] || "quarterly";
  const marginOptions = basisOptions(data.margins);
  const mBasis = marginBasis && marginOptions.includes(marginBasis) ? marginBasis : marginOptions[0] || "quarterly";
  const revenue = revenueNetIncomeChart(data, fin);
  const margins = marginChart(data, mBasis);
  const growth = growthChart(data);
  const ratios = ratioRows(data);
  const price = priceView(data);

  return (
    <>
      <p className="small muted">
        Data as of {formatDateTime(data.data_as_of)} · {data.data_source} · values in {data.currency} as reported
      </p>
      <div className="report-charts">
        <Panel title="Price history" sub="Daily closing price and volume">
          <EmptyState title="Not enough data to chart">
            <span className="small">{price.reason}</span>
            {price.latest && (
              <span className="small">
                {" "}
                Latest stored quote: <strong>{price.latest.text}</strong> ({price.latest.provider}, {formatDateTime(price.latest.fetchedAt)}).
              </span>
            )}
          </EmptyState>
        </Panel>

        <Panel
          title="Revenue and net income"
          sub={`${BASIS_LABEL[fin]} · ${data.currency} billions`}
          action={<BasisToggle options={finOptions} value={fin} onChange={setFinBasis} label="Revenue period basis" />}
        >
          {revenue.empty ? (
            <ChartEmpty view={revenue} />
          ) : (
            <BarChart data={revenue.data} legend={revenue.legend} yFormat={billionsAxis} yTitle={revenue.yTitle} xTitle={revenue.xTitle} ariaLabel="Revenue and net income by period" />
          )}
        </Panel>

        <Panel
          title="Margin trends"
          sub={`${BASIS_LABEL[mBasis]} · gross, operating and net margin`}
          action={<BasisToggle options={marginOptions} value={mBasis} onChange={setMarginBasis} label="Margin period basis" />}
        >
          {margins.empty ? (
            <ChartEmpty view={margins} />
          ) : (
            <>
              <LineChart series={margins.series} yFormat={pctAxis} yTitle={margins.yTitle} xTitle={margins.xTitle} ariaLabel="Margin trends" />
              {margins.notes.map((n) => (
                <p key={n} className="small muted">
                  {n}
                </p>
              ))}
            </>
          )}
        </Panel>

        <Panel title="Growth rates" sub={growth.empty ? "Year-over-year revenue and net profit growth" : `Year-over-year growth, ${growth.basis}`}>
          {growth.empty ? (
            <ChartEmpty view={growth} />
          ) : (
            <>
              <BarChart data={growth.data} legend={growth.legend} yFormat={pctAxis} yTitle={growth.yTitle} xTitle={growth.xTitle} ariaLabel="Year-over-year growth rates" />
              {growth.notes.map((n) => (
                <p key={n} className="small muted">
                  {n}
                </p>
              ))}
            </>
          )}
        </Panel>

        <Panel title="Key ratios vs sector" sub={ratios.sector ? `${ratios.sector} sector median` : "Sector median"}>
          {ratios.empty ? (
            <ChartEmpty view={ratios} />
          ) : (
            <>
              <div className="ratio-rows" role="list">
                {ratios.rows.map((r) => (
                  <div key={r.key} role="listitem">
                    <div className="ratio-row-label">
                      <span>{r.label}</span>
                      <span>
                        <strong>{r.companyText}</strong> <span className="muted">vs {r.medianText}</span>
                      </span>
                    </div>
                    <div className="ratio-bar-track" aria-hidden="true">
                      <div className="ratio-bar is-median" style={{ width: `${r.medianWidth}%` }} />
                    </div>
                    <div className="ratio-bar-track" aria-hidden="true">
                      <div className="ratio-bar is-company" style={{ width: `${r.companyWidth}%` }} />
                    </div>
                  </div>
                ))}
              </div>
              <p className="small muted">
                Upper bar: sector median. Lower bar: this company. {ratios.basis}. {ratios.note}
              </p>
            </>
          )}
        </Panel>
      </div>
    </>
  );
}
