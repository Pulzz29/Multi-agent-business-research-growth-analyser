import { Fragment, useEffect, useState } from "react";
import { ChevronDown, ChevronRight, FileText } from "lucide-react";
import { Card, CompanyMark, EmptyState, ErrorState, Skeleton, StatusBadge } from "../components/ui.jsx";
import { api } from "../lib/api.js";
import { duration, formatCount, formatDateTime, formatReportTime, formatSeconds, humanize, shortId } from "../lib/format.js";
import { companyByTicker } from "../lib/reportModel.js";
import { runReportRoute } from "../lib/auth.js";
import { OperationsPanel } from "../components/OperationsPanel.jsx";

function TraceDetails({ token, run }) {
  const [state, setState] = useState({ loading: true, error: "", data: null });

  useEffect(() => {
    const controller = new AbortController();
    api
      .traces(token, run.run_id, controller.signal)
      .then((data) => setState({ loading: false, error: "", data }))
      .catch((error) => {
        if (error?.name === "AbortError") return;
        setState({
          loading: false,
          error:
            error.status === 404
              ? "No trace events are stored for this run. Traces exist only for runs recorded in the runtime database with tracing enabled."
              : error.message,
          data: null,
        });
      });
    return () => controller.abort();
  }, [token, run.run_id]);

  if (state.loading) return <Skeleton lines={2} />;
  if (state.error) return <p className="small muted">{state.error}</p>;
  const events = state.data?.events || [];
  if (!events.length) return <p className="small muted">No trace events were recorded for this run.</p>;

  return (
    <div className="trace">
      <p className="small muted">
        {state.data.event_count} events in {state.data.trace_count} trace{state.data.trace_count === 1 ? "" : "s"}
      </p>
      <div className="table-scroll">
        <table className="mini-table">
          <thead>
            <tr>
              <th>Time (UTC)</th>
              <th>Component</th>
              <th>Step</th>
              <th>Event</th>
              <th>Status</th>
              <th className="num">Duration</th>
              <th>Trace / span</th>
            </tr>
          </thead>
          <tbody>
            {events.map((e) => (
              <tr key={e.id}>
                <td>{String(e.timestamp_utc).slice(11, 23)}</td>
                <td>{humanize(e.component)}</td>
                <td>{humanize(e.step_name)}</td>
                <td>{humanize(e.event_type)}</td>
                <td>
                  <StatusBadge status={e.status === "ok" || e.status === "success" ? "completed" : e.status} label={humanize(e.status)} />
                </td>
                <td className="num">{e.duration_ms ? `${(e.duration_ms / 1000).toFixed(2)} s` : "—"}</td>
                <td>
                  <code className="code-inline" title={`trace ${e.trace_id} / span ${e.span_id}`}>
                    {String(e.trace_id).slice(0, 8)}/{String(e.span_id).slice(0, 6)}
                  </code>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function PerformanceRow({ label, component }) {
  const p95 = component?.p95_success_latency_ms;
  return (
    <div className="perf-row">
      <span>{label}</span>
      <span className="perf-bar" aria-hidden="true">
        <span style={{ width: `${Math.min(100, Math.max(2, ((p95 || 0) / 15000) * 100))}%` }} />
      </span>
      <strong>{p95 === null || p95 === undefined ? "—" : `${(p95 / 1000).toFixed(2)} s`}</strong>
      <span className="muted small">{formatCount(component?.completed_span_count)} spans</span>
    </div>
  );
}

export function ActivityPage({ token, runs, dashboard, loading, error, onRetry, navigate }) {
  const [expanded, setExpanded] = useState(null);
  const [filter, setFilter] = useState("ALL");
  // Filter options: companies (US and India) with a completed run that has a
  // saved report, read from PostgreSQL by the backend (nothing hard-coded).
  const [reportCompanies, setReportCompanies] = useState([]);
  useEffect(() => {
    if (!token) return undefined;
    const controller = new AbortController();
    api
      .researchCompanies(token, controller.signal)
      .then((body) => setReportCompanies(body.companies || []))
      .catch(() => {
        if (!controller.signal.aborted) setReportCompanies([]);
      });
    return () => controller.abort();
  }, [token, runs]);
  const names = Object.fromEntries(reportCompanies.map((c) => [c.ticker, c.name]));
  const secValidated = new Set(reportCompanies.filter((c) => c.sec_validated).map((c) => c.ticker));
  const isSecTicker = (ticker) => Boolean(companyByTicker(ticker)) || secValidated.has(ticker);
  const all = runs?.runs || [];
  const list = filter === "ALL" ? all : all.filter((r) => r.ticker === filter);
  const perf = dashboard?.performance?.components;
  const policies = dashboard?.reliability?.policies || [];

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <p className="eyebrow">Observability</p>
          <h1>Research activity</h1>
          <p className="page-lede">Every recorded research run with agent outcomes and timings. Expand a run for its stored trace events.</p>
        </div>
      </header>

      {error && <ErrorState message={error} onRetry={onRetry} />}


      <Card
        title="Research runs"
        subtitle="Combined from the job database, the job journal and saved report files"
        action={
          <label className="select-wrap">
            <span className="sr-only">Filter by company</span>
            <select value={filter} onChange={(e) => setFilter(e.target.value)}>
              <option value="ALL">All companies</option>
              {reportCompanies.map((c) => (
                <option key={c.ticker} value={c.ticker}>
                  {c.name && c.name !== c.ticker ? `${c.name} (${c.ticker})` : c.ticker}
                </option>
              ))}
            </select>
          </label>
        }
      >
        {loading && !runs ? (
          <Skeleton lines={5} />
        ) : !list.length ? (
          <EmptyState title="No research runs recorded" />
        ) : (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">
                    <span className="sr-only">Expand</span>
                  </th>
                  <th scope="col">Company</th>
                  <th scope="col">Run</th>
                  <th scope="col">Created</th>
                  <th scope="col">Finished</th>
                  <th scope="col">Status</th>
                  <th scope="col" className="num">
                    Financial
                  </th>
                  <th scope="col" className="num">
                    News
                  </th>
                  <th scope="col" className="num">
                    Strategic
                  </th>
                  <th scope="col" className="num">
                    Total
                  </th>
                  <th scope="col">Previous report</th>
                </tr>
              </thead>
              <tbody>
                {list.map((run) => {
                  const open = expanded === run.run_id;
                  const timing = run.agent_timings_seconds || {};
                  const total = run.total_elapsed_seconds ?? duration(run.started_at_utc, run.finished_at_utc);
                  const reportRoute = runReportRoute(run, isSecTicker);
                  return (
                    <Fragment key={run.run_id}>
                      <tr className={open ? "is-selected" : ""}>
                        <td>
                          <button
                            type="button"
                            className="icon-btn icon-btn-sm"
                            aria-expanded={open}
                            aria-label={`${open ? "Hide" : "Show"} details for run ${shortId(run.run_id)}`}
                            onClick={() => setExpanded(open ? null : run.run_id)}
                          >
                            {open ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
                          </button>
                        </td>
                        <td>
                          <span className="inline-company">
                            <CompanyMark ticker={run.ticker} size="sm" /> {companyByTicker(run.ticker)?.name || names[run.ticker] || run.ticker}
                          </span>
                        </td>
                        <td>
                          {reportRoute ? (
                            <button type="button" className="link-btn" onClick={() => navigate(reportRoute)}>
                              <code className="code-inline">{shortId(run.run_id)}</code>
                            </button>
                          ) : (
                            <code className="code-inline">{shortId(run.run_id)}</code>
                          )}
                        </td>
                        <td>{formatDateTime(run.created_at_utc || run.report_generated_at_utc)}</td>
                        <td>{formatDateTime(run.finished_at_utc)}</td>
                        <td>
                          <StatusBadge status={run.status} />
                        </td>
                        {["financial", "news", "strategic"].map((agent) => (
                          <td key={agent} className="num">
                            <span className={`agent-dot is-${run.agent_statuses?.[agent] || "pending"}`} title={run.agent_statuses?.[agent] || "unknown"} />{" "}
                            {timing[agent] !== undefined ? formatSeconds(timing[agent]) : "—"}
                          </td>
                        ))}
                        <td className="num">{total !== null && total !== undefined ? formatSeconds(total) : "—"}</td>
                        <td className="small">
                          {reportRoute ? (
                            // Opens this run's own saved report (stored data only; no AI or provider calls).
                            <button
                              type="button"
                              className="btn btn-sm"
                              onClick={() => navigate(reportRoute)}
                              aria-label={`View report for run ${shortId(run.run_id)} (${run.ticker})`}
                            >
                              <FileText size={14} aria-hidden="true" /> View report
                            </button>
                          ) : (
                            <span className="run-report-unavailable muted">
                              <span>No saved report</span>
                              <span>{run.status === "completed" ? "Report file missing" : humanize(run.status)}</span>
                            </span>
                          )}
                        </td>
                      </tr>
                      {open && (
                        <tr className="detail-row">
                          <td colSpan={11}>
                            <dl className="kv kv-inline">
                              <div className="kv-row">
                                <dt>Run ID</dt>
                                <dd>
                                  <code className="code-inline">{run.run_id}</code>
                                </dd>
                              </div>
                              <div className="kv-row">
                                <dt>Started</dt>
                                <dd>{formatDateTime(run.started_at_utc)}</dd>
                              </div>
                              <div className="kv-row">
                                <dt>Error</dt>
                                <dd>{run.error_type ? `${run.error_type} (${humanize(run.error_code)})` : "None"}</dd>
                              </div>
                              <div className="kv-row">
                                <dt>Stored in</dt>
                                <dd>{run.record_sources.map(humanize).join(", ")}</dd>
                              </div>
                              <div className="kv-row">
                                <dt>Report</dt>
                                <dd>{run.report_available ? `Saved (generated ${formatReportTime(run.report_generated_at_utc)})` : "No report saved"}</dd>
                              </div>
                            </dl>
                            <TraceDetails token={token} run={run} />
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <div className="grid-2">
        <Card title="Agent latency" subtitle="95th-percentile successful span duration from stored traces">
          {!dashboard ? (
            <Skeleton lines={4} />
          ) : (
            <div className="perf-list">
              <PerformanceRow label="Worker (end-to-end)" component={perf?.worker} />
              <PerformanceRow label="Financial Agent" component={perf?.financial} />
              <PerformanceRow label="News Agent" component={perf?.news} />
              <PerformanceRow label="Strategic Agent" component={perf?.strategic} />
              <p className="small muted">Measured token usage per model is shown under Operations below.</p>
            </div>
          )}
        </Card>
        <Card title="Reliability policies" subtitle="Evaluated over recorded jobs and workflow spans">
          {!dashboard ? (
            <Skeleton lines={4} />
          ) : !policies.length ? (
            <EmptyState title="No reliability policies reported" />
          ) : (
            <ul className="policy-list">
              {policies.map((p) => (
                <li key={p.policy}>
                  <span>{humanize(p.policy)}</span>
                  <span className="muted small">
                    {formatCount(p.observed_count)} / {formatCount(p.minimum_samples)} samples
                  </span>
                  <StatusBadge
                    status={p.state === "ok" || p.state === "healthy" ? "completed" : p.state === "insufficient_data" ? "pending" : "unavailable"}
                    label={humanize(p.state)}
                  />
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <h2 className="section-title">Operations</h2>
      <OperationsPanel token={token} />
    </div>
  );
}
