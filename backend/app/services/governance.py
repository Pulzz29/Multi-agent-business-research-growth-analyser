"""
AI governance records (PostgreSQL ``ai_governance_events``).

One row per model call (financial, news and strategic agents, executive
summaries, chatbot), per chatbot guardrail decision and per finished research
report: model identity, prompt SHA-256 and guardrail version, validation
outcome and rejections, detected violations and evidence provenance (ids and
an input hash; the evidence itself stays in the saved report). Recording
never raises and never stores prompts, secrets or user message text.
"""

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("insightiq.governance")
OUTCOMES = ("success", "partial", "rejected", "failed", "blocked")
EVIDENCE_ID = re.compile(r"\b(?:SEC|CMP|FORECAST|NEWS|FMP|YF|METRIC)_[A-Za-z0-9_]+\b|\"E\d{1,3}\"")


def sha(text):
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest() if text else None


def evidence_from_messages(messages):
    """Provenance of a model request: evidence ids it carried and an input hash."""
    user = "\n".join(str(m.get("content") or "") for m in messages if m.get("role") != "system")
    ids = sorted({m.strip('"') for m in EVIDENCE_ID.findall(user)})
    return {"evidence_ids": ids[:80], "evidence_id_count": len(ids), "input_sha256": sha(user)}


def record(component, outcome, *, call_name=None, run_id=None, conversation_id=None, trace_id=None, ticker=None,
           provider=None, model=None, prompt_sha256=None, validation_rejections=0, violations=(), evidence=None):
    try:
        from app.services import guardrails, telemetry

        if outcome not in OUTCOMES or not telemetry.enabled():
            return
        from app.database import db

        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO ai_governance_events(occurred_at, component, call_name, outcome, run_id, "
                "conversation_id, trace_id, ticker, provider, model, prompt_sha256, guardrail_version, "
                "validation_rejections, violations_json, evidence_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (datetime.now(timezone.utc), str(component)[:80], call_name, outcome,
                 run_id or telemetry.current_run_id(), conversation_id, trace_id or telemetry.current_trace_id(),
                 ticker, provider, model, prompt_sha256, guardrails.GUARDRAIL_VERSION, int(validation_rejections or 0),
                 json.dumps([str(v)[:200] for v in violations][:20]),
                 json.dumps(evidence or {}, default=str)[:8000]))
    except Exception as exc:
        logger.warning("governance_write_failed %s", type(exc).__name__)


def summary(days=30, now=None):
    """Outcome counts per component and the overall validation pass rate."""
    from app.database import db

    since = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT component, outcome, COUNT(*) AS n, COALESCE(SUM(validation_rejections), 0) AS rejections "
            "FROM ai_governance_events WHERE occurred_at >= ? GROUP BY component, outcome", (since,)).fetchall()
    by_component, totals = {}, {o: 0 for o in OUTCOMES}
    rejections = 0
    for r in rows:
        by_component.setdefault(r["component"], {})[r["outcome"]] = r["n"]
        totals[r["outcome"]] += r["n"]
        rejections += r["rejections"]
    checked = sum(totals.values())
    return {"window_days": days, "events": checked, "by_outcome": totals, "by_component": by_component,
            "validation_rejections": rejections,
            "pass_rate": round(totals["success"] / checked, 4) if checked else None}
