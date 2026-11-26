import { Fragment, useCallback, useEffect, useState } from "react";
import { Badge, Card, EmptyState, ErrorState, Skeleton, StatusBadge } from "./ui.jsx";
import { api } from "../lib/api.js";
import { formatCount, formatDateTime, humanize } from "../lib/format.js";
import { latencyRows, ms, observabilitySummary, tokenRows } from "../lib/operationsModel.js";

function TraceSpans({ token, traceId }) {
  const [state, setState] = useState({ loading: true, error: "", spans: [] });
  useEffect(() => {
    const controller = new AbortController();
    api
      .operationsTrace(token, traceId, controller.signal)
      .then((d) => setState({ loading: false, error: "", spans: d.spans || [] }))
      .catch((e) => e?.name !== "AbortError" && setState({ loading: false, error: e.message, spans: [] }));
    return () => controller.abort();
  }, [token, traceId]);
  if (state.loading) return <Skeleton lines={2} />;
  if (state.error) return <p className="small muted">{state.error}</p>;
  const depth = {};
  for (const s of state.spans) depth[s.span_id] = s.parent_span_id ? (depth[s.parent_span_id] ?? 0) + 1 : 0;
  return (
    <div className="table-scroll">
      <table className="mini-table">
        <thead>
          <tr>
            <th>Span</th>
            <th>Kind</th>
            <th>Status</th>
            <th className="num">Duration</th>
            <th className="num">Tokens in / out</th>
          </tr>
        </thead>
        <tbody>
          {state.spans.map((s) => (
            <tr key={s.span_id}>
              <td style={{ paddingLeft: `${8 + 14 * (depth[s.span_id] || 0)}px` }}>{s.name}</td>
              <td>{humanize(s.kind)}</td>
              <td>
                <StatusBadge status={s.status === "ok" ? "completed" : s.status === "error" ? "failed" : "pending"} label={humanize(s.status)} />
              </td>
              <td className="num">{ms(s.duration_ms)}</td>
              <td className="num">
                {s.input_tokens == null && s.output_tokens == null ? "—" : `${formatCount(s.input_tokens)} / ${formatCount(s.output_tokens)}`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function OperationsPanel({ token }) {
  const [state, setState] = useState({ loading: true, error: "", data: null, unavailable: false });
  const [openTrace, setOpenTrace] = useState(null);

  const load = useCallback(
    (signal) =>
      api
        .operations(token, signal)
        .then((data) => setState({ loading: false, error: "", data, unavailable: false }))
        .catch((e) => {
          if (e?.name === "AbortError") return;
          setState({ loading: false, error: e.status === 404 ? "" : e.message, data: null, unavailable: e.status === 404 });
        }),
    [token]
  );

  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal);
    return () => controller.abort();
  }, [load]);

  if (state.loading) return <Card title="Operations"><Skeleton lines={4} /></Card>;
  if (state.unavailable) return <Card title="Operations"><EmptyState title="Operational telemetry needs the PostgreSQL backend" /></Card>;
  if (state.error) return <ErrorState message={state.error} onRetry={() => load()} />;

  const d = state.data;
  const latency = latencyRows(d.latency);
  const tokens = tokenRows(d.tokens);
  const runs = d.runs || {};
  const obs = observabilitySummary(d);
  const backup = d.last_backup;

  return (
    <Fragment>
      <section className="obs-strip card" aria-label="OpenTelemetry observability">
        <div className="obs-item">
          <span className="obs-label">Research runs ({runs.window_days} d)</span>
          <strong>{obs.runs == null ? "—" : formatCount(obs.runs)}</strong>
        </div>
        <div className="obs-item">
          <span className="obs-label">Success rate</span>
          <strong>{obs.successRate == null ? "—" : `${(obs.successRate * 100).toFixed(1)}%`}</strong>
        </div>
        <div className="obs-item">
          <span className="obs-label">Failed runs</span>
          <strong className={obs.failed ? "tone-negative" : undefined}>{formatCount(obs.failed)}</strong>
        </div>
        <div className="obs-item">
          <span className="obs-label">Run P50 / P95</span>
          <strong>{obs.p50 == null ? "—" : `${obs.p50.toFixed(0)} s / ${obs.p95.toFixed(0)} s`}</strong>
        </div>
        <div className="obs-item">
          <span className="obs-label">AI tokens ({d.tokens?.window_days} d)</span>
          <strong title="Provider-reported tokens only">{obs.tokens == null ? "Not reported" : formatCount(obs.tokens)}</strong>
        </div>
        <div className="obs-item">
          <span className="obs-label">Guardrails</span>
          {obs.guardrail ? (
            <span className="obs-guard">
              <StatusBadge
                status={obs.guardrail.rejected ? "unavailable" : "completed"}
                label={obs.guardrail.passRate == null ? "—" : `${(obs.guardrail.passRate * 100).toFixed(0)}% passed`}
              />
              <span className="small muted">
                {formatCount(obs.guardrail.rejected)} rejected · {formatCount(obs.guardrail.blocked)} blocked
              </span>
            </span>
          ) : (
            <strong className="muted">No checks yet</strong>
          )}
        </div>
        <div className="obs-item">
          <span className="obs-label">Open alerts</span>
          <strong className={d.alerts.length ? "tone-negative" : undefined}>{formatCount(d.alerts.length)}</strong>
        </div>
      </section>

      <div className="grid-2">
        <Card title="Alerts" subtitle="Evaluated on measured telemetry; stored until resolved">
          {!d.alerts.length ? (
            <EmptyState title="No open alerts" />
          ) : (
            <ul className="policy-list">
              {d.alerts.map((a) => (
                <li key={a.id}>
                  <span>{a.message}</span>
                  <span className="muted small">since {formatDateTime(a.first_seen_at)} · ×{a.occurrences}</span>
                  <Badge tone={a.severity === "critical" ? "negative" : "warning"}>{humanize(a.severity)}</Badge>
                </li>
              ))}
            </ul>
          )}
          <p className="small muted">
            Last database backup:{" "}
            {backup ? `${humanize(backup.status)} ${formatDateTime(backup.finished_at || backup.started_at)}${backup.restore_verified ? " · restore verified" : ""}` : "none recorded"}
          </p>
        </Card>

        <Card title="Latency" subtitle={`p50 / p95 of measured spans, last ${d.latency.window_hours} h`}>
          {!latency.length ? (
            <EmptyState title="No spans recorded yet" />
          ) : (
            <table className="mini-table">
              <thead>
                <tr>
                  <th>Kind</th>
                  <th className="num">Count</th>
                  <th className="num">p50</th>
                  <th className="num">p95</th>
                  <th className="num">Errors</th>
                </tr>
              </thead>
              <tbody>
                {latency.map((r) => (
                  <tr key={r.kind}>
                    <td>{r.label}</td>
                    <td className="num">{formatCount(r.count)}</td>
                    <td className="num">{ms(r.p50_ms)}</td>
                    <td className="num">{ms(r.p95_ms)}</td>
                    <td className="num">{r.errors ? `${r.errors} (${(r.error_rate * 100).toFixed(1)}%)` : "0"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      </div>

      <Card title="Model usage" subtitle={`Provider-reported tokens, last ${d.tokens.window_days} days (live traces and saved reports)`}>
        {!tokens.length ? (
          <EmptyState title="No model calls recorded" />
        ) : (
          <div className="table-scroll">
            <table className="mini-table">
              <thead>
                <tr>
                  <th>Provider · model</th>
                  <th className="num">Calls</th>
                  <th className="num">Input</th>
                  <th className="num">Output</th>
                  <th className="num">Reasoning</th>
                  <th>Source</th>
                </tr>
              </thead>
              <tbody>
                {tokens.map((m) => (
                  <tr key={`${m.provider}|${m.model}`}>
                    <td>{m.label}</td>
                    <td className="num">
                      {formatCount(m.calls)}
                      {m.calls_with_usage < m.calls ? <span className="muted small"> ({formatCount(m.calls_with_usage)} with usage)</span> : null}
                    </td>
                    <td className="num">{formatCount(m.input_tokens)}</td>
                    <td className="num">{formatCount(m.output_tokens)}</td>
                    <td className="num">{formatCount(m.reasoning_tokens)}</td>
                    <td className="small">{m.sources.map(humanize).join(", ")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title="Recent traces" subtitle="API requests, research runs and their database, model, forecast and PDF spans">
        {!d.recent_traces.length ? (
          <EmptyState title="No traces recorded yet" />
        ) : (
          <div className="table-scroll">
            <table className="mini-table">
              <thead>
                <tr>
                  <th>Started (UTC)</th>
                  <th>Root</th>
                  <th className="num">Spans</th>
                  <th className="num">Duration</th>
                  <th>Errors</th>
                </tr>
              </thead>
              <tbody>
                {d.recent_traces.map((t) => (
                  <Fragment key={t.trace_id}>
                    <tr className="clickable" onClick={() => setOpenTrace(openTrace === t.trace_id ? null : t.trace_id)}>
                      <td>{formatDateTime(t.started_at)}</td>
                      <td>
                        {t.root_name || "—"} <code className="code-inline">{t.trace_id.slice(0, 8)}</code>
                      </td>
                      <td className="num">{formatCount(t.spans)}</td>
                      <td className="num">{ms(t.root_ms)}</td>
                      <td>{t.errors ? <Badge tone="negative">{t.errors}</Badge> : "0"}</td>
                    </tr>
                    {openTrace === t.trace_id && (
                      <tr>
                        <td colSpan={5}>
                          <TraceSpans token={token} traceId={t.trace_id} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </Fragment>
  );
}
