"""
Correlated tracing, structured logs, metrics, persistent alerts and audit
events, stored in PostgreSQL (tables telemetry_spans, operational_alerts,
audit_events). Only measured values are recorded; nothing is estimated.

* ``span(kind, name, **attributes)`` measures one unit of work. Spans nest
  through a context variable, so one trace links an API request to its
  database statements, financial calculations, forecasts, agents, model
  calls, validations and PDF rendering. Kinds: api, db, workflow, agent,
  financial_calculation, forecast, model_call, validation, pdf.
* ``install()`` wraps the existing entry points (no business code changes;
  the fingerprinted forecasting files stay byte-identical).
* Spans are written by one background thread in batches; telemetry never
  makes a request fail.
* Attributes never contain prompts, model output, request bodies or secrets.
"""

import contextlib
import contextvars
import functools
import hashlib
import json
import logging
import queue
import re
import statistics
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("insightiq.telemetry")
KINDS = ("api", "db", "workflow", "agent", "financial_calculation", "forecast",
         "model_call", "validation", "pdf")


@dataclass(frozen=True)
class _Ctx:
    trace_id: str
    span_id: str
    run_id: str | None


_CURRENT = contextvars.ContextVar("insightiq_trace", default=None)
_QUEUE = queue.Queue(maxsize=20000)
_WRITER = {"thread": None}
_WRITER_LOCAL = threading.local()
_GUARD = threading.Lock()
_SQL_TARGET = re.compile(r"^\s*(\w+)\b.*?\b(?:FROM|INTO|UPDATE|TABLE)\s+\"?(\w+)", re.I | re.S)


def enabled():
    from app.database import db

    try:
        return db.is_postgres()
    except Exception:
        return False


def current_trace_id():
    ctx = _CURRENT.get()
    return ctx.trace_id if ctx else None


def current_run_id():
    ctx = _CURRENT.get()
    return ctx.run_id if ctx else None


def in_writer():
    return getattr(_WRITER_LOCAL, "active", False)


@contextlib.contextmanager
def span(kind, name, trace_id=None, run_id=None, **attributes):
    """Measure a unit of work; yields a dict for extra attributes."""
    parent = _CURRENT.get()
    ctx = _Ctx(
        trace_id=(parent.trace_id if parent else None) or trace_id or uuid.uuid4().hex,
        span_id=uuid.uuid4().hex[:16],
        run_id=run_id or (parent.run_id if parent else None),
    )
    token = _CURRENT.set(ctx)
    extra = dict(attributes)
    started_wall = datetime.now(timezone.utc)
    started = time.perf_counter()
    status = "ok"
    try:
        yield extra
        status = extra.pop("_status", "ok")
    except BaseException as exc:
        status = "error"
        extra["error_type"] = type(exc).__name__
        raise
    finally:
        _CURRENT.reset(token)
        record(kind, name, ctx, parent.span_id if parent else None, status, started_wall,
               (time.perf_counter() - started) * 1000.0, extra)


def record(kind, name, ctx, parent_span_id, status, started_at, duration_ms, attributes):
    if not enabled() or in_writer():
        return
    tokens_in = attributes.pop("input_tokens", None)
    tokens_out = attributes.pop("output_tokens", None)
    row = (ctx.trace_id, ctx.span_id, parent_span_id, ctx.run_id, kind, name[:200], status,
           started_at, round(duration_ms, 3), tokens_in, tokens_out,
           json.dumps(attributes, default=str, sort_keys=True)[:4000])
    try:
        _QUEUE.put_nowait(row)
    except queue.Full:
        return  # telemetry must never slow or fail the application
    _ensure_writer()


def db_statement(sql, duration_ms, ok):
    """Called by the database layer for statements inside a trace."""
    ctx = _CURRENT.get()
    if ctx is None or in_writer():
        return
    match = _SQL_TARGET.match(sql or "")
    name = f"{match.group(1).upper()} {match.group(2)}" if match else (sql or "").strip().split(" ")[0].upper()
    record("db", name, _Ctx(ctx.trace_id, uuid.uuid4().hex[:16], ctx.run_id),
           ctx.span_id, "ok" if ok else "error", datetime.now(timezone.utc), duration_ms, {})


def _ensure_writer():
    if _WRITER["thread"] is not None and _WRITER["thread"].is_alive():
        return
    with _GUARD:
        if _WRITER["thread"] is None or not _WRITER["thread"].is_alive():
            thread = threading.Thread(target=_writer_loop, name="insightiq-telemetry", daemon=True)
            _WRITER["thread"] = thread
            thread.start()


def _writer_loop():
    _WRITER_LOCAL.active = True
    while True:
        batch = [_QUEUE.get()]
        deadline = time.monotonic() + 0.5
        while len(batch) < 500 and time.monotonic() < deadline:
            try:
                batch.append(_QUEUE.get(timeout=0.1))
            except queue.Empty:
                break
        _write(batch)


def _write(batch):
    from app.database import db

    try:
        with db.transaction() as conn:
            conn.executemany(
                "INSERT INTO telemetry_spans(trace_id, span_id, parent_span_id, run_id, kind, name, status, "
                "started_at, duration_ms, input_tokens, output_tokens, attributes_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                batch,
            )
    except Exception as exc:  # pragma: no cover - logged, never raised
        logger.warning("telemetry_write_failed %s rows=%s", type(exc).__name__, len(batch))


def flush(timeout=5.0):
    """Write everything queued so far (tests, shutdown)."""
    deadline = time.monotonic() + timeout
    while not _QUEUE.empty() and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.6)  # let the writer finish its current batch


# ------------------------------------------------------------ instrumentation

def _wrap(func, kind, name, attrs_fn=None, result_fn=None):
    if getattr(func, "__insightiq_traced__", False):
        return func

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        attrs = attrs_fn(args, kwargs) if attrs_fn else {}
        run_id = attrs.pop("run_id", None)
        with span(kind, name, run_id=run_id, **attrs) as extra:
            try:
                result = func(*args, **kwargs)
            except BaseException as exc:
                if result_fn:
                    result_fn(None, extra, exc)
                raise
            if result_fn:
                result_fn(result, extra, None)
            return result

    wrapper.__insightiq_traced__ = True
    wrapper.__wrapped_original__ = func
    return wrapper


def _replace_everywhere(original, replacement):
    """Swap a function in every loaded app module (covers `from x import f` aliases)."""
    for module in list(sys.modules.values()):
        if module is None or not getattr(module, "__name__", "").startswith("app."):
            continue
        for attr, value in list(vars(module).items()):
            if value is original:
                setattr(module, attr, replacement)


def _model_call_attrs(args, kwargs):
    return {"agent": kwargs.get("agent"), "call": kwargs.get("call_name"), "schema": kwargs.get("schema_name")}


def _model_call_result(result, extra, exc):
    diagnostics = (result[2] if result else getattr(exc, "diagnostics", None)) or {}
    extra.update({
        "provider": diagnostics.get("provider"), "model": diagnostics.get("model"),
        "api": diagnostics.get("api"), "profile": diagnostics.get("profile_used"),
        "requests": diagnostics.get("provider_requests"),
        "outcome": diagnostics.get("status") or (getattr(exc, "reason", None) if exc else None),
        "input_tokens": diagnostics.get("input_tokens"), "output_tokens": diagnostics.get("output_tokens"),
        "reasoning_tokens": diagnostics.get("reasoning_tokens"),
    })


def _traced_structured_completion(original):
    def call(*args, **kwargs):
        validate = kwargs.get("validate")
        if validate is not None and not getattr(validate, "__insightiq_traced__", False):
            def traced_validate(parsed, profile, _v=validate):
                with span("validation", f"{kwargs.get('agent')}.{kwargs.get('call_name')}",
                          profile=getattr(profile, "name", None)) as extra:
                    try:
                        return _v(parsed, profile)
                    except ValueError:
                        extra["_status"] = "rejected"
                        raise
            traced_validate.__insightiq_traced__ = True
            kwargs["validate"] = traced_validate
        return original(*args, **kwargs)

    functools.update_wrapper(call, original)
    return _wrap(call, "model_call", "structured_completion", _model_call_attrs, _model_call_result)


def install():
    """Wrap the application's entry points once per process."""
    import importlib

    targets = [
        ("app.services.ai_provider", "structured_completion", None),
        ("app.agents.research_workflow", "run_research_workflow", ("workflow", "research_workflow")),
        ("app.agents.financial_agent", "run_financial_agent", ("agent", "financial_agent")),
        ("app.agents.news_agent", "run_news_agent", ("agent", "news_agent")),
        ("app.agents.strategic_agent", "analyze_priorities", ("agent", "strategic_agent")),
        ("app.agents.provider_financial", "analyze", ("agent", "provider_financial_agent")),
        ("app.services.financial_normalization", "normalized_quarterly_financials",
         ("financial_calculation", "normalized_quarterly_financials")),
        ("app.services.growth_analysis", "company_growth", ("financial_calculation", "growth_analysis")),
        ("app.services.sector_comparison", "build_comparison", ("financial_calculation", "sector_comparison")),
        ("app.services.forecasting", "forecast_financial_metric", ("forecast", "forecast_financial_metric")),
        ("app.services.forecast_cache", "forecast_financial_metric_cached", ("forecast", "forecast_cached")),
        ("app.services.executive_summary", "validate_summary", ("validation", "validate_summary")),
        ("app.services.company_report_pdf", "render_pdf", ("pdf", "company_report_pdf")),
        ("app.services.sector_report", "render_pdf", ("pdf", "sector_report_pdf")),
        ("app.services.research_jobs", "execute_queued_research_job", ("workflow", "research_job")),
    ]
    installed = []
    for module_name, attr, spec in targets:
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue
        original = getattr(module, attr, None)
        if original is None or getattr(original, "__insightiq_traced__", False):
            continue
        if spec is None:
            replacement = _traced_structured_completion(original)
        else:
            attrs_fn = _run_id_attrs if attr == "execute_queued_research_job" else _ticker_attrs
            replacement = _wrap(original, spec[0], spec[1], attrs_fn)
        _replace_everywhere(original, replacement)
        installed.append(f"{module_name}.{attr}")
    return installed


def _ticker_attrs(args, kwargs):
    ticker = kwargs.get("ticker") or (args[0] if args and isinstance(args[0], str) else None)
    return {"ticker": ticker} if ticker else {}


def _run_id_attrs(args, kwargs):
    run_id = kwargs.get("run_id") or (args[1] if len(args) > 1 else None)
    return {"run_id": str(run_id) if run_id else None}


# ------------------------------------------------------------ structured logs

def log_event(event, **fields):
    """One JSON log line with the current trace id (no secrets: see redaction)."""
    payload = {"event": event, "ts": datetime.now(timezone.utc).isoformat(), "trace_id": current_trace_id(),
               **fields}
    logging.getLogger("insightiq.events").info(json.dumps(payload, default=str, sort_keys=True))


def client_hash(ip):
    return hashlib.sha256(f"insightiq:{ip or ''}".encode()).hexdigest()[:16] if ip else None


# ------------------------------------------------------------------ audit

def audit(action, target=None, outcome="ok", client_ip=None, details=None):
    if not enabled():
        return
    from app.database import db

    try:
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO audit_events(occurred_at, action, target, outcome, trace_id, client_hash, details_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (datetime.now(timezone.utc), action[:200], (target or "")[:200] or None, outcome,
                 current_trace_id(), client_hash(client_ip), json.dumps(details or {}, sort_keys=True)[:2000]),
            )
    except Exception as exc:
        logger.warning("audit_write_failed %s", type(exc).__name__)


# ---------------------------------------------------------------- metrics

def percentile(values, q):
    """Linear-interpolated percentile (q in 0..100) of measured values."""
    values = sorted(values)
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 3)
    position = (len(values) - 1) * q / 100.0
    low = int(position)
    high = min(low + 1, len(values) - 1)
    return round(values[low] + (values[high] - values[low]) * (position - low), 3)


def latency_summary(hours=24, now=None):
    from app.database import db

    now = now or datetime.now(timezone.utc)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT kind, name, status, duration_ms FROM telemetry_spans WHERE started_at >= ?",
            (now - timedelta(hours=hours),),
        ).fetchall()
    by_kind = {}
    for kind, name, status, duration in rows:
        by_kind.setdefault(kind, []).append((name, status, duration))
    out = []
    for kind in sorted(by_kind):
        items = by_kind[kind]
        durations = [d for _, _, d in items]
        errors = sum(1 for _, s, _ in items if s == "error")
        out.append({"kind": kind, "count": len(items), "errors": errors,
                    "error_rate": round(errors / len(items), 4),
                    "p50_ms": percentile(durations, 50), "p95_ms": percentile(durations, 95),
                    "max_ms": round(max(durations), 3)})
    return {"window_hours": hours, "span_count": len(rows), "by_kind": out}


def run_metrics(days=30, now=None):
    """Research-run outcomes and durations from research_execution_runs."""
    from app.database import db
    from app.services.data_freshness import parse_utc

    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).isoformat()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT status, started_at_utc, finished_at_utc FROM research_execution_runs "
            "WHERE created_at_utc >= ?", (since,),
        ).fetchall()
    counts = {}
    durations = []
    for status, started, finished in rows:
        counts[status] = counts.get(status, 0) + 1
        s, f = parse_utc(started), parse_utc(finished)
        if status == "completed" and s and f and f >= s:
            durations.append((f - s).total_seconds())
    finished = counts.get("completed", 0) + counts.get("failed", 0)
    return {"window_days": days, "runs": len(rows), "by_status": counts,
            "success_rate": round(counts.get("completed", 0) / finished, 4) if finished else None,
            "duration_seconds": {"p50": percentile(durations, 50), "p95": percentile(durations, 95),
                                 "mean": round(statistics.fmean(durations), 3) if durations else None,
                                 "samples": len(durations)}}


def token_usage(days=30, now=None):
    """Provider-reported tokens: live model-call spans plus the calls recorded
    in saved research reports (generation.calls). Unreported usage stays None."""
    from app.database import db

    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    models = {}

    def add(provider, model, source, input_tokens, output_tokens, reasoning=None):
        key = (provider or "unknown", model or "unknown")
        entry = models.setdefault(key, {"provider": key[0], "model": key[1], "calls": 0,
                                        "calls_with_usage": 0, "input_tokens": 0, "output_tokens": 0,
                                        "reasoning_tokens": 0, "sources": set()})
        entry["calls"] += 1
        entry["sources"].add(source)
        if input_tokens is not None or output_tokens is not None:
            entry["calls_with_usage"] += 1
            entry["input_tokens"] += int(input_tokens or 0)
            entry["output_tokens"] += int(output_tokens or 0)
            entry["reasoning_tokens"] += int(reasoning or 0)

    with db.connect() as conn:
        spans = conn.execute(
            "SELECT input_tokens, output_tokens, attributes_json FROM telemetry_spans "
            "WHERE kind = 'model_call' AND started_at >= ?", (since,),
        ).fetchall()
    for input_tokens, output_tokens, attrs in spans:
        a = json.loads(attrs or "{}")
        add(a.get("provider"), a.get("model"), "live_trace", input_tokens, output_tokens, a.get("reasoning_tokens"))
    for call in _report_calls(since):
        add(call.get("provider"), call.get("model"), "saved_report", call.get("input_tokens"),
            call.get("output_tokens"), call.get("reasoning_tokens"))
    rows = []
    for entry in sorted(models.values(), key=lambda e: (-e["calls"], e["model"])):
        entry["sources"] = sorted(entry["sources"])
        entry["total_tokens"] = entry["input_tokens"] + entry["output_tokens"]
        rows.append(entry)
    return {"window_days": days, "models": rows,
            "totals": {k: sum(r[k] for r in rows) for k in ("calls", "calls_with_usage", "input_tokens",
                                                            "output_tokens", "total_tokens")}}


_REPORT_CALLS_CACHE = {}


def _report_calls(since):
    from app.services import document_store
    from app.services.data_freshness import parse_utc
    from app.services.report_history import project_root, reports_directory

    out = []
    for path in document_store.list_paths(reports_directory(project_root())):
        marker = document_store.version(path)
        cached = _REPORT_CALLS_CACHE.get(str(path))
        if cached and cached[0] == marker:
            generated, calls = cached[1]
        else:
            try:
                doc = json.loads(document_store.read_text(path) or "{}")
            except (ValueError, document_store.DocumentIntegrityError):
                continue
            generation = doc.get("generation") or {}
            generated = doc.get("generated_at_utc")
            calls = [{"provider": c.get("provider") or generation.get("provider"),
                      "model": c.get("model") or generation.get("model"),
                      "input_tokens": c.get("input_tokens", c.get("prompt_tokens")),
                      "output_tokens": c.get("output_tokens", c.get("completion_tokens")),
                      "reasoning_tokens": c.get("reasoning_tokens")}
                     for c in generation.get("calls") or [] if isinstance(c, dict)]
            _REPORT_CALLS_CACHE[str(path)] = (marker, (generated, calls))
        when = parse_utc(generated)
        if when is None or when >= since:
            out.extend(calls)
    return out


def recent_traces(limit=20):
    from app.database import db

    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT trace_id, MIN(started_at) AS started_at, COUNT(*) AS spans,
                   SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors,
                   MAX(CASE WHEN parent_span_id IS NULL THEN name END) AS root_name,
                   MAX(CASE WHEN parent_span_id IS NULL THEN duration_ms END) AS root_ms,
                   MAX(run_id) AS run_id
            FROM telemetry_spans
            GROUP BY trace_id ORDER BY MIN(started_at) DESC LIMIT ?
            """, (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def trace_detail(trace_id):
    from app.database import db

    if not re.fullmatch(r"[0-9a-f]{32}", trace_id or ""):
        raise ValueError("Invalid trace id.")
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT span_id, parent_span_id, run_id, kind, name, status, started_at, duration_ms, "
            "input_tokens, output_tokens, attributes_json FROM telemetry_spans WHERE trace_id = ? ORDER BY id",
            (trace_id,),
        ).fetchall()
    spans = []
    for r in rows:
        item = dict(r)
        item["attributes"] = json.loads(item.pop("attributes_json") or "{}")
        spans.append(item)
    return {"trace_id": trace_id, "spans": spans}


# ----------------------------------------------------------------- alerts

ALERT_RULES = ("api_error_rate", "api_latency_p95", "model_call_failures", "research_run_failures",
               "backup_not_verified")


def evaluate_alerts(now=None):
    """Evaluate every rule on measured data; open, update or resolve alerts."""
    if not enabled():
        return []
    import os

    from app.database import db

    now = now or datetime.now(timezone.utc)
    firing = {}
    with db.connect() as conn:
        api = conn.execute(
            "SELECT status, duration_ms, attributes_json FROM telemetry_spans WHERE kind = 'api' AND started_at >= ?",
            (now - timedelta(minutes=15),),
        ).fetchall()
        failed_calls = conn.execute(
            "SELECT COUNT(*) FROM telemetry_spans WHERE kind = 'model_call' AND status = 'error' AND started_at >= ?",
            (now - timedelta(hours=1),),
        ).fetchone()[0]
        recent_runs = [r[0] for r in conn.execute(
            "SELECT status FROM research_execution_runs WHERE status IN ('completed', 'failed') "
            "ORDER BY finished_at_utc DESC LIMIT 5").fetchall()]
        backups = conn.execute(
            "SELECT COUNT(*) FROM backup_runs WHERE status = 'succeeded' AND restore_verified AND finished_at >= ?",
            (now - timedelta(hours=float(os.getenv("ALERT_BACKUP_MAX_AGE_HOURS", "26"))),),
        ).fetchone()[0]
    if len(api) >= 20:
        server_errors = sum(1 for _, _, a in api if json.loads(a or "{}").get("status_code", 0) >= 500)
        if server_errors / len(api) > 0.05:
            firing["api_error_rate"] = ("critical", f"{server_errors} of {len(api)} API requests failed (5xx) in 15 minutes.")
        p95 = percentile([d for _, d, _ in api], 95)
        limit = float(os.getenv("ALERT_API_P95_MS", "2000"))
        if p95 is not None and p95 > limit:
            firing["api_latency_p95"] = ("warning", f"API p95 latency {p95:.0f} ms over the last 15 minutes (limit {limit:.0f} ms).")
    if failed_calls >= 3:
        firing["model_call_failures"] = ("warning", f"{failed_calls} AI model calls failed in the last hour.")
    if len(recent_runs) >= 3 and recent_runs.count("failed") >= 3:
        firing["research_run_failures"] = ("warning", f"{recent_runs.count('failed')} of the last {len(recent_runs)} research runs failed.")
    if backups == 0:
        firing["backup_not_verified"] = ("warning", "No restore-verified database backup in the configured period.")

    with db.transaction() as conn:
        open_rules = {r[0] for r in conn.execute(
            "SELECT rule FROM operational_alerts WHERE resolved_at IS NULL").fetchall()}
        for rule, (severity, message) in firing.items():
            if rule in open_rules:
                conn.execute(
                    "UPDATE operational_alerts SET last_seen_at = ?, occurrences = occurrences + 1, "
                    "message = ?, severity = ? WHERE rule = ? AND resolved_at IS NULL",
                    (now, message, severity, rule))
            else:
                conn.execute(
                    "INSERT INTO operational_alerts(rule, severity, message, first_seen_at, last_seen_at) "
                    "VALUES (?, ?, ?, ?, ?)", (rule, severity, message, now, now))
                logger.warning("alert_opened rule=%s severity=%s", rule, severity)
        for rule in open_rules - set(firing):
            conn.execute("UPDATE operational_alerts SET resolved_at = ? WHERE rule = ? AND resolved_at IS NULL",
                         (now, rule))
    return sorted(firing)


def alerts(include_resolved=False, limit=50):
    from app.database import db

    clause = "" if include_resolved else "WHERE resolved_at IS NULL"
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT id, rule, severity, message, first_seen_at, last_seen_at, occurrences, resolved_at "
            f"FROM operational_alerts {clause} ORDER BY last_seen_at DESC LIMIT ?", (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def audit_events(limit=50):
    from app.database import db

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT occurred_at, action, target, outcome, trace_id FROM audit_events ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- cleanup

def cleanup(now=None):
    """Delete old telemetry only (never business data). Returns counts."""
    import os

    from app.database import db

    if not enabled():
        return {}
    now = now or datetime.now(timezone.utc)
    spans_days = float(os.getenv("TELEMETRY_RETENTION_DAYS", "30"))
    audit_days = float(os.getenv("AUDIT_RETENTION_DAYS", "365"))
    with db.transaction() as conn:
        spans = conn.execute("DELETE FROM telemetry_spans WHERE started_at < ?",
                             (now - timedelta(days=spans_days),)).rowcount
        resolved = conn.execute("DELETE FROM operational_alerts WHERE resolved_at < ?",
                                (now - timedelta(days=90),)).rowcount
        audits = conn.execute("DELETE FROM audit_events WHERE occurred_at < ?",
                              (now - timedelta(days=audit_days),)).rowcount
    return {"telemetry_spans": max(spans, 0), "resolved_alerts": max(resolved, 0), "audit_events": max(audits, 0)}
