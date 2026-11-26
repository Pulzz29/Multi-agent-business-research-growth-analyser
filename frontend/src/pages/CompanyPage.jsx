import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  BarChart3,
  CalendarClock,
  FileText,
  Gauge,
  LineChart as LineIcon,
  Newspaper,
  PlayCircle,
  RefreshCw,
  TrendingUp,
  Wallet,
} from "lucide-react";
import { BarChart, LineChart } from "../components/charts.jsx";
import { FundamentalsPanel } from "../components/FundamentalsPanel.jsx";
import { GrowthAnalysisCard } from "../components/GrowthAnalysis.jsx";
import {
  Badge,
  Card,
  ClampText,
  CompanyMark,
  EmptyState,
  ErrorState,
  EvidencePanel,
  ExternalLink,
  Notice,
  Skeleton,
  Stat,
  StatusBadge,
  Tabs,
} from "../components/ui.jsx";
import { api } from "../lib/api.js";
import {
  exactUsd,
  formatBillions,
  formatCount,
  formatDate,
  formatDateTime,
  formatPct,
  formatUsd,
  formatUsdLong,
  periodRange,
  relativeTime,
  shortPeriod,
  trendTone,
} from "../lib/format.js";
import {
  METHOD_SHORT,
  METHOD_TEXT,
  REFRESH_STATUS_LABELS,
  RELEVANCE_LABELS,
  companyByTicker,
  modelLabel,
} from "../lib/reportModel.js";
import { ReportHistory, ReportViewer } from "./ReportViewer.jsx";

const PENDING = ["queued", "running"];
const toB = (v) => (v === null || v === undefined ? null : Number(v) / 1e9);
const fmtAxisB = (v, precise) => (precise ? `$${v.toFixed(2)}B` : `$${Math.round(v)}B`);

/* ------------------------------------------------------------ data refresh */

const SOURCE_LABELS = { sec: "SEC EDGAR financial filings", news: "NewsData.io company news" };
const STATE_LABELS = { fresh: "Within freshness policy", stale: "Older than freshness policy", never: "Never refreshed" };
const STATE_TONES = { fresh: "positive", stale: "warning", never: "warning" };
const RESULT_TONES = {
  refreshed: "positive",
  no_new_filings: "positive",
  cached_fresh: "info",
  recently_refreshed: "info",
  busy: "warning",
  failed: "negative",
};

function DataRefreshPanel({ token, ticker, onChanged, onAuthError }) {
  const [state, setState] = useState({ loading: true, error: "", freshness: null });
  const [busy, setBusy] = useState(null);
  const [outcome, setOutcome] = useState(null);
  const inFlight = useRef(false);

  const load = useCallback(
    (signal) =>
      api
        .freshness(token, ticker, signal)
        .then((freshness) => setState({ loading: false, error: "", freshness }))
        .catch((err) => {
          if (err?.name === "AbortError") return;
          if (err.status === 401) onAuthError?.();
          setState({ loading: false, error: err.message, freshness: null });
        }),
    [token, ticker, onAuthError]
  );

  useEffect(() => {
    const controller = new AbortController();
    setOutcome(null);
    setState({ loading: true, error: "", freshness: null });
    load(controller.signal);
    return () => controller.abort();
  }, [load]);

  async function refresh(sources, force, label) {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(label);
    setOutcome(null);
    try {
      const result = await api.refreshSources(token, ticker, sources, force);
      setOutcome({ ok: true, result });
      setState((s) => ({ ...s, freshness: result.freshness }));
      onChanged?.();
    } catch (err) {
      if (err.status === 401) onAuthError?.();
      setOutcome({ ok: false, message: err.message });
    } finally {
      inFlight.current = false;
      setBusy(null);
    }
  }

  const f = state.freshness;
  return (
    <Card
      title="Source data"
      subtitle="Refreshing data does not run AI research and does not change saved reports."
      className="refresh-panel"
      action={
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={() => refresh(["sec", "news"], false, "stale")}
          disabled={!!busy}
          title="Contact providers only for sources older than the freshness policy"
        >
          <RefreshCw size={14} className={busy === "stale" ? "spin" : ""} aria-hidden="true" />
          {busy === "stale" ? "Checking…" : "Refresh stale sources"}
        </button>
      }
    >
      {state.error && <Notice tone="error">Source status unavailable: {state.error}</Notice>}
      {state.loading && <Skeleton lines={2} />}
      {f && (
        <div className="table-scroll">
          <table className="mini-table">
            <thead>
              <tr>
                <th>Source</th>
                <th>Status</th>
                <th>Latest data</th>
                <th>Last successful refresh</th>
                <th>
                  <span className="sr-only">Action</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {["sec", "news"].map((key) => {
                const src = f[key] || {};
                const latest =
                  key === "sec"
                    ? `Quarter to ${formatDate(src.latest_financial_period_end?.revenue)}${src.latest_periodic_filing ? ` · ${src.latest_periodic_filing.form} filed ${formatDate(src.latest_periodic_filing.filed_at)}` : ""}`
                    : `${formatCount(src.article_count)} articles · newest ${formatDate(src.latest_published_at)}`;
                return (
                  <tr key={key}>
                    <td>{SOURCE_LABELS[key]}</td>
                    <td>
                      <Badge tone={STATE_TONES[src.state] || "neutral"}>{STATE_LABELS[src.state] || src.state}</Badge>
                      {src.refresh_in_progress && <div className="small muted">Refresh in progress</div>}
                      {src.last_attempt_failed && <div className="small muted">Last attempt failed ({src.last_failure_category || "error"})</div>}
                    </td>
                    <td className="small">{latest}</td>
                    <td className="small">
                      {src.last_successful_refresh_at ? `${formatDateTime(src.last_successful_refresh_at)} (${relativeTime(src.last_successful_refresh_at)})` : "Never"}
                      <div className="muted">Policy: {src.policy_max_age_hours} h</div>
                    </td>
                    <td>
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm"
                        onClick={() => refresh([key], true, key)}
                        disabled={!!busy}
                        title={key === "sec" ? "Check SEC EDGAR for newer filings now" : "Fetch new articles now (uses NewsData.io quota)"}
                      >
                        <RefreshCw size={14} className={busy === key ? "spin" : ""} aria-hidden="true" />
                        {busy === key ? "Refreshing…" : "Refresh now"}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {outcome && !outcome.ok && <Notice tone="error">Refresh could not run: {outcome.message} Stored data is unchanged.</Notice>}
      {outcome?.ok && (
        <ul className="plain-list small refresh-results" aria-live="polite">
          {Object.entries(outcome.result.results || {}).map(([key, r]) => (
            <li key={key}>
              <Badge tone={RESULT_TONES[r.status] || "neutral"}>{REFRESH_STATUS_LABELS[r.status] || r.status}</Badge>{" "}
              {SOURCE_LABELS[key === "SEC" ? "sec" : key]}: {r.message || ""}
              {r.status === "refreshed" && ` ${formatCount(r.records_inserted)} new record${r.records_inserted === 1 ? "" : "s"} stored.`}
            </li>
          ))}
          <li className="muted">
            Forecast inputs:{" "}
            {outcome.result.forecast_inputs?.sec_facts_changed
              ? "SEC data changed — forecasts will be recomputed on the next research run."
              : "unchanged — stored forecasts remain valid."}
          </li>
        </ul>
      )}
    </Card>
  );
}

export function ResearchOptions({ options, onChange }) {
  return (
    <details className="research-options">
      <summary>Research options</summary>
      <label className="option-row">
        <input
          type="checkbox"
          checked={options.refresh === "if_stale"}
          onChange={(e) => onChange({ ...options, refresh: e.target.checked ? "if_stale" : "skip" })}
        />
        Refresh stale SEC and news data first
      </label>
      <label className="option-row">
        If a refresh fails:
        <select
          value={options.on_refresh_failure}
          onChange={(e) => onChange({ ...options, on_refresh_failure: e.target.value })}
          disabled={options.refresh !== "if_stale"}
        >
          <option value="continue_with_cached">continue with stored data (labelled)</option>
          <option value="fail">stop the research run</option>
        </select>
      </label>
    </details>
  );
}

/* ------------------------------------------------------------------ header */

function Freshness({ data, runs }) {
  const f = data?.freshness || {};
  const latestRun = runs?.runs?.[0];
  const items = [
    { label: "Latest financial quarter", value: f.latest_financial_period_end ? formatDate(f.latest_financial_period_end) : "Unavailable", hint: f.sec_latest_filed_at ? `Latest filing ${formatDate(f.sec_latest_filed_at)}` : null },
    { label: "Latest news article", value: f.news?.latest_published_at ? formatDate(f.news.latest_published_at) : "None stored", hint: f.news?.latest_retrieved_at ? `Retrieved ${relativeTime(f.news.latest_retrieved_at)}` : null },
    { label: "Latest saved report", value: f.latest_completed_report ? formatDate(f.latest_completed_report.generated_at_utc) : "None", hint: f.latest_completed_report ? relativeTime(f.latest_completed_report.generated_at_utc) : "Start research to create one" },
    { label: "Last research run", value: latestRun ? <StatusBadge status={latestRun.status} /> : "None", hint: latestRun ? formatDateTime(latestRun.created_at_utc || latestRun.report_generated_at_utc) : null },
  ];
  return (
    <dl className="freshness">
      {items.map((item) => (
        <div key={item.label}>
          <dt>{item.label}</dt>
          <dd>{item.value}</dd>
          {item.hint && <dd className="freshness-hint">{item.hint}</dd>}
        </div>
      ))}
    </dl>
  );
}

function CompanyHeader({ ticker, data, runs, actions }) {
  const company = companyByTicker(ticker);
  const forecastsAvailable = data && Object.values(data.forecasts || {}).some((f) => f.available);
  return (
    <header className="company-head card">
      <div className="company-head-top">
        <CompanyMark ticker={ticker} name={company?.legal || company?.name} size="lg" />
        <div className="company-head-title">
          <p className="eyebrow">Company dashboard</p>
          <h1>
            {company?.legal || ticker} <span className="ticker-chip">{ticker}</span>
          </h1>
          <div className="company-head-badges">
            {data?.company?.cik && <Badge tone="neutral">CIK {data.company.cik}</Badge>}
            {data && (
              <Badge tone={data.freshness?.latest_completed_report ? "positive" : "neutral"}>
                {runs ? `${runs.completed_report_count} saved report${runs.completed_report_count === 1 ? "" : "s"}` : "Reports…"}
              </Badge>
            )}
            {data && <Badge tone={forecastsAvailable ? "info" : "neutral"}>{forecastsAvailable ? "Forecasts available" : "No forecasts"}</Badge>}
            {data && <Badge tone="neutral">{formatCount(data.freshness?.sec_fact_count)} SEC facts</Badge>}
          </div>
        </div>
        <div className="company-head-actions">{actions}</div>
      </div>
      {data && <Freshness data={data} runs={runs} />}
    </header>
  );
}

/* --------------------------------------------------------------- overview */

function latest(series) {
  return series?.length ? series[series.length - 1] : null;
}

function growthSub(row) {
  if (!row) return null;
  const qoq =
    row.qoq_change_pct === null || row.qoq_change_pct === undefined ? "QoQ n/a" : `${formatPct(row.qoq_change_pct)} QoQ`;
  return `${row.fiscal_label} · ${periodRange(row.period_start, row.period_end)} · ${qoq}`;
}

// Headline cards describe one common quarter (the latest one stored for every
// metric), so revenue and cash flow are never compared across periods.
function atCommonPeriod(series, commonEnd) {
  if (!series?.length) return null;
  if (commonEnd) return series.find((r) => r.period_end === commonEnd) || null;
  return latest(series);
}

function yoyValue(row) {
  if (!row) return "Unavailable";
  if (row.yoy_change_pct === null || row.yoy_change_pct === undefined) return row.yoy_reason ? "n/a" : "Unavailable";
  return formatPct(row.yoy_change_pct);
}

function yoySub(row) {
  if (row?.yoy_reason) return row.yoy_reason;
  return row?.yoy_basis ? `vs. quarter ending ${formatDate(row.yoy_basis)}` : "No comparable prior-year quarter";
}

function OverviewTab({ data, runs, navigate, ticker }) {
  const commonEnd = data.freshness?.latest_common_period_end;
  const rev = atCommonPeriod(data.financials.revenue, commonEnd);
  const ocf = atCommonPeriod(data.financials.operating_cash_flow, commonEnd);
  const fRev = data.forecasts.revenue;
  const lastReport = runs?.runs?.find((r) => r.report_available);

  const bars = useMemo(() => {
    const revenue = data.financials.revenue.slice(-8);
    const byEnd = Object.fromEntries(data.financials.operating_cash_flow.map((r) => [r.period_end, r]));
    return revenue.map((r) => ({
      key: r.period_end,
      label: shortPeriod(r.period_end),
      fullLabel: `${r.fiscal_label} · ${periodRange(r.period_start, r.period_end)}`,
      values: [
        { id: "rev", label: "Revenue", tone: "primary", value: toB(r.value_usd), title: `Revenue ${exactUsd(r.value_usd)}` },
        { id: "ocf", label: "Operating cash flow", tone: "secondary", value: toB(byEnd[r.period_end]?.value_usd), title: byEnd[r.period_end] ? `OCF ${exactUsd(byEnd[r.period_end].value_usd)}` : "OCF unavailable" },
      ],
    }));
  }, [data]);

  return (
    <div className="tab-panel">
      {data.freshness?.stale && (
        <Notice tone="warning" title="Old financial data">
          The latest quarter stored from SEC filings ended {formatDate(data.freshness.latest_financial_period_end)} (
          {data.freshness.latest_period_age_days} days ago). The figures below describe that period, not the current one.
        </Notice>
      )}
      <div className="stats-row">
        <Stat label="Quarterly revenue" icon={TrendingUp} value={rev ? formatUsd(rev.value_usd) : "Unavailable"} title={rev ? exactUsd(rev.value_usd) : undefined} sub={growthSub(rev)} />
        <Stat label="Quarterly operating cash flow" icon={Wallet} value={ocf ? formatUsd(ocf.value_usd) : "Unavailable"} title={ocf ? exactUsd(ocf.value_usd) : undefined} sub={growthSub(ocf)} />
        <Stat
          label="Revenue growth (YoY)"
          icon={BarChart3}
          value={yoyValue(rev)}
          tone={trendTone(rev?.yoy_change_pct)}
          sub={yoySub(rev)}
        />
        <Stat
          label="Cash-flow growth (YoY)"
          icon={BarChart3}
          value={yoyValue(ocf)}
          tone={trendTone(ocf?.yoy_change_pct)}
          sub={yoySub(ocf)}
        />
        <Stat
          label="Forecast horizon"
          icon={CalendarClock}
          value={fRev?.available ? `${fRev.horizon_quarters} quarters` : "Unavailable"}
          sub={fRev?.available ? `Estimates after ${formatDate(fRev.data_cutoff)} (statistical, not reported)` : "No stored forecast"}
        />
        <Stat
          label="Latest research"
          icon={FileText}
          value={lastReport ? "Completed" : "None"}
          sub={lastReport ? formatDateTime(lastReport.report_generated_at_utc) : "Start research to create a report"}
        />
      </div>

      <div className="grid-2">
        <Card title="Revenue and operating cash flow" subtitle="Last eight standalone quarters, USD billions">
          <BarChart
            data={bars}
            yFormat={fmtAxisB}
            ariaLabel="Revenue and operating cash flow, last eight quarters"
            legend={[
              { id: "rev", label: "Revenue", tone: "primary" },
              { id: "ocf", label: "Operating cash flow", tone: "secondary" },
            ]}
          />
        </Card>

        <Card
          title="Latest saved research"
          subtitle={lastReport ? `Generated ${formatDateTime(lastReport.report_generated_at_utc)}` : "No report yet"}
          action={
            lastReport && (
              <button type="button" className="btn btn-primary btn-sm" onClick={() => navigate({ page: "company", ticker, tab: "research", runId: lastReport.run_id })}>
                Open report
              </button>
            )
          }
        >
          {lastReport ? (
            <div className="report-teaser">
              <div className="teaser-counts">
                <div>
                  <strong>{lastReport.counts?.financial_claims ?? "—"}</strong>
                  <span>Financial findings</span>
                </div>
                <div>
                  <strong>{lastReport.counts?.news_articles ?? "—"}</strong>
                  <span>News evidence</span>
                </div>
                <div>
                  <strong>{lastReport.counts?.strategic_priorities ?? "—"}</strong>
                  <span>Strategic priorities</span>
                </div>
              </div>
              <p className="muted small">
                Executive summary: {lastReport.summary_status === "completed" ? "available" : lastReport.summary_status === "unavailable" ? "generation failed — can be retried" : "not generated yet"}.
                {lastReport.in_database ? "" : " Recovered from the saved report file."}
              </p>
            </div>
          ) : (
            <EmptyState title="No saved research for this company">
              Financials, forecasts and news below are available without research. Use <strong>Start research</strong> to run the Financial, News and Strategic agents.
            </EmptyState>
          )}
        </Card>
      </div>

      <Card
        title="Recent company news"
        subtitle="Stored NewsData.io articles (claims not independently verified)"
        action={
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => navigate({ page: "company", ticker, tab: "news" })}>
            All news
          </button>
        }
      >
        <NewsList articles={data.news.slice(0, 3)} compact />
      </Card>
    </div>
  );
}

/* -------------------------------------------------------------- financials */

const RANGES = [
  { id: "3y", label: "3Y", quarters: 12 },
  { id: "5y", label: "5Y", quarters: 20 },
  { id: "10y", label: "10Y", quarters: 40 },
  { id: "all", label: "All", quarters: Infinity },
];

function Delta({ value, basis, kind, reason }) {
  if (value === null || value === undefined) {
    if (reason) {
      // Not meaningful (zero or negative base): say so instead of a percentage.
      return (
        <span className="muted" title={reason}>
          n/a
        </span>
      );
    }
    return (
      <span className="muted" title={kind === "QoQ" ? "No contiguous previous quarter" : "No same fiscal quarter one year earlier"}>
        —
      </span>
    );
  }
  return (
    <span className={`tone-${trendTone(value)}`} title={basis ? `${kind} vs. quarter ending ${formatDate(basis)}` : undefined}>
      {formatPct(value)}
    </span>
  );
}

function evidenceRows(row) {
  return [
    ["Period", `${row.period_start} to ${row.period_end} (${row.days} days)`],
    ["Exact value", exactUsd(row.value_usd, row.unit)],
    ["SEC concept", row.concept],
    ["Derivation", METHOD_TEXT[row.method] || row.method],
  ];
}

function FactSources({ evidence }) {
  return (
    <table className="mini-table">
      <thead>
        <tr>
          <th>Fact ID</th>
          <th>Form</th>
          <th>Period</th>
          <th>Value basis</th>
          <th>Filed</th>
          <th>Accession</th>
        </tr>
      </thead>
      <tbody>
        {evidence.map((e) => (
          <tr key={e.sec_fact_id}>
            <td>{e.sec_fact_id}</td>
            <td>{e.form}</td>
            <td>
              {e.period_start} → {e.period_end}
            </td>
            <td>
              FY{e.fiscal_year_reported} {e.fiscal_period_reported}
              {e.sec_frame ? ` · ${e.sec_frame}` : ""}
            </td>
            <td>{e.filed_at}</td>
            <td>
              <ExternalLink href={e.source_url}>{e.accession}</ExternalLink>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function FinancialsTab({ data, token, ticker }) {
  const [range, setRange] = useState("5y");
  const quarters = RANGES.find((r) => r.id === range).quarters;
  const revenue = data.financials.revenue;
  const ocf = data.financials.operating_cash_flow;
  const ocfByEnd = useMemo(() => Object.fromEntries(ocf.map((r) => [r.period_end, r])), [ocf]);
  const revByEnd = useMemo(() => Object.fromEntries(revenue.map((r) => [r.period_end, r])), [revenue]);

  const allEnds = useMemo(() => [...new Set([...revenue, ...ocf].map((r) => r.period_end))].sort(), [revenue, ocf]);
  const ends = allEnds.slice(-Math.min(quarters, allEnds.length));
  const endSet = new Set(ends);

  const series = [
    { id: "rev", label: "Revenue", tone: "primary", points: revenue.filter((r) => endSet.has(r.period_end)).map((r) => ({ x: r.period_end, y: toB(r.value_usd) })) },
    { id: "ocf", label: "Operating cash flow", tone: "secondary", points: ocf.filter((r) => endSet.has(r.period_end)).map((r) => ({ x: r.period_end, y: toB(r.value_usd) })) },
  ];

  const xFormat = (x) => {
    const row = revByEnd[x] || ocfByEnd[x];
    return row ? `${row.fiscal_label} · ${periodRange(row.period_start, row.period_end)}` : x;
  };

  const discrepancies = (data.normalization?.diagnostics || []).filter((d) => d.code === "direct_ytd_discrepancy");
  const otherDiagnostics = (data.normalization?.diagnostics || []).filter((d) => d.code !== "direct_ytd_discrepancy");
  const tableEnds = [...ends].reverse();

  return (
    <div className="tab-panel">
      <GrowthAnalysisCard token={token} ticker={ticker} />
      <Card
        title="Quarterly revenue and operating cash flow"
        subtitle="Standalone fiscal quarters from SEC filings, USD billions. Actual reported or derived results only."
        action={
          <div className="segmented" role="group" aria-label="Chart range">
            {RANGES.map((r) => (
              <button key={r.id} type="button" className={range === r.id ? "is-active" : ""} aria-pressed={range === r.id} onClick={() => setRange(r.id)}>
                {r.label}
              </button>
            ))}
          </div>
        }
      >
        {revenue.length || ocf.length ? (
          <LineChart series={series} yFormat={fmtAxisB} xFormat={xFormat} ariaLabel="Quarterly revenue and operating cash flow" footnote="Use the mouse or arrow keys on the chart to inspect each quarter." />
        ) : (
          <EmptyState title="No normalized quarterly financials">SEC facts for this company could not be normalized into standalone quarters.</EmptyState>
        )}
      </Card>

      <Card
        title="Quarterly results"
        subtitle="QoQ compares with the immediately preceding quarter; YoY with the same fiscal quarter one year earlier. Growth is never computed across gaps."
      >
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">Fiscal quarter</th>
                <th scope="col">Period</th>
                <th scope="col" className="num">
                  Revenue
                </th>
                <th scope="col" className="num">
                  QoQ
                </th>
                <th scope="col" className="num">
                  YoY
                </th>
                <th scope="col" className="num">
                  Operating cash flow
                </th>
                <th scope="col" className="num">
                  QoQ
                </th>
                <th scope="col" className="num">
                  YoY
                </th>
                <th scope="col">Basis</th>
              </tr>
            </thead>
            <tbody>
              {tableEnds.map((end) => {
                const r = revByEnd[end];
                const o = ocfByEnd[end];
                const ref = r || o;
                return (
                  <tr key={end}>
                    <th scope="row">{ref.fiscal_label}</th>
                    <td>{periodRange(ref.period_start, ref.period_end)}</td>
                    <td className="num" title={r ? exactUsd(r.value_usd) : "Unavailable"}>
                      {r ? formatUsd(r.value_usd) : "—"}
                    </td>
                    <td className="num">
                      <Delta value={r?.qoq_change_pct} basis={r?.qoq_basis} kind="QoQ" reason={r?.qoq_reason} />
                    </td>
                    <td className="num">
                      <Delta value={r?.yoy_change_pct} basis={r?.yoy_basis} kind="YoY" reason={r?.yoy_reason} />
                    </td>
                    <td className="num" title={o ? exactUsd(o.value_usd) : "Unavailable"}>
                      {o ? formatUsd(o.value_usd) : "—"}
                    </td>
                    <td className="num">
                      <Delta value={o?.qoq_change_pct} basis={o?.qoq_basis} kind="QoQ" reason={o?.qoq_reason} />
                    </td>
                    <td className="num">
                      <Delta value={o?.yoy_change_pct} basis={o?.yoy_basis} kind="YoY" reason={o?.yoy_reason} />
                    </td>
                    <td>
                      <details className="row-evidence">
                        <summary>
                          <Badge tone={ref.is_derived ? "neutral" : "info"} title={METHOD_TEXT[ref.method]}>
                            {METHOD_SHORT[ref.method] || ref.method}
                          </Badge>
                        </summary>
                        <div className="row-evidence-body">
                          {[r, o].filter(Boolean).map((row) => (
                            <div key={row.concept + row.period_end} className="row-evidence-block">
                              <p className="row-evidence-title">{row === r ? "Revenue" : "Operating cash flow"}</p>
                              <dl className="kv">
                                {evidenceRows(row).map(([k, v]) => (
                                  <div key={k} className="kv-row">
                                    <dt>{k}</dt>
                                    <dd>{v}</dd>
                                  </div>
                                ))}
                              </dl>
                              <FactSources evidence={row.evidence} />
                            </div>
                          ))}
                        </div>
                      </details>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>

      <Card title="Data quality notes" subtitle="Normalization diagnostics recorded for this company's SEC data">
        {discrepancies.length === 0 && otherDiagnostics.length === 0 && !(data.normalization?.warnings || []).length ? (
          <EmptyState title="No normalization diagnostics recorded" />
        ) : (
          <>
            {discrepancies.length > 0 && (
              <Notice tone="info" title="Derived quarter differs slightly from a separately reported quarterly fact">
                InsightIQ derives standalone quarters from year-to-date totals so that four quarters always add up to the annual 10-K figure. Where the company also reported the quarter directly, small differences can appear (typically rounding in filings). The derived value is shown above.
              </Notice>
            )}
            {discrepancies.length > 0 && (
              <div className="table-scroll">
                <table className="mini-table">
                  <thead>
                    <tr>
                      <th>Metric</th>
                      <th>Quarter ending</th>
                      <th className="num">Derived (shown)</th>
                      <th className="num">Directly reported</th>
                      <th className="num">Difference</th>
                      <th>Direct fact</th>
                    </tr>
                  </thead>
                  <tbody>
                    {discrepancies
                      .slice()
                      .reverse()
                      .map((d) => (
                        <tr key={`${d.metric}-${d.end}`}>
                          <td>{d.metric === "revenue" ? "Revenue" : "Operating cash flow"}</td>
                          <td>{formatDate(d.end)}</td>
                          <td className="num">{exactUsd(d.derived_value)}</td>
                          <td className="num">{exactUsd(d.direct_value)}</td>
                          <td className="num">{exactUsd(String(Number(d.derived_value) - Number(d.direct_value)))}</td>
                          <td>{d.direct_fact_id}</td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              </div>
            )}
            {otherDiagnostics.length > 0 && (
              <EvidencePanel label={`Other normalization diagnostics (${otherDiagnostics.length})`}>
                <ul className="plain-list small">
                  {otherDiagnostics.map((d, i) => (
                    <li key={i}>
                      <code className="code-inline">{d.code}</code> — {d.metric}, fiscal year starting {d.fiscal_start}
                      {d.quarter ? `, Q${d.quarter}` : ""}
                    </li>
                  ))}
                </ul>
              </EvidencePanel>
            )}
          </>
        )}
      </Card>
    </div>
  );
}

/* --------------------------------------------------------------- forecasts */

function ForecastSection({ metric, label, forecast, history }) {
  if (!forecast?.available) {
    return (
      <Card title={`${label} forecast`}>
        <EmptyState title="No stored forecast">{forecast?.reason || "Forecasts are produced during research runs."}</EmptyState>
      </Card>
    );
  }

  const recent = history.slice(-12);
  const last = recent[recent.length - 1];
  const actual = recent.map((r) => ({ x: r.period_end, y: toB(r.value_usd), note: r.fiscal_label }));
  const forecastPoints = [
    ...(last ? [{ x: last.period_end, y: toB(last.value_usd), note: "Last reported quarter" }] : []),
    ...forecast.predictions.map((p) => ({ x: p.approx_period_end, y: p.predicted_billion_usd, note: `Estimate, ${p.fiscal_quarter_ahead} quarter${p.fiscal_quarter_ahead > 1 ? "s" : ""} ahead` })),
  ];
  const validation = forecast.validation || null;
  // Intervals are drawn only when the backend's out-of-sample coverage gate
  // allowed them and both bounds exist; otherwise the point estimate stands alone.
  const intervalsAllowed = Boolean(validation?.interval_display_allowed);
  const hasInterval = (p) => intervalsAllowed && Number.isFinite(p.lower_95_billion_usd) && Number.isFinite(p.upper_95_billion_usd);
  const band = forecast.predictions.filter(hasInterval).map((p) => ({ x: p.approx_period_end, lo: p.lower_95_billion_usd, hi: p.upper_95_billion_usd }));

  const bt = forecast.backtesting || {};
  const baseline = validation?.baseline_mae_billion_usd ?? null;
  const selected = validation?.selected_mae_billion_usd ?? null;
  const improvement = baseline && selected != null && forecast.selected_model !== "seasonal_naive" ? (1 - selected / baseline) * 100 : null;

  return (
    <Card
      title={`${label} forecast`}
      subtitle={`${modelLabel(forecast.selected_model, forecast.specification)} · trained on ${forecast.history_quarters} quarters to ${formatDate(forecast.data_cutoff)}`}
      action={<Badge tone="warning">Statistical estimate</Badge>}
    >
      <LineChart
        series={[
          { id: "actual", label: "Reported (actual)", tone: metric === "revenue" ? "primary" : "secondary", points: actual },
          { id: "forecast", label: "Forecast (estimate)", tone: "forecast", dashed: true, points: forecastPoints, band },
        ]}
        yFormat={fmtAxisB}
        xFormat={(x) => (x === last?.period_end ? `${last.fiscal_label} · ${periodRange(last.period_start, last.period_end)}` : `≈ quarter ending ${formatDate(x)}`)}
        ariaLabel={`${label}: reported quarters and forecast`}
        footnote={
          band.length
            ? `Forecast quarter-end dates are approximate. The shaded band is the model's 95% interval; ${validation?.interval_warning || ""}`
            : `Forecast quarter-end dates are approximate. ${validation?.interval_warning || "No validated prediction interval is available."}`
        }
      />

      <div className="grid-2 tight">
        <div>
          <h3 className="sub-title">Forecast values</h3>
          <table className="mini-table">
            <thead>
              <tr>
                <th>Horizon</th>
                <th>≈ Quarter ending</th>
                <th className="num">Estimate</th>
                <th className="num">95% interval</th>
              </tr>
            </thead>
            <tbody>
              {forecast.predictions.map((p) => (
                <tr key={p.fiscal_quarter_ahead}>
                  <td>Q+{p.fiscal_quarter_ahead}</td>
                  <td>{formatDate(p.approx_period_end)}</td>
                  <td className="num">{formatBillions(p.predicted_billion_usd)}</td>
                  <td className="num">
                    {hasInterval(p) ? `${formatBillions(p.lower_95_billion_usd)} – ${formatBillions(p.upper_95_billion_usd)}` : "Not validated"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div>
          <h3 className="sub-title">Model selection (point-in-time folds, mean absolute error)</h3>
          <table className="mini-table">
            <thead>
              <tr>
                <th>Model</th>
                <th className="num">Mean MAE</th>
                <th className="num">Folds</th>
                <th>Notes</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(bt).map(([name, b]) => (
                <tr key={name} className={name === forecast.selected_model ? "is-selected" : ""}>
                  <td>
                    {name === "seasonal_naive" ? "Seasonal naive (baseline)" : modelLabel(name, b.specification)}
                    {name === forecast.selected_model && <Badge tone="info">Selected</Badge>}
                  </td>
                  <td className="num">{b.mean_mae_billion_usd === null || b.mean_mae_billion_usd === undefined ? "Failed" : formatBillions(b.mean_mae_billion_usd)}</td>
                  <td className="num">{b.fold_count || "—"}</td>
                  <td className="small">{b.failures?.length ? `${b.failures.length} failed fold(s)` : b.mean_mae_billion_usd === null ? "Backtest failed" : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {validation ? (
            validation.evaluation_available ? (
              <p className="small muted">
                Held-out evaluation (forecast from {formatDate(validation.evaluation_origin_period_end)}, {validation.holdout_quarters} quarters not used to choose the model):
                selected model error {formatBillions(selected)} vs seasonal-naive baseline {formatBillions(baseline)}
                {improvement !== null ? ` (${Math.abs(improvement).toFixed(0)}% ${improvement >= 0 ? "lower" : "higher"})` : ""}.
              </p>
            ) : (
              <p className="small muted">Held-out evaluation unavailable: {validation.evaluation_unavailable_reason}</p>
            )
          ) : (
            <p className="small muted">This forecast predates the held-out validation method; it is recomputed on the next research run.</p>
          )}
        </div>
      </div>

      <p className="small">
        <strong>Model selection:</strong> {forecast.selection_reason}
      </p>
      <EvidencePanel
        label="Forecast details and limitations"
        rows={[
          ["Interval method", forecast.interval_method],
          ["Model generated", formatDateTime(forecast.generated_at_utc)],
          ["Stored (cached)", formatDateTime(forecast.cached_at_utc)],
          ["Data cutoff (last reported quarter)", formatDate(forecast.data_cutoff)],
          ["Final model converged", forecast.final_fit_converged],
          ["Agent reuse window", forecast.ttl_expired ? "Expired — the next research run will recompute this forecast" : "Within 24 h reuse window"],
        ]}
      >
        <ul className="plain-list small">
          {forecast.caveats.map((c) => (
            <li key={c}>{c}</li>
          ))}
        </ul>
      </EvidencePanel>
    </Card>
  );
}

function ForecastsTab({ data }) {
  return (
    <div className="tab-panel">
      <Notice tone="warning" title="Forecasts are estimates, not reported results">
        These are stored SARIMAX projections produced during earlier research runs. They are not regenerated when you open this page and are not guaranteed.
      </Notice>
      <ForecastSection metric="revenue" label="Revenue" forecast={data.forecasts.revenue} history={data.financials.revenue} />
      <ForecastSection metric="operating_cash_flow" label="Operating cash flow" forecast={data.forecasts.operating_cash_flow} history={data.financials.operating_cash_flow} />
    </div>
  );
}

/* -------------------------------------------------------------------- news */

export function NewsList({ articles, compact }) {
  if (!articles?.length) {
    return (
      <EmptyState title="No verified company-specific news is available for the selected period.">
        Only articles in which this company is the subject of the headline are shown. Use Refresh news to check NewsData.io for new articles.
      </EmptyState>
    );
  }
  return (
    <ul className={`news-list ${compact ? "is-compact" : ""}`}>
      {articles.map((a) => (
        <li key={a.evidence_id} className="news-item">
          <div className="news-meta">
            <span className="news-publisher">{a.publisher || "Unknown publisher"}</span>
            <span aria-hidden="true">·</span>
            <time dateTime={a.published_at}>{formatDateTime(a.published_at)}</time>
            {a.relevance_reason && <Badge tone="neutral">{RELEVANCE_LABELS[a.relevance_reason] || a.relevance_reason}</Badge>}
          </div>
          <h3 className="news-title">
            <ExternalLink href={a.source_url}>{a.title}</ExternalLink>
          </h3>
          {!compact && <ClampText className="news-desc" label="Article summary (publisher feed):" text={a.description} />}
          {!compact && (
            <EvidencePanel
              label="Source details"
              rows={[
                ["Evidence ID", a.evidence_id],
                ["Provider", a.provider],
                ["Relevance score", a.relevance_score],
                ["Retrieved", formatDateTime(a.retrieved_at)],
                ["URL", a.source_url],
              ]}
            />
          )}
        </li>
      ))}
    </ul>
  );
}

function NewsTab({ data }) {
  const status = data.freshness.news;
  return (
    <div className="tab-panel">
      <Card
        title="Company news"
        subtitle={`${formatCount(status.article_count)} stored articles · latest published ${formatDateTime(status.latest_published_at)} · last successful refresh ${formatDateTime(status.last_successful_refresh_at)}`}
      >
        <Notice tone="info">
          Headlines, publishers, dates and links come from stored NewsData.io records. Article content is reported by the publisher and has not been independently verified by InsightIQ.
        </Notice>
        <NewsList articles={data.news} />
      </Card>
    </div>
  );
}

/* ---------------------------------------------------------------- research */

export function AgentProgress({ job }) {
  const statuses = job?.agent_statuses || {};
  const agents = [
    { id: "financial", label: "Financial Agent", desc: "Verifies financial observations and writes the financial analysis" },
    { id: "news", label: "News Agent", desc: "Selects company news and writes source-attributed intelligence" },
    { id: "strategic", label: "Strategic Agent", desc: "Writes evidence-grounded strategic priorities" },
  ];
  return (
    <ol className="agent-steps">
      {agents.map((agent) => (
        <li key={agent.id} className={`agent-step is-${statuses[agent.id] || "pending"}`}>
          <span className="agent-step-dot" aria-hidden="true" />
          <div>
            <p className="agent-step-title">{agent.label}</p>
            <p className="agent-step-desc">{agent.desc}</p>
          </div>
          <StatusBadge status={statuses[agent.id] || "pending"} />
        </li>
      ))}
    </ol>
  );
}

export function ResearchTab({ token, ticker, runs, runId, navigate, activeJob, jobError, company, onSummaryChange }) {
  const [state, setState] = useState({ loading: false, error: "", doc: null });
  const [cancel, setCancel] = useState({ busy: false, error: "", confirming: false });
  const selectedId = runId || runs?.latest_completed_run_id || null;
  const selectedRun = runs?.runs?.find((r) => r.run_id === selectedId);
  const jobForTicker = activeJob?.ticker === ticker ? activeJob : null;
  const jobRunning = Boolean(jobForTicker && PENDING.includes(jobForTicker.status));
  // While new research runs, the previous saved report is not shown under it
  // (it stays one click away and is clearly labelled as the previous report).
  const hidePrevious = jobRunning && !runId;

  async function cancelRun() {
    if (!jobForTicker?.run_id || cancel.busy) return;
    setCancel({ busy: true, error: "", confirming: false });
    try {
      await api.cancelJob(token, jobForTicker.run_id);
      setCancel({ busy: false, error: "", confirming: false });
    } catch (error) {
      setCancel({ busy: false, error: error.message, confirming: false });
    }
  }

  const load = useCallback(
    async (signal) => {
      if (!selectedId) {
        setState({ loading: false, error: "", doc: null });
        return;
      }
      // Never keep showing a different run's report while the selected one loads.
      setState((s) => ({ loading: true, error: "", doc: s.doc?.report?.run_id === selectedId ? s.doc : null }));
      try {
        const doc = await api.companyReport(token, ticker, selectedId, signal);
        if (!signal?.aborted) setState({ loading: false, error: "", doc });
      } catch (error) {
        if (error?.name !== "AbortError") setState({ loading: false, error: error.message, doc: null });
      }
    },
    [token, ticker, selectedId]
  );

  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal);
    return () => controller.abort();
  }, [load]);

  // A just-finished run writes its executive summary shortly after completion.
  useEffect(() => {
    if (!state.doc || state.doc.summary || !jobForTicker || jobForTicker.run_id !== selectedId || jobForTicker.status !== "completed") return undefined;
    let tries = 0;
    const timer = window.setInterval(async () => {
      tries += 1;
      try {
        const doc = await api.companyReport(token, ticker, selectedId);
        // The summary is one more AI request after the report; allow ~6 minutes.
        if (doc.summary || tries >= 72) {
          window.clearInterval(timer);
          setState((s) => ({ ...s, doc }));
          if (doc.summary) onSummaryChange?.();
        }
      } catch {
        window.clearInterval(timer);
      }
    }, 5000);
    return () => window.clearInterval(timer);
  }, [state.doc, jobForTicker, selectedId, token, ticker, onSummaryChange]);

  return (
    <div className="tab-panel">
      {jobForTicker && (PENDING.includes(jobForTicker.status) || jobForTicker.status === "failed") && (
        <Card
          title={PENDING.includes(jobForTicker.status) ? "Research in progress" : "Research did not complete"}
          subtitle={`Run ${jobForTicker.run_id?.slice(0, 8) || "…"} · started ${formatDateTime(jobForTicker.created_at_utc) || "just now"}`}
          action={<StatusBadge status={jobForTicker.status} />}
        >
          <AgentProgress job={jobForTicker} />
          {jobForTicker.status === "failed" && jobForTicker.error_code === "cancelled" && (
            <Notice tone="info">This research run was cancelled. No report was saved and earlier reports are unchanged.</Notice>
          )}
          {jobForTicker.status === "failed" && jobForTicker.error_code !== "cancelled" && (
            <Notice tone="error">
              This research run failed{jobForTicker.error_code ? ` (${jobForTicker.error_code.replace(/_/g, " ")})` : ""}. No report was saved and earlier reports are unchanged. You can start another run.
            </Notice>
          )}
          {PENDING.includes(jobForTicker.status) && (
            <>
              <p className="small muted">
                This is a new research run (Azure OpenAI; each AI step can take a few minutes). The new report appears here
                automatically once it has been saved. You can keep browsing other pages meanwhile.
              </p>
              {cancel.confirming ? (
                <div className="badge-row" role="group" aria-label="Confirm cancelling the research run">
                  <span className="small">No report will be saved, and any AI request in progress is cancelled.</span>
                  <button type="button" className="btn btn-sm" onClick={cancelRun}>
                    Confirm cancel
                  </button>
                  <button type="button" className="btn btn-ghost btn-sm" onClick={() => setCancel((c) => ({ ...c, confirming: false }))}>
                    Keep running
                  </button>
                </div>
              ) : (
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={() => setCancel((c) => ({ ...c, confirming: true, error: "" }))}
                  disabled={cancel.busy || !jobForTicker.run_id}
                >
                  {cancel.busy ? "Cancelling…" : "Cancel research"}
                </button>
              )}
              {cancel.error && <Notice tone="error">Could not cancel: {cancel.error}</Notice>}
            </>
          )}
        </Card>
      )}
      {jobError && <ErrorState message={jobError} />}

      {hidePrevious && selectedId ? (
        <Card>
          <p className="small muted">
            The previous saved report ({formatDateTime(selectedRun?.report_generated_at_utc || selectedRun?.created_at_utc) || selectedId.slice(0, 8)}) is not
            shown while new research runs.{" "}
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => navigate({ page: "company", ticker, tab: "research", runId: selectedId })}>
              Open previous report
            </button>
          </p>
        </Card>
      ) : !selectedId ? (
        <Card>
          <EmptyState title="No saved research report for this company" icon={FileText}>
            Start research to run the Financial, News and Strategic agents. Financial data, forecasts and news on the other tabs are available now.
          </EmptyState>
        </Card>
      ) : state.loading && !state.doc ? (
        <Card>
          <Skeleton lines={6} />
        </Card>
      ) : state.error ? (
        <ErrorState message={`The saved report could not be loaded: ${state.error}`} onRetry={() => load()} />
      ) : state.doc ? (
        <ReportViewer
          key={selectedId}
          token={token}
          doc={state.doc}
          company={company}
          isLatest={selectedRun?.is_latest_completed}
          inDatabase={selectedRun?.in_database}
          onSummary={(summary) => {
            setState((s) => ({ ...s, doc: { ...s.doc, summary } }));
            onSummaryChange?.();
          }}
        />
      ) : null}

      <Card title="Report history" subtitle="Every recorded research run for this company. Saved reports are shown exactly as originally generated.">
        <ReportHistory runs={runs?.runs || []} selectedId={selectedId} onOpen={(id) => navigate({ page: "company", ticker, tab: "research", runId: id })} />
      </Card>
    </div>
  );
}

/* -------------------------------------------------------------------- page */

export function CompanyPage({ token, ticker, tab, runId, navigate, activeJob, jobError, onStartResearch, startBusy, onAuthError }) {
  const [data, setData] = useState(null);
  const [runs, setRuns] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [reloadKey, setReloadKey] = useState(0);
  const [news, setNews] = useState({ busy: false, message: "", tone: "info" });
  const [showRefresh, setShowRefresh] = useState(false);
  // Default mirrors the server policy: refresh stale sources, continue with labelled stored data on failure.
  const [researchOptions, setResearchOptions] = useState({ refresh: "if_stale", on_refresh_failure: "continue_with_cached" });
  const company = companyByTicker(ticker);

  const reload = useCallback(() => setReloadKey((k) => k + 1), []);

  useEffect(() => {
    if (!company) return undefined;
    const controller = new AbortController();
    setLoading(true);
    setError("");
    Promise.all([api.company(token, ticker, controller.signal), api.runs(token, ticker, controller.signal)])
      .then(([overview, history]) => {
        if (controller.signal.aborted) return;
        setData(overview);
        setRuns(history);
      })
      .catch((err) => {
        if (controller.signal.aborted || err?.name === "AbortError") return;
        if (err.status === 401) onAuthError?.();
        setError(err.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [token, ticker, reloadKey, company, onAuthError]);

  // Reset per-company UI state when switching companies.
  useEffect(() => {
    setShowRefresh(false);
    setData(null);
    setRuns(null);
    setNews({ busy: false, message: "", tone: "info" });
  }, [ticker]);

  // When a research job for this company finishes, refresh history and open its report.
  const lastStatus = useRef(activeJob ? `${activeJob.run_id}:${activeJob.status}` : null);
  useEffect(() => {
    if (!activeJob || activeJob.ticker !== ticker) return;
    const previous = lastStatus.current;
    lastStatus.current = `${activeJob.run_id}:${activeJob.status}`;
    if (previous === lastStatus.current) return;
    if (activeJob.status === "completed") {
      reload();
      navigate({ page: "company", ticker, tab: "research", runId: activeJob.run_id });
    } else if (activeJob.status === "failed") {
      reload();
    }
  }, [activeJob, ticker, reload, navigate]);

  if (!company) {
    return (
      <div className="page">
        <ErrorState message={`“${ticker}” is not one of the supported companies.`} />
      </div>
    );
  }

  const jobPending = PENDING.includes(activeJob?.status);
  const jobHere = activeJob?.ticker === ticker && jobPending;

  const actions = (
    <>
      <button
        type="button"
        className="btn btn-ghost"
        onClick={() => setShowRefresh((v) => !v)}
        aria-expanded={showRefresh}
        title="View source freshness and refresh SEC or news data without running research"
      >
        <RefreshCw size={15} aria-hidden="true" />
        {showRefresh ? "Hide source data" : "Refresh data"}
      </button>
      <button
        type="button"
        className="btn btn-primary"
        onClick={() => {
          onStartResearch(ticker, researchOptions);
          navigate({ page: "company", ticker, tab: "research" });
        }}
        disabled={jobPending || startBusy}
        title={jobPending ? `A research run for ${activeJob.ticker} is in progress` : "Run the Financial, News and Strategic agents (uses Azure OpenAI tokens; can take several minutes)"}
      >
        <PlayCircle size={16} aria-hidden="true" />
        {jobHere ? "Research running…" : jobPending ? "Another run in progress" : "Start research"}
      </button>
      <ResearchOptions options={researchOptions} onChange={setResearchOptions} />
    </>
  );

  const tabs = [
    { id: "overview", label: "Overview", icon: BarChart3 },
    { id: "financials", label: "Financials", icon: TrendingUp },
    { id: "fundamentals", label: "Fundamentals", icon: Gauge },
    { id: "forecasts", label: "Forecasts", icon: LineIcon },
    { id: "news", label: "News", icon: Newspaper, count: data?.freshness?.news?.article_count },
    { id: "research", label: "Research", icon: FileText, count: runs?.completed_report_count },
  ];

  return (
    <div className="page">
      <CompanyHeader ticker={ticker} data={data} runs={runs} actions={actions} />
      {showRefresh && <DataRefreshPanel token={token} ticker={ticker} onChanged={reload} onAuthError={onAuthError} />}
      {news.message && (
        <Notice tone={news.tone === "success" ? "success" : news.tone === "error" ? "error" : "info"}>{news.message}</Notice>
      )}

      <Tabs tabs={tabs} active={tab} onChange={(id) => navigate({ page: "company", ticker, tab: id })} label={`${company.name} sections`} />

      {error && <ErrorState message={error} onRetry={reload} />}

      {!data && loading ? (
        <div className="tab-panel">
          <div className="stats-row">
            {Array.from({ length: 4 }, (_, i) => (
              <div className="stat" key={i}>
                <Skeleton lines={2} />
              </div>
            ))}
          </div>
          <Card>
            <Skeleton height={240} />
          </Card>
        </div>
      ) : data ? (
        <>
          {tab === "overview" && <OverviewTab data={data} runs={runs} navigate={navigate} ticker={ticker} />}
          {tab === "financials" && <FinancialsTab data={data} token={token} ticker={ticker} />}
          {tab === "fundamentals" && <FundamentalsPanel token={token} market="US" symbol={ticker} onAuthError={onAuthError} />}
          {tab === "forecasts" && <ForecastsTab data={data} />}
          {tab === "news" && <NewsTab data={data} />}
          {tab === "research" && (
            <ResearchTab
              token={token}
              ticker={ticker}
              runs={runs}
              runId={runId}
              navigate={navigate}
              activeJob={activeJob}
              jobError={activeJob?.ticker === ticker ? jobError : ""}
              company={data}
              onSummaryChange={reload}
            />
          )}
        </>
      ) : null}
    </div>
  );
}
