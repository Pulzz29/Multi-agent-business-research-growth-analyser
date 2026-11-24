"""
Azure OpenAI (GPT-5.4-Pro) structured requests for every AI-writing step.

GPT-5.4-Pro is served only through the Responses API and a single request
can take minutes, so each request is created in background mode
(``background=True``, which requires ``store=True``) and polled until it
reaches a terminal status or the per-call deadline passes. On deadline or
job cancellation the response is cancelled at the provider.

One call = an ordered list of request *profiles* (for example "full" then
"reduced"). Each profile builds its own, smaller evidence payload. The
helper:

* estimates the input size and skips a profile above the input budget;
* sends a strict JSON-schema structured-output request;
* on an over-long input, an output-token limit, invalid JSON/schema or a
  failed caller validation, moves to the next (smaller) profile and never
  resends an unchanged request;
* retries HTTP 429 with the provider's Retry-After (bounded) and a
  timeout / connection / 5xx error once;
* rejects any response whose served model does not match the configured
  expectation (no silent model substitution);
* never creates more than ``len(profiles) + 1`` responses per call.

Diagnostics contain sizes, timings, outcomes, status codes, response IDs,
the served model and provider-reported token usage only. Prompts, model
output, provider error messages and credentials are never recorded.
"""

import contextlib
import json
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pydantic import ValidationError


logger = logging.getLogger("insightiq.ai")

PROVIDER = "azure_openai"
ESTIMATED_BYTES_PER_TOKEN = 3.0
TRANSIENT_BACKOFF_SECONDS = 3.0
MAX_RATE_LIMIT_RETRIES = 3
MAX_SINGLE_RATE_WAIT_SECONDS = 60.0
MAX_TOTAL_RATE_WAIT_SECONDS = 180.0
MAX_CONSECUTIVE_POLL_ERRORS = 5
TERMINAL_STATUSES = {"completed", "incomplete", "failed", "cancelled"}

# Keywords kept when converting a Pydantic schema to a strict structured-output
# schema. Value constraints (lengths, patterns, ranges) are enforced by the
# Pydantic model after parsing, so the provider schema only fixes the shape.
_SCHEMA_KEYWORDS = {
    "type", "properties", "required", "additionalProperties", "items",
    "enum", "anyOf", "$defs", "$ref", "description", "const",
}


class AICallFailed(RuntimeError):
    """Raised with a short, user-safe reason code and diagnostics."""

    def __init__(self, reason, diagnostics):
        super().__init__(reason)
        self.reason = reason
        self.diagnostics = diagnostics


class ResponseRejected(ValueError):
    """Raised by a caller validation hook to reject a parsed response."""


class CallCancelled(RuntimeError):
    pass


@dataclass
class RequestProfile:
    name: str
    max_output_tokens: int
    options: dict = field(default_factory=dict)


# ------------------------------------------------------------------ settings

def ai_runtime_settings():
    from app.core.config import get_settings

    s = get_settings()
    return {
        "deployment": s.azure_openai_deployment,
        "endpoint": s.azure_openai_endpoint,
        "reasoning_effort": s.azure_reasoning_effort,
        "input_token_budget": s.azure_input_token_budget,
        "max_output_financial": s.azure_max_output_tokens_financial,
        "max_output_news_selection": s.azure_max_output_tokens_news_selection,
        "max_output_news_analysis": s.azure_max_output_tokens_news_analysis,
        "max_output_strategic": s.azure_max_output_tokens_strategic,
        "max_output_summary": s.azure_max_output_tokens_summary,
        "max_output_research_brief": s.azure_max_output_tokens_research_brief,
        "max_output_sector": s.azure_max_output_tokens_sector,
        "deadline_seconds": s.azure_call_deadline_seconds,
        "poll_interval_seconds": s.azure_poll_interval_seconds,
        "http_timeout_seconds": s.azure_http_timeout_seconds,
        "max_concurrent_calls": s.azure_max_concurrent_calls,
        "expected_model_prefix": s.azure_expected_model_prefix or s.azure_openai_deployment,
    }


def configured_deployment():
    try:
        return ai_runtime_settings()["deployment"]
    except Exception:
        return None


def provider_label(deployment=None, model=None):
    """Human label such as 'Azure OpenAI · gpt-5.4-pro'."""
    name = model or deployment or configured_deployment() or "deployment not configured"
    return f"Azure OpenAI · {name}"


def default_client(timeout_seconds=None):
    """Official OpenAI SDK pointed at the Azure v1 endpoint, or None."""
    try:
        from app.core.config import get_settings

        s = get_settings()
        key = s.azure_openai_api_key.get_secret_value()
        endpoint = s.azure_openai_endpoint
        timeout = timeout_seconds or s.azure_http_timeout_seconds
    except Exception:
        return None
    if not key or not endpoint:
        return None
    from openai import OpenAI

    return OpenAI(base_url=endpoint, api_key=key, timeout=timeout, max_retries=0)


# ------------------------------------------------- job context (cancel/deadline)

_CONTEXT = threading.local()


@contextlib.contextmanager
def job_context(cancel_check=None, deadline_monotonic=None):
    """Bind a research job's cancellation check and deadline to this thread."""
    previous = (getattr(_CONTEXT, "cancel_check", None), getattr(_CONTEXT, "deadline", None))
    _CONTEXT.cancel_check = cancel_check
    _CONTEXT.deadline = deadline_monotonic
    try:
        yield
    finally:
        _CONTEXT.cancel_check, _CONTEXT.deadline = previous


class JobCancelled(RuntimeError):
    """The user cancelled the research job."""


class JobDeadlineExceeded(RuntimeError):
    """The research job ran past its configured deadline."""


def check_job_continuation(clock=None):
    """Raise when the bound job was cancelled or passed its deadline."""
    if _job_cancelled():
        raise JobCancelled("Research job was cancelled.")
    deadline = _job_deadline()
    if deadline is not None and (clock or time.monotonic)() >= deadline:
        raise JobDeadlineExceeded("Research job exceeded its deadline.")


def _job_cancelled():
    check = getattr(_CONTEXT, "cancel_check", None)
    try:
        return bool(check and check())
    except Exception:
        return False


def _job_deadline():
    return getattr(_CONTEXT, "deadline", None)


# ---------------------------------------------------------------- concurrency

_SLOTS = None
_SLOTS_GUARD = threading.Lock()


def _slots(limit):
    global _SLOTS
    with _SLOTS_GUARD:
        if _SLOTS is None or _SLOTS[0] != limit:
            _SLOTS = (limit, threading.BoundedSemaphore(limit))
        return _SLOTS[1]


# -------------------------------------------------------------------- schemas

def strict_json_schema(schema_model):
    """Pydantic model -> strict structured-output JSON schema."""

    def convert(node):
        if isinstance(node, list):
            return [convert(item) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return {"$ref": node["$ref"]}
        out = {}
        for key, value in node.items():
            if key not in _SCHEMA_KEYWORDS:
                continue
            if key in ("properties", "$defs"):
                out[key] = {name: convert(sub) for name, sub in value.items()}
            else:
                out[key] = convert(value)
        if out.get("type") == "object" or "properties" in out:
            out["type"] = "object"
            out["additionalProperties"] = False
            out["required"] = list((out.get("properties") or {}).keys())
            out.setdefault("properties", {})
        return out

    return convert(schema_model.model_json_schema())


def text_format(schema_name, schema_model):
    return {
        "format": {
            "type": "json_schema",
            "name": schema_name,
            "schema": strict_json_schema(schema_model),
            "strict": True,
        }
    }


def split_messages(messages):
    """Chat-style messages -> (instructions, input items) for the Responses API."""
    instructions = "\n\n".join(m["content"] for m in messages if m["role"] in ("system", "developer"))
    items = [
        {"role": m["role"], "content": m["content"]}
        for m in messages if m["role"] not in ("system", "developer")
    ]
    return instructions, items


def estimate_request_tokens(instructions, items, fmt):
    """Conservative estimate (over-counts typical English/JSON)."""
    payload = json.dumps({"instructions": instructions, "input": items, "text": fmt},
                         ensure_ascii=False, allow_nan=False)
    return math.ceil(len(payload.encode("utf-8")) / ESTIMATED_BYTES_PER_TOKEN) + 8 * (len(items) + 1)


def brief_diagnostics(diagnostics):
    """Compact, secret-free subset of call diagnostics stored in reports."""
    d = diagnostics or {}
    return {
        "provider": d.get("provider"),
        "deployment": d.get("deployment"),
        "model": d.get("model"),
        "response_id": d.get("response_id"),
        "response_status": d.get("response_status"),
        "elapsed_seconds": d.get("elapsed_seconds"),
        "input_tokens": d.get("input_tokens"),
        "output_tokens": d.get("output_tokens"),
        "reasoning_tokens": d.get("reasoning_tokens"),
        "total_tokens": d.get("total_tokens"),
    }


# ------------------------------------------------------------ response access

def _get(obj, name, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def response_text(response):
    """(text, refused) from a Responses API response."""
    refused = False
    chunks = []
    for item in _get(response, "output") or []:
        if _get(item, "type") != "message":
            continue
        for part in _get(item, "content") or []:
            kind = _get(part, "type")
            if kind == "output_text":
                chunks.append(_get(part, "text") or "")
            elif kind == "refusal":
                refused = True
    text = "".join(chunks)
    if not text:
        text = _get(response, "output_text") or ""
    return text, refused


TOKEN_FIELDS = ("input_tokens", "output_tokens", "reasoning_tokens", "cached_input_tokens", "total_tokens")


def response_usage(response):
    usage = _get(response, "usage")
    if usage is None:
        return None
    out_details = _get(usage, "output_tokens_details")
    in_details = _get(usage, "input_tokens_details")
    return {
        "input_tokens": _get(usage, "input_tokens"),
        "output_tokens": _get(usage, "output_tokens"),
        "reasoning_tokens": _get(out_details, "reasoning_tokens"),
        "cached_input_tokens": _get(in_details, "cached_tokens"),
        "total_tokens": _get(usage, "total_tokens"),
    }


def _retry_after(error):
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None) or {}
    for name in ("retry-after-ms", "retry-after"):
        value = headers.get(name) if hasattr(headers, "get") else None
        if value:
            try:
                seconds = float(value) / (1000.0 if name.endswith("-ms") else 1.0)
                return max(0.0, seconds)
            except ValueError:
                continue
    return None


def _error_code(error):
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        inner = body.get("error") if isinstance(body.get("error"), dict) else body
        return (str(inner.get("code") or "")[:60] or None), (str(inner.get("param") or "")[:60] or None)
    return None, None


def _classify_exception(error):
    """Return (outcome, status_code, action) with action in {retry, next, fatal, rate}."""
    import openai

    if isinstance(error, CallCancelled):
        return "cancelled", None, "fatal"
    if isinstance(error, openai.APITimeoutError):
        return "timeout", None, "retry"
    if isinstance(error, openai.APIConnectionError):
        return "connection_error", None, "retry"
    if isinstance(error, openai.APIStatusError):
        code = getattr(error, "status_code", None)
        provider_code, _ = _error_code(error)
        if code == 429:
            return "rate_limited", 429, "rate"
        if code in (401, 403):
            return "authentication_failed", code, "fatal"
        if code == 404:
            return "deployment_unavailable", 404, "fatal"
        if code == 413 or (code == 400 and provider_code and "context_length" in provider_code):
            return "payload_too_large", code, "next"
        if code == 400:
            return "bad_request", 400, "fatal"
        if code is not None and code >= 500:
            return "provider_server_error", code, "retry"
        return "provider_error", code, "fatal"
    return "unexpected_error", None, "fatal"


# ----------------------------------------------------------------- the call

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
    clock=time.monotonic,
):
    """
    Run one bounded structured request. Returns (parsed, validated, diagnostics).

    ``validate(parsed, profile)`` may raise ``ResponseRejected`` (or any
    ValueError) to reject a response; its return value is passed back as
    ``validated``.
    """
    settings = ai_runtime_settings()
    budget = int(request_token_budget or settings["input_token_budget"])
    deployment = settings["deployment"]
    effort = settings["reasoning_effort"]
    expected_prefix = (settings["expected_model_prefix"] or "").lower()

    diagnostics = {
        "provider": PROVIDER,
        "agent": agent,
        "call": call_name,
        "deployment": deployment,
        "model": None,
        "status": "failed",
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_token_budget": budget,
        "reasoning_effort": effort,
        "profile_used": None,
        "attempts": [],
        "provider_requests": 0,
        "input_tokens": None,
        "output_tokens": None,
        "reasoning_tokens": None,
        "cached_input_tokens": None,
        "total_tokens": None,
        "token_usage_reported_by_provider": False,
        "estimated_input_tokens": None,
        "max_output_tokens": None,
        "response_id": None,
        "response_status": None,
        "incomplete_reason": None,
        "elapsed_seconds": None,
        "rate_limit_retries": 0,
        "fallback_reason": None,
        "failure_reason": None,
    }
    if not profiles:
        raise ValueError("At least one request profile is required.")

    if client is None:
        client = default_client(settings["http_timeout_seconds"])
    if client is None:
        diagnostics["failure_reason"] = "credential_missing"
        raise AICallFailed("credential_missing", diagnostics)

    started = clock()
    call_deadline = started + settings["deadline_seconds"]
    job_deadline = _job_deadline()
    if job_deadline is not None:
        call_deadline = min(call_deadline, job_deadline)
    max_requests = len(profiles) + 1
    first_problem = None
    rate_wait_total = 0.0

    def finish(status, reason=None):
        diagnostics["status"] = status
        diagnostics["elapsed_seconds"] = round(clock() - started, 3)
        # Every Azure response is billed, including ones rejected by schema or
        # evidence validation: total them across attempts so cost is never
        # under-reported. A failed call's top-level token fields show this total.
        billed = [a["usage"] for a in diagnostics["attempts"] if a.get("usage")]
        if billed:
            totals = {k: sum(u.get(k) or 0 for u in billed) for k in TOKEN_FIELDS}
            diagnostics["billed_tokens_all_attempts"] = {**totals, "responses": len(billed)}
            if status != "completed" and diagnostics["input_tokens"] is None:
                diagnostics.update(totals)
                diagnostics["token_usage_reported_by_provider"] = True
        if reason:
            diagnostics["failure_reason"] = reason
        if diagnostics["profile_used"] and diagnostics["profile_used"] != profiles[0].name:
            diagnostics["fallback_reason"] = first_problem
        logger.info("ai_call %s", json.dumps({
            k: diagnostics[k] for k in (
                "provider", "agent", "call", "deployment", "model", "status", "profile_used",
                "provider_requests", "response_id", "input_tokens", "output_tokens",
                "reasoning_tokens", "elapsed_seconds", "rate_limit_retries",
                "fallback_reason", "failure_reason",
            )
        }))

    def check_cancel_and_deadline():
        if _job_cancelled():
            raise CallCancelled("job_cancelled")
        if clock() >= call_deadline:
            raise TimeoutError("deadline_exceeded")

    def cancel_remote(response_id):
        if not response_id:
            return
        try:
            client.responses.cancel(response_id)
        except Exception:  # best effort; the outcome is recorded either way
            pass

    def poll(response, attempt):
        nonlocal rate_wait_total
        errors = 0
        while _get(response, "status") not in TERMINAL_STATUSES:
            try:
                check_cancel_and_deadline()
            except (CallCancelled, TimeoutError):
                cancel_remote(_get(response, "id"))
                raise
            sleep(settings["poll_interval_seconds"])
            try:
                response = client.responses.retrieve(_get(response, "id"))
                errors = 0
            except Exception as error:
                outcome, code, action = _classify_exception(error)
                if action == "rate":
                    wait = min(_retry_after(error) or 5.0, MAX_SINGLE_RATE_WAIT_SECONDS)
                    rate_wait_total += wait
                    diagnostics["rate_limit_retries"] += 1
                    sleep(wait)
                    continue
                errors += 1
                if action != "retry" or errors >= MAX_CONSECUTIVE_POLL_ERRORS:
                    attempt["poll_error"] = outcome
                    attempt["status_code"] = code
                    cancel_remote(_get(response, "id"))
                    raise
        return response

    semaphore = _slots(settings["max_concurrent_calls"])
    with semaphore:
        for profile in profiles:
            messages = build_messages(profile)
            instructions, items = split_messages(messages)
            fmt = text_format(schema_name, schema_model)
            estimated = estimate_request_tokens(instructions, items, fmt)

            if estimated > budget:
                diagnostics["attempts"].append({
                    "profile": profile.name,
                    "outcome": "skipped_over_budget",
                    "status_code": None,
                    "estimated_input_tokens": estimated,
                    "max_output_tokens": profile.max_output_tokens,
                })
                first_problem = first_problem or "skipped_over_budget"
                continue

            transient_retry_used = False
            rate_retries = 0
            while True:
                if diagnostics["provider_requests"] >= max_requests:
                    diagnostics["attempt_limit_reached"] = True
                    reason = first_problem or "attempt_limit_reached"
                    finish("failed", reason)
                    raise AICallFailed(reason, diagnostics)
                try:
                    check_cancel_and_deadline()
                except CallCancelled:
                    finish("cancelled", "cancelled")
                    raise AICallFailed("cancelled", diagnostics) from None
                except TimeoutError:
                    finish("failed", "deadline_exceeded")
                    raise AICallFailed("deadline_exceeded", diagnostics) from None

                diagnostics["provider_requests"] += 1
                attempt = {
                    "profile": profile.name,
                    "outcome": None,
                    "status_code": None,
                    "response_id": None,
                    "estimated_input_tokens": estimated,
                    "max_output_tokens": profile.max_output_tokens,
                }
                diagnostics["attempts"].append(attempt)
                attempt_started = clock()

                try:
                    response = client.responses.create(
                        model=deployment,
                        instructions=instructions,
                        input=items,
                        text=fmt,
                        reasoning={"effort": effort},
                        max_output_tokens=profile.max_output_tokens,
                        background=True,
                        store=True,
                    )
                    attempt["response_id"] = _get(response, "id")
                    response = poll(response, attempt)
                except CallCancelled:
                    attempt["outcome"] = "cancelled"
                    attempt["elapsed_seconds"] = round(clock() - attempt_started, 3)
                    finish("cancelled", "cancelled")
                    raise AICallFailed("cancelled", diagnostics) from None
                except TimeoutError:
                    attempt["outcome"] = "deadline_exceeded"
                    attempt["elapsed_seconds"] = round(clock() - attempt_started, 3)
                    finish("failed", "deadline_exceeded")
                    raise AICallFailed("deadline_exceeded", diagnostics) from None
                except Exception as error:
                    outcome, code, action = _classify_exception(error)
                    attempt["outcome"] = outcome
                    attempt["status_code"] = code
                    provider_code, param = _error_code(error)
                    if provider_code:
                        attempt["provider_error_code"] = provider_code
                    if param:
                        attempt["provider_error_param"] = param
                    attempt["elapsed_seconds"] = round(clock() - attempt_started, 3)

                    if action == "rate":
                        wait = _retry_after(error) or min(5.0 * (2 ** rate_retries), 40.0)
                        if (rate_retries >= MAX_RATE_LIMIT_RETRIES or wait > MAX_SINGLE_RATE_WAIT_SECONDS
                                or rate_wait_total + wait > MAX_TOTAL_RATE_WAIT_SECONDS
                                or clock() + wait >= call_deadline):
                            finish("failed", "rate_limit_exhausted")
                            raise AICallFailed("rate_limit_exhausted", diagnostics) from None
                        rate_retries += 1
                        rate_wait_total += wait
                        diagnostics["rate_limit_retries"] += 1
                        diagnostics["provider_requests"] -= 1  # a refused request created nothing
                        sleep(wait)
                        continue

                    first_problem = first_problem or outcome
                    if action == "next":
                        break
                    if action == "retry" and not transient_retry_used:
                        transient_retry_used = True
                        sleep(TRANSIENT_BACKOFF_SECONDS)
                        continue
                    if action == "fatal":
                        finish("failed", outcome)
                        raise AICallFailed(outcome, diagnostics) from None
                    break

                attempt["elapsed_seconds"] = round(clock() - attempt_started, 3)
                status = _get(response, "status")
                served_model = _get(response, "model")
                attempt["response_status"] = status
                attempt["model"] = served_model
                billed_usage = response_usage(response)
                if billed_usage:
                    attempt["usage"] = billed_usage

                if status == "failed":
                    error_obj = _get(response, "error")
                    code = str(_get(error_obj, "code") or "")[:60] or None
                    attempt["outcome"] = "provider_failed"
                    attempt["provider_error_code"] = code
                    first_problem = first_problem or "provider_failed"
                    if code in ("server_error", "rate_limit_exceeded") and not transient_retry_used:
                        transient_retry_used = True
                        sleep(TRANSIENT_BACKOFF_SECONDS)
                        continue
                    break
                if status == "cancelled":
                    attempt["outcome"] = "cancelled_by_provider"
                    finish("failed", "cancelled_by_provider")
                    raise AICallFailed("cancelled_by_provider", diagnostics)
                if status == "incomplete":
                    reason = str(_get(_get(response, "incomplete_details"), "reason") or "unknown")
                    attempt["incomplete_reason"] = reason
                    attempt["outcome"] = (
                        "output_token_limit" if reason == "max_output_tokens"
                        else "content_filtered" if reason == "content_filter"
                        else "incomplete_response"
                    )
                    first_problem = first_problem or attempt["outcome"]
                    break

                if expected_prefix and not str(served_model or "").lower().startswith(expected_prefix):
                    attempt["outcome"] = "unexpected_model"
                    diagnostics["model"] = served_model
                    finish("failed", "unexpected_model")
                    raise AICallFailed("unexpected_model", diagnostics)

                text, refused = response_text(response)
                if refused or not text:
                    attempt["outcome"] = "refused" if refused else "empty_response"
                    first_problem = first_problem or attempt["outcome"]
                    break
                try:
                    parsed = schema_model.model_validate_json(text)
                except (ValidationError, ValueError):
                    attempt["outcome"] = "invalid_schema"
                    first_problem = first_problem or "invalid_schema"
                    break
                try:
                    validated = validate(parsed, profile) if validate else None
                except (ResponseRejected, ValueError) as rejection:
                    attempt["outcome"] = "validation_rejected"
                    attempt["validation_reason"] = str(rejection)[:200]
                    first_problem = first_problem or "validation_rejected"
                    break

                usage = response_usage(response)
                attempt["outcome"] = "success"
                diagnostics.update({
                    "profile_used": profile.name,
                    "model": served_model,
                    "estimated_input_tokens": estimated,
                    "max_output_tokens": profile.max_output_tokens,
                    "response_id": _get(response, "id"),
                    "response_status": status,
                    "token_usage_reported_by_provider": usage is not None,
                    **(usage or {}),
                })
                finish("completed")
                return parsed, validated, diagnostics

    reason = first_problem or "no_profile_succeeded"
    finish("failed", reason)
    raise AICallFailed(reason, diagnostics)
