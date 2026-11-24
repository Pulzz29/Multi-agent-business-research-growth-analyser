"""
Bounded, budget-aware structured Groq requests for the research agents.

One call = an ordered list of request *profiles* (for example "full" then
"reduced"). Each profile builds its own, smaller evidence payload. The
helper:

* estimates the request size before sending and skips any profile whose
  estimate plus reserved completion tokens exceeds the request budget;
* sends through ``guarded_chat_completion`` (bounded HTTP 429 handling
  with provider retry guidance);
* on HTTP 413, ``finish_reason == "length"``, invalid JSON / schema, or a
  failed caller-supplied validation, moves to the next (smaller) profile
  and never resends an unchanged request;
* retries a timeout, connection error or 5xx response once with a short
  backoff;
* never makes more than ``len(profiles) + 1`` provider requests.

Diagnostics contain sizes, timings, outcomes, status codes and the
provider-reported token usage only. Prompts, responses, provider error
messages and credentials are never recorded.
"""

import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pydantic import ValidationError

from app.services.groq_rate_limit import (
    GroqRateLimitExhausted,
    guarded_chat_completion,
    retry_event_count,
)


logger = logging.getLogger("insightiq.groq")

DEFAULT_MODEL = "openai/gpt-oss-120b"
ESTIMATED_BYTES_PER_TOKEN = 3.0
PER_MESSAGE_OVERHEAD_TOKENS = 8
TRANSIENT_BACKOFF_SECONDS = 2.0

_SIZE_EVENTS = []


class GroqCallFailed(RuntimeError):
    """Raised with a short, user-safe reason code and diagnostics."""

    def __init__(self, reason, diagnostics):
        super().__init__(reason)
        self.reason = reason
        self.diagnostics = diagnostics


class ResponseRejected(ValueError):
    """Raised by a caller validation hook to reject a parsed response."""


@dataclass
class RequestProfile:
    name: str
    max_completion_tokens: int
    options: dict = field(default_factory=dict)


def configured_model():
    return (os.getenv("GROQ_MODEL") or DEFAULT_MODEL).strip() or DEFAULT_MODEL


def groq_runtime_settings():
    """Budget settings, with safe defaults if settings cannot load."""
    try:
        from app.core.config import get_settings

        s = get_settings()
        return {
            "request_token_budget": s.groq_request_token_budget,
            "financial_max_completion": s.groq_financial_max_completion,
            "news_analysis_max_completion": s.groq_news_analysis_max_completion,
            "strategic_max_completion": s.groq_strategic_max_completion,
            "reasoning_effort": s.groq_reasoning_effort,
            "timeout_seconds": s.groq_request_timeout_seconds,
        }
    except Exception:
        return {
            "request_token_budget": 7000,
            "financial_max_completion": 2800,
            "news_analysis_max_completion": 2400,
            "strategic_max_completion": 2800,
            "reasoning_effort": "low",
            "timeout_seconds": 45.0,
        }


def response_format(schema_name, schema_model):
    return {
        "type": "json_schema",
        "json_schema": {
            "name": schema_name,
            "strict": True,
            "schema": schema_model.model_json_schema(),
        },
    }


def estimate_request_tokens(messages, fmt):
    """Conservative estimate (over-counts typical English/JSON)."""
    payload = json.dumps(
        {"messages": messages, "response_format": fmt},
        ensure_ascii=False,
        allow_nan=False,
    )

    return (
        math.ceil(len(payload.encode("utf-8")) / ESTIMATED_BYTES_PER_TOKEN)
        + PER_MESSAGE_OVERHEAD_TOKENS * len(messages)
    )


def _provider_limit_numbers(error):
    """Extract only the numeric limit/requested values from a 413."""
    message = str(getattr(error, "message", "") or "")
    numbers = re.search(
        r"limit\D{0,20}(\d+)\D{1,40}requested\D{0,20}(\d+)",
        message,
        flags=re.IGNORECASE,
    )
    if not numbers:
        return None, None
    return int(numbers.group(1)), int(numbers.group(2))


def recent_size_events():
    return list(_SIZE_EVENTS)


def default_client(timeout_seconds):
    from groq import Groq

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        return None
    return Groq(api_key=api_key, timeout=timeout_seconds, max_retries=0)


def _classify_exception(error):
    """Return (outcome, status_code, retry_same_profile)."""
    from groq import (
        APIConnectionError,
        APIStatusError,
        APITimeoutError,
    )

    if isinstance(error, APITimeoutError):
        return "timeout", None, True

    if isinstance(error, APIConnectionError):
        return "connection_error", None, True

    if isinstance(error, APIStatusError):
        code = getattr(error, "status_code", None)

        if code == 413:
            return "payload_too_large", 413, False

        if code == 400:
            body = getattr(error, "body", None)
            text = json.dumps(body, default=str).lower() if body else ""
            if "json_validate_failed" in text or "failed to generate json" in text:
                # Strict-schema generation failure: a smaller profile may succeed.
                return "invalid_schema", 400, False
            return "bad_request", 400, None

        if code is not None and code >= 500:
            return "provider_server_error", code, True

        return "provider_error", code, None

    return "unexpected_error", None, None


def structured_completion(
    *,
    agent,
    call_name,
    schema_name,
    schema_model,
    build_messages,
    profiles,
    validate=None,
    request_token_budget=None,
    client=None,
    sleep=time.sleep,
):
    """
    Run one bounded structured request. Returns (parsed, validated, diagnostics).

    ``validate(parsed, profile)`` may raise ``ResponseRejected`` (or any
    ValueError) to reject a response; its return value is passed back as
    ``validated``.
    """
    settings = groq_runtime_settings()
    budget = int(request_token_budget or settings["request_token_budget"])
    model = configured_model()
    reasoning_effort = settings["reasoning_effort"]

    diagnostics = {
        "agent": agent,
        "call": call_name,
        "model": model,
        "status": "failed",
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "request_token_budget": budget,
        "reasoning_effort": reasoning_effort or None,
        "profile_used": None,
        "attempts": [],
        "provider_requests": 0,
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "token_usage_reported_by_provider": False,
        "estimated_input_tokens": None,
        "max_completion_tokens": None,
        "response_id": None,
        "finish_reason": None,
        "elapsed_seconds": None,
        "rate_limit_retries": 0,
        "fallback_reason": None,
        "failure_reason": None,
    }

    if not profiles:
        raise ValueError("At least one request profile is required.")

    if client is None:
        client = default_client(settings["timeout_seconds"])

    if client is None:
        diagnostics["failure_reason"] = "credential_missing"
        raise GroqCallFailed("credential_missing", diagnostics)

    max_requests = len(profiles) + 1
    started = time.perf_counter()
    rate_events_before = retry_event_count()
    first_problem = None

    def finish(status, reason=None):
        diagnostics["status"] = status
        diagnostics["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        diagnostics["rate_limit_retries"] = max(0, retry_event_count() - rate_events_before)
        if reason:
            diagnostics["failure_reason"] = reason
        if diagnostics["profile_used"] and diagnostics["profile_used"] != profiles[0].name:
            diagnostics["fallback_reason"] = first_problem
        logger.info(
            "groq_call %s",
            json.dumps({
                k: diagnostics[k]
                for k in (
                    "agent", "call", "model", "status", "profile_used",
                    "provider_requests", "prompt_tokens", "completion_tokens",
                    "total_tokens", "elapsed_seconds", "rate_limit_retries",
                    "fallback_reason", "failure_reason",
                )
            }),
        )

    for profile in profiles:
        messages = build_messages(profile)
        fmt = response_format(schema_name, schema_model)
        estimated = estimate_request_tokens(messages, fmt)

        if estimated + profile.max_completion_tokens > budget:
            diagnostics["attempts"].append({
                "profile": profile.name,
                "outcome": "skipped_over_budget",
                "status_code": None,
                "estimated_input_tokens": estimated,
                "max_completion_tokens": profile.max_completion_tokens,
            })
            first_problem = first_problem or "skipped_over_budget"
            continue

        transient_retry_used = False

        while True:
            if diagnostics["provider_requests"] >= max_requests:
                diagnostics["attempt_limit_reached"] = True
                reason = first_problem or "attempt_limit_reached"
                finish("failed", reason)
                raise GroqCallFailed(reason, diagnostics)

            diagnostics["provider_requests"] += 1
            attempt = {
                "profile": profile.name,
                "outcome": None,
                "status_code": None,
                "estimated_input_tokens": estimated,
                "max_completion_tokens": profile.max_completion_tokens,
            }
            diagnostics["attempts"].append(attempt)
            attempt_started = time.perf_counter()

            kwargs = dict(
                model=model,
                messages=messages,
                response_format=fmt,
                temperature=0,
                max_completion_tokens=profile.max_completion_tokens,
            )
            if reasoning_effort:
                kwargs["reasoning_effort"] = reasoning_effort

            try:
                response = guarded_chat_completion(client, **kwargs)

            except GroqRateLimitExhausted:
                attempt["outcome"] = "rate_limit_exhausted"
                attempt["status_code"] = 429
                attempt["elapsed_seconds"] = round(time.perf_counter() - attempt_started, 3)
                finish("failed", "rate_limit_exhausted")
                raise GroqCallFailed("rate_limit_exhausted", diagnostics) from None

            except Exception as error:  # provider / network errors
                outcome, code, retry_same = _classify_exception(error)
                attempt["outcome"] = outcome
                attempt["status_code"] = code
                attempt["elapsed_seconds"] = round(time.perf_counter() - attempt_started, 3)
                first_problem = first_problem or outcome

                if outcome == "payload_too_large":
                    limit, requested = _provider_limit_numbers(error)
                    attempt["provider_limit_tokens"] = limit
                    attempt["provider_requested_tokens"] = requested
                    _SIZE_EVENTS.append({
                        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                        "agent": agent,
                        "call": call_name,
                        "profile": profile.name,
                        "estimated_input_tokens": estimated,
                        "max_completion_tokens": profile.max_completion_tokens,
                        "provider_limit_tokens": limit,
                        "provider_requested_tokens": requested,
                    })
                    del _SIZE_EVENTS[:-50]
                    logger.warning(
                        "groq_request_too_large agent=%s call=%s profile=%s estimated=%s",
                        agent, call_name, profile.name, estimated,
                    )
                    break  # next, smaller profile; never resend unchanged

                if retry_same is True and not transient_retry_used:
                    transient_retry_used = True
                    sleep(TRANSIENT_BACKOFF_SECONDS)
                    continue

                if retry_same is None:
                    # Non-retryable request error (e.g. HTTP 400).
                    finish("failed", outcome)
                    raise GroqCallFailed(outcome, diagnostics) from None

                break  # schema failure or repeated transient: next profile

            attempt["elapsed_seconds"] = round(time.perf_counter() - attempt_started, 3)
            choices = getattr(response, "choices", None)

            if not choices:
                attempt["outcome"] = "empty_response"
                first_problem = first_problem or "empty_response"
                break

            choice = choices[0]
            finish_reason = getattr(choice, "finish_reason", None)
            content = getattr(getattr(choice, "message", None), "content", None)

            if finish_reason != "stop" or not content:
                attempt["outcome"] = (
                    "output_token_limit" if finish_reason == "length"
                    else "incomplete_response"
                )
                attempt["finish_reason"] = finish_reason
                first_problem = first_problem or attempt["outcome"]
                break

            try:
                parsed = schema_model.model_validate_json(content)
            except (ValidationError, ValueError):
                attempt["outcome"] = "invalid_schema"
                first_problem = first_problem or "invalid_schema"
                break

            try:
                validated = validate(parsed, profile) if validate else None
            except (ResponseRejected, ValueError) as rejection:
                attempt["outcome"] = "validation_rejected"
                # Validators raise short, content-free reasons.
                attempt["validation_reason"] = str(rejection)[:200]
                first_problem = first_problem or "validation_rejected"
                break

            usage = getattr(response, "usage", None)
            attempt["outcome"] = "success"
            diagnostics.update({
                "profile_used": profile.name,
                "estimated_input_tokens": estimated,
                "max_completion_tokens": profile.max_completion_tokens,
                "response_id": getattr(response, "id", None),
                "finish_reason": finish_reason,
                "prompt_tokens": getattr(usage, "prompt_tokens", None) if usage else None,
                "completion_tokens": getattr(usage, "completion_tokens", None) if usage else None,
                "total_tokens": getattr(usage, "total_tokens", None) if usage else None,
                "token_usage_reported_by_provider": usage is not None,
            })
            finish("completed")
            return parsed, validated, diagnostics

    reason = first_problem or "no_profile_succeeded"
    finish("failed", reason)
    raise GroqCallFailed(reason, diagnostics)
