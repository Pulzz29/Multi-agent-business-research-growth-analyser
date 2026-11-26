"""
The one LLM module: every AI-writing step in InsightIQ calls
``structured_completion`` here. No other module imports an LLM SDK or names a
model; the model comes from ``LLM_MODEL`` in .env (on Azure: the deployment
name), so switching models is a one-line .env change.

Primary provider: Azure OpenAI (``AZURE_OPENAI_ENDPOINT`` /openai/v1,
``AZURE_OPENAI_API_KEY``), through the official OpenAI SDK.

APIs. Requests use Chat Completions with a strict JSON-schema response
format. Models that only support the Responses API (the API rejects them on
Chat Completions) are switched automatically to the Responses API in
background mode (``background=True``, ``store=True``), polled until a
terminal status or the per-call deadline; on deadline or job cancellation
the response is cancelled at the provider. ``LLM_API`` can force either API.

Model differences handled here, learned from the model's own HTTP 400 and
remembered per model for the life of the process (at most one extra request
each, recorded in diagnostics as ``param_adaptations``):

* ``max_completion_tokens`` rejected -> ``max_tokens`` (and back);
* ``temperature`` or reasoning effort rejected -> sent without it;
* output cap above the model's limit ("at most N") -> cap lowered to N;
* strict ``json_schema`` output unsupported -> JSON mode with the schema in
  the instructions (Pydantic validation is unchanged).

One call = an ordered list of request *profiles* (for example "full" then
"reduced"). Each profile builds its own, smaller evidence payload. The
helper:

* estimates the input size and skips a profile above the input budget;
* on an over-long input, an output-token limit or a failed caller
  validation, moves to the next (smaller) profile;
* on invalid JSON retries once (next profile, or the same one when it is the
  last), then fails;
* retries HTTP 429 (Retry-After honoured), 5xx, timeouts and connection
  errors with exponential backoff, at most ``LLM_MAX_RETRIES`` times;
* rejects any response whose served model does not match the configured
  expectation (no silent model substitution).

Fallback: when Azure fails because its key is missing or rejected or the
deployment is not found, and GROQ_API_KEY and GROQ_MODEL are set, the same
request (same profiles, schema and caller validation) is sent to Groq's
OpenAI-compatible Chat Completions API. Diagnostics then carry
``provider: "groq"`` and ``fallback_from``. No other failure falls back.

Diagnostics contain sizes, timings, outcomes, status codes, response IDs,
the served model and provider-reported token usage only. Prompts, model
output, provider error messages and credentials are never recorded.
"""

import contextlib
import json
import logging
import math
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pydantic import ValidationError


logger = logging.getLogger("insightiq.ai")

PROVIDER = "azure_openai"
FALLBACK_PROVIDER = "groq"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
# Only key/configuration failures switch to Groq; outages, rate limits and
# answers that fail validation are reported as they are.
FALLBACK_REASONS = {"credential_missing", "authentication_failed", "deployment_unavailable"}
ESTIMATED_BYTES_PER_TOKEN = 3.0
# Exponential backoff: 2s, 4s, 8s ... capped, for 429 / 5xx / timeouts.
TRANSIENT_BACKOFF_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 60.0
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
        "deployment": s.llm_model,
        "model": s.llm_model,
        "endpoint": s.azure_openai_endpoint,
        "api": s.llm_api,
        "temperature": s.llm_temperature,
        "max_output_tokens": s.llm_max_output_tokens,
        "max_retries": s.llm_max_retries,
        "reasoning_effort": s.llm_reasoning_effort,
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
        "http_timeout_seconds": s.llm_timeout_seconds,
        "max_concurrent_calls": s.azure_max_concurrent_calls,
        "expected_model_prefix": s.llm_expected_model_prefix or s.llm_model,
    }


def configured_deployment():
    try:
        return ai_runtime_settings()["deployment"]
    except Exception:
        return None


def provider_label(deployment=None, model=None, provider=None):
    """Human label such as 'Azure OpenAI · <model>' or 'Groq · <model>'."""
    if provider == FALLBACK_PROVIDER:
        return f"Groq · {model or deployment or 'model not reported'}"
    name = model or deployment or configured_deployment() or "model not configured"
    return f"Azure OpenAI · {name}"


def fallback_settings():
    """Groq fallback settings (no secret values), or None when unreadable."""
    try:
        from app.core.config import get_settings

        s = get_settings()
    except Exception:
        return None
    key = s.groq_api_key.get_secret_value().strip() if s.groq_api_key else ""
    return {
        "provider": FALLBACK_PROVIDER,
        "model": s.groq_model,
        "configured": bool(key and s.groq_model),
        "input_token_budget": s.groq_input_token_budget,
        "max_output_tokens": s.groq_max_output_tokens,
        "http_timeout_seconds": s.groq_http_timeout_seconds,
    }


def fallback_client(timeout_seconds=None):
    """Official OpenAI SDK pointed at Groq's OpenAI-compatible API, or None."""
    try:
        from app.core.config import get_settings

        s = get_settings()
        key = s.groq_api_key.get_secret_value().strip() if s.groq_api_key else ""
        timeout = timeout_seconds or s.groq_http_timeout_seconds
    except Exception:
        return None
    if not key:
        return None
    from openai import OpenAI

    return OpenAI(base_url=GROQ_BASE_URL, api_key=key, timeout=timeout, max_retries=0)


def default_client(timeout_seconds=None):
    """Official OpenAI SDK pointed at the Azure v1 endpoint, or None."""
    try:
        from app.core.config import get_settings

        s = get_settings()
        key = s.azure_openai_api_key.get_secret_value()
        endpoint = s.azure_openai_endpoint
        timeout = timeout_seconds or s.llm_timeout_seconds
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
        **({"fallback_from": d["fallback_from"]} if d.get("fallback_from") else {}),
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


# ------------------------------------------------------------ model quirks
# What a model rejected, learned from its own HTTP 400 errors, per
# (provider, model). Kept for the life of the process.

_QUIRKS = {}
_QUIRKS_GUARD = threading.Lock()
TOKEN_PARAMS = ("max_completion_tokens", "max_tokens", "max_output_tokens")
_RESPONSES_ONLY_CODES = {"operationnotsupported", "unsupported_api_for_model"}
_RESPONSES_ONLY_TEXT = (
    "only supported in v1/responses",
    "chat completions operation does not work",
    "not supported in v1/chat/completions",
    "use the responses api",
)
_PARAM_ALIASES = {
    "temperature": ("drop", "temperature"),
    "reasoning_effort": ("drop", "reasoning"),
    "reasoning": ("drop", "reasoning"),
    "reasoning.effort": ("drop", "reasoning"),
    "max_completion_tokens": ("token_param", "max_tokens"),
    "max_tokens": ("token_param", "max_completion_tokens"),
    "response_format": ("json_mode", "json_object"),
    "text.format": ("json_mode", "json_object"),
    "response_format.json_schema": ("json_mode", "json_object"),
}


def reset_model_quirks():
    """Forget learned model behaviour (tests; a changed model is a new key anyway)."""
    with _QUIRKS_GUARD:
        _QUIRKS.clear()


def _quirks_for(key):
    with _QUIRKS_GUARD:
        return _QUIRKS.setdefault(key, {"drop": set()})


def _error_message(error):
    """Provider error text, lower-cased, for matching only (never recorded)."""
    body = getattr(error, "body", None)
    text = ""
    if isinstance(body, dict):
        inner = body.get("error") if isinstance(body.get("error"), dict) else body
        text = str(inner.get("message") or "")
    return (text or str(getattr(error, "message", "") or "")).lower()


def _adaptation(error, sent, current_cap):
    """For an HTTP 400 rejecting a request parameter: the change that makes the
    model accept the request, as (kind, value), else None."""
    import openai

    if not isinstance(error, openai.APIStatusError) or getattr(error, "status_code", None) != 400:
        return None
    code, param = _error_code(error)
    code = (code or "").lower()
    param = (param or "").lower()
    message = _error_message(error)
    if code in _RESPONSES_ONLY_CODES or any(text in message for text in _RESPONSES_ONLY_TEXT):
        return ("api", "responses")
    if param in TOKEN_PARAMS or any(name in message for name in TOKEN_PARAMS):
        limit = re.search(r"at most (\d+)", message)
        if limit and 0 < int(limit.group(1)) < current_cap:
            return ("output_limit", int(limit.group(1)))
    names = [param] if param else []
    names += [name for name in sent if name in message]
    for name in names:
        if name in _PARAM_ALIASES and (name in sent or name.split(".")[0] in sent):
            return _PARAM_ALIASES[name]
    return None


def _apply_adaptation(quirks, adaptation):
    kind, value = adaptation
    if kind == "drop":
        quirks["drop"].add(value)
    else:
        quirks[kind] = value


def _schema_instruction(schema):
    return (
        "Return only a JSON object that matches this JSON schema exactly "
        "(no other text):\n" + json.dumps(schema, ensure_ascii=False)
    )


# --------------------------------------------------------------- transports
# Each sends one request and returns a normalised reply.

@dataclass
class _Reply:
    status: str  # completed | incomplete | failed | cancelled
    model: str | None = None
    text: str = ""
    refused: bool = False
    usage: dict | None = None
    response_id: str | None = None
    incomplete_reason: str | None = None
    error_code: str | None = None


def chat_usage(response):
    usage = _get(response, "usage")
    if usage is None:
        return None
    return {
        "input_tokens": _get(usage, "prompt_tokens"),
        "output_tokens": _get(usage, "completion_tokens"),
        "reasoning_tokens": _get(_get(usage, "completion_tokens_details"), "reasoning_tokens"),
        "cached_input_tokens": _get(_get(usage, "prompt_tokens_details"), "cached_tokens"),
        "total_tokens": _get(usage, "total_tokens"),
    }


def _send_temperature(target, quirks):
    # Reasoning models (a reasoning effort is configured) do not take sampling
    # parameters; others get the temperature unless the model rejected it.
    return (target["temperature"] is not None and not target["effort"]
            and "temperature" not in quirks["drop"])


def _chat_request(target, quirks, messages, schema_name, schema, max_output):
    """(kwargs, optional parameter names sent) for Chat Completions."""
    messages = [{"role": m["role"], "content": m["content"]} for m in messages]
    if quirks.get("json_mode") == "json_object":
        response_format = {"type": "json_object"}
        messages = [{"role": "system", "content": _schema_instruction(schema)}, *messages]
    else:
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "schema": schema, "strict": True},
        }
    token_param = quirks.get("token_param", "max_completion_tokens")
    kwargs = {"model": target["model"], "messages": messages, "response_format": response_format,
              token_param: max_output}
    sent = {"response_format", token_param}
    if _send_temperature(target, quirks):
        kwargs["temperature"] = target["temperature"]
        sent.add("temperature")
    if target["effort"] and "reasoning" not in quirks["drop"]:
        kwargs["reasoning_effort"] = target["effort"]
        sent.add("reasoning_effort")
    return kwargs, sent


def _chat_reply(response):
    choice = (_get(response, "choices") or [None])[0]
    finish_reason = _get(choice, "finish_reason")
    message = _get(choice, "message")
    reply = _Reply(
        status="completed", model=_get(response, "model"), text=_get(message, "content") or "",
        refused=bool(_get(message, "refusal")), usage=chat_usage(response),
        response_id=_get(response, "id"),
    )
    if finish_reason == "length":
        reply.status, reply.incomplete_reason = "incomplete", "max_output_tokens"
    elif finish_reason == "content_filter":
        reply.status, reply.incomplete_reason = "incomplete", "content_filter"
    return reply


def _responses_request(target, quirks, messages, schema_name, schema, max_output):
    instructions, items = split_messages(messages)
    if quirks.get("json_mode") == "json_object":
        fmt = {"format": {"type": "json_object"}}
        instructions = (instructions + "\n\n" if instructions else "") + _schema_instruction(schema)
    else:
        fmt = {"format": {"type": "json_schema", "name": schema_name, "schema": schema, "strict": True}}
    kwargs = {"model": target["model"], "instructions": instructions, "input": items, "text": fmt,
              "max_output_tokens": max_output, "background": True, "store": True}
    sent = {"text.format", "max_output_tokens"}
    if _send_temperature(target, quirks):
        kwargs["temperature"] = target["temperature"]
        sent.add("temperature")
    if target["effort"] and "reasoning" not in quirks["drop"]:
        kwargs["reasoning"] = {"effort": target["effort"]}
        sent.add("reasoning")
    return kwargs, sent


def _responses_reply(response):
    status = _get(response, "status")
    text, refused = response_text(response)
    reply = _Reply(
        status=status, model=_get(response, "model"), text=text, refused=refused,
        usage=response_usage(response), response_id=_get(response, "id"),
    )
    if status == "incomplete":
        reply.incomplete_reason = str(_get(_get(response, "incomplete_details"), "reason") or "unknown")
    if status == "failed":
        reply.error_code = str(_get(_get(response, "error"), "code") or "")[:60] or None
    return reply


# ----------------------------------------------------------------- the call

def _azure_target(client=None):
    settings = ai_runtime_settings()
    if client is None:
        client = default_client(settings["http_timeout_seconds"])
    return {
        "provider": PROVIDER,
        "client": client,
        "model": settings["model"],
        "api": settings["api"],
        "expected_prefix": settings["expected_model_prefix"],
        "input_token_budget": settings["input_token_budget"],
        "max_output_tokens": settings["max_output_tokens"],
        "temperature": settings["temperature"],
        "effort": settings["reasoning_effort"] if settings["reasoning_effort"] != "none" else None,
        "timeout": settings["http_timeout_seconds"],
    }


GROQ_EFFORT = {"none": None, "minimal": "low", "low": "low", "medium": "medium", "high": "high", "xhigh": "high"}


def _groq_target(client, fallback):
    settings = ai_runtime_settings()
    return {
        "provider": FALLBACK_PROVIDER,
        "client": client,
        "model": fallback.get("model"),
        "api": "chat",
        "expected_prefix": fallback.get("model"),
        "input_token_budget": fallback.get("input_token_budget"),
        "max_output_tokens": fallback.get("max_output_tokens"),
        "temperature": settings["temperature"],
        "effort": GROQ_EFFORT.get(settings["reasoning_effort"] or "none"),
        "timeout": fallback.get("http_timeout_seconds") or settings["http_timeout_seconds"],
    }


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
    groq_client=None,
    sleep=time.sleep,
    clock=time.monotonic,
):
    """
    Run one bounded structured request. Returns (parsed, validated, diagnostics).

    ``validate(parsed, profile)`` may raise ``ResponseRejected`` (or any
    ValueError) to reject a response; its return value is passed back as
    ``validated``.

    Azure OpenAI is used first. Only when it fails for a key/configuration
    reason (``FALLBACK_REASONS``) and the Groq fallback is configured is the
    same request, with the same validation, sent to Groq.
    """
    common = dict(
        agent=agent, call_name=call_name, schema_name=schema_name, schema_model=schema_model,
        build_messages=build_messages, profiles=profiles, validate=validate,
        sleep=sleep, clock=clock,
    )
    try:
        return _run_call(target=_azure_target(client), request_token_budget=request_token_budget, **common)
    except AICallFailed as failure:
        if failure.reason not in FALLBACK_REASONS:
            raise
        fallback = fallback_settings()
        if not (fallback and fallback["configured"]):
            raise
        if groq_client is None:
            groq_client = fallback_client(fallback["http_timeout_seconds"])
            if groq_client is None:
                raise
        primary = failure

    return _run_call(target=_groq_target(groq_client, fallback), primary=primary, **common)


def _run_call(
    *, target, agent, call_name, schema_name, schema_model, build_messages, profiles,
    validate=None, request_token_budget=None, primary=None, sleep=time.sleep, clock=time.monotonic,
):
    settings = ai_runtime_settings()
    client = target["client"]
    model = target["model"]
    budget = int(request_token_budget or target["input_token_budget"] or settings["input_token_budget"])
    if target["provider"] == FALLBACK_PROVIDER:
        # The fallback's own (smaller) budget always applies.
        budget = min(budget, int(target["input_token_budget"] or budget))
    expected_prefix = (target["expected_prefix"] or "").lower()
    max_retries = int(settings["max_retries"])
    quirks = _quirks_for((target["provider"], model))
    schema = strict_json_schema(schema_model)

    diagnostics = {
        "provider": target["provider"],
        "agent": agent,
        "call": call_name,
        "deployment": model,
        "model": None,
        "api": None,
        "status": "failed",
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_token_budget": budget,
        "reasoning_effort": target["effort"],
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
        "transient_retries": 0,
        "param_adaptations": [],
        "fallback_reason": None,
        "failure_reason": None,
    }
    if primary is not None:
        diagnostics["fallback_from"] = {"provider": PROVIDER, "reason": primary.reason}
        diagnostics["primary_attempts"] = (primary.diagnostics or {}).get("attempts", [])
    if not profiles:
        raise ValueError("At least one request profile is required.")

    started = clock()
    call_deadline = started + settings["deadline_seconds"]
    job_deadline = _job_deadline()
    if job_deadline is not None:
        call_deadline = min(call_deadline, job_deadline)
    max_requests = len(profiles) + 1
    first_problem = None
    wait_total = 0.0
    retries = 0
    json_retry_used = False
    applied = set()

    def finish(status, reason=None):
        diagnostics["status"] = status
        diagnostics["elapsed_seconds"] = round(clock() - started, 3)
        # Every response is billed, including ones rejected by schema or
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
        line = {k: diagnostics[k] for k in (
            "provider", "agent", "call", "deployment", "model", "api", "status", "profile_used",
            "provider_requests", "response_id", "input_tokens", "output_tokens",
            "reasoning_tokens", "elapsed_seconds", "rate_limit_retries", "transient_retries",
            "param_adaptations", "fallback_reason", "failure_reason",
        )}
        line["attempt_seconds"] = [a.get("elapsed_seconds") for a in diagnostics["attempts"]]
        if diagnostics.get("fallback_from"):
            line["fallback_from"] = diagnostics["fallback_from"]
        logger.info("ai_call %s", json.dumps(line))

    def fail(reason, status="failed"):
        finish(status, reason)
        return AICallFailed(reason, diagnostics)

    if client is None:
        raise fail("credential_missing")

    def check_cancel_and_deadline():
        if _job_cancelled():
            raise CallCancelled("job_cancelled")
        if clock() >= call_deadline:
            raise TimeoutError("deadline_exceeded")

    def request_timeout():
        remaining = call_deadline - clock()
        return max(1.0, min(float(target["timeout"]), remaining))

    def cancel_remote(response_id):
        if not response_id:
            return
        try:
            client.responses.cancel(response_id)
        except Exception:  # best effort; the outcome is recorded either way
            pass

    def poll(response, attempt):
        nonlocal wait_total
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
                    wait_total += wait
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

    def send(api, messages, max_output, attempt):
        if api == "responses":
            kwargs, sent = _responses_request(target, quirks, messages, schema_name, schema, max_output)
            response = client.responses.create(**kwargs, timeout=request_timeout())
            attempt["response_id"] = _get(response, "id")
            return _responses_reply(poll(response, attempt)), sent
        kwargs, sent = _chat_request(target, quirks, messages, schema_name, schema, max_output)
        return _chat_reply(client.chat.completions.create(**kwargs, timeout=request_timeout())), sent

    def sent_names(api, messages, max_output):
        builder = _responses_request if api == "responses" else _chat_request
        return builder(target, quirks, messages, schema_name, schema, max_output)[1]

    def backoff(error=None):
        """Seconds to wait before retry number ``retries + 1``, or None when exhausted."""
        wait = _retry_after(error) if error is not None else None
        if wait is None:
            wait = min(TRANSIENT_BACKOFF_SECONDS * (2 ** retries), MAX_BACKOFF_SECONDS)
        if (retries >= max_retries or wait > MAX_SINGLE_RATE_WAIT_SECONDS
                or wait_total + wait > MAX_TOTAL_RATE_WAIT_SECONDS or clock() + wait >= call_deadline):
            return None
        return wait

    with _slots(settings["max_concurrent_calls"]):
        for index, profile in enumerate(profiles):
            is_last = index == len(profiles) - 1
            messages = build_messages(profile)
            instructions, items = split_messages(messages)
            estimated = estimate_request_tokens(instructions, items, text_format(schema_name, schema_model))

            def output_cap():
                cap = int(profile.max_output_tokens)
                if target["max_output_tokens"]:
                    cap = min(cap, int(target["max_output_tokens"]))
                if quirks.get("output_limit"):
                    cap = min(cap, int(quirks["output_limit"]))
                return cap

            if estimated > budget:
                diagnostics["attempts"].append({
                    "profile": profile.name,
                    "outcome": "skipped_over_budget",
                    "status_code": None,
                    "estimated_input_tokens": estimated,
                    "max_output_tokens": output_cap(),
                })
                first_problem = first_problem or "skipped_over_budget"
                continue

            while True:
                if diagnostics["provider_requests"] >= max_requests:
                    diagnostics["attempt_limit_reached"] = True
                    raise fail(first_problem or "attempt_limit_reached")
                try:
                    check_cancel_and_deadline()
                except CallCancelled:
                    raise fail("cancelled", "cancelled") from None
                except TimeoutError:
                    raise fail("deadline_exceeded") from None

                api = "responses" if (target["api"] == "responses" or (
                    target["api"] == "auto" and quirks.get("api") == "responses")) else "chat"
                diagnostics["api"] = api
                max_output = output_cap()
                diagnostics["provider_requests"] += 1
                attempt = {
                    "profile": profile.name,
                    "api": api,
                    "outcome": None,
                    "status_code": None,
                    "response_id": None,
                    "estimated_input_tokens": estimated,
                    "max_output_tokens": max_output,
                }
                diagnostics["attempts"].append(attempt)
                attempt_started = clock()

                try:
                    reply, _ = send(api, messages, max_output, attempt)
                except CallCancelled:
                    attempt["outcome"] = "cancelled"
                    attempt["elapsed_seconds"] = round(clock() - attempt_started, 3)
                    raise fail("cancelled", "cancelled") from None
                except TimeoutError:
                    attempt["outcome"] = "deadline_exceeded"
                    attempt["elapsed_seconds"] = round(clock() - attempt_started, 3)
                    raise fail("deadline_exceeded") from None
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

                    adaptation = _adaptation(error, sent_names(api, messages, max_output), max_output)
                    if adaptation == ("api", "responses") and target["api"] != "auto":
                        adaptation = None
                    if adaptation and adaptation not in applied:
                        applied.add(adaptation)
                        _apply_adaptation(quirks, adaptation)
                        attempt["outcome"] = "parameter_adapted"
                        diagnostics["param_adaptations"].append(f"{adaptation[0]}:{adaptation[1]}")
                        diagnostics["provider_requests"] -= 1  # a rejected request produced nothing
                        continue

                    if code == 400 and provider_code == "json_validate_failed":
                        outcome, action = "invalid_schema", "invalid_json"
                        attempt["outcome"] = outcome

                    if action in ("rate", "retry"):
                        wait = backoff(error)
                        if wait is None:
                            raise fail("rate_limit_exhausted" if action == "rate" else outcome) from None
                        retries += 1
                        wait_total += wait
                        diagnostics["rate_limit_retries" if action == "rate" else "transient_retries"] += 1
                        diagnostics["provider_requests"] -= 1  # retries are bounded by LLM_MAX_RETRIES
                        sleep(wait)
                        continue

                    first_problem = first_problem or outcome
                    if action == "invalid_json":
                        if json_retry_used:
                            raise fail("invalid_schema") from None
                        json_retry_used = True
                        if is_last:
                            continue
                        break
                    if action == "next":
                        break
                    raise fail(outcome) from None

                attempt["elapsed_seconds"] = round(clock() - attempt_started, 3)
                attempt["response_id"] = reply.response_id
                attempt["response_status"] = reply.status
                attempt["model"] = reply.model
                if reply.usage:
                    attempt["usage"] = reply.usage

                if reply.status == "failed":
                    attempt["outcome"] = "provider_failed"
                    attempt["provider_error_code"] = reply.error_code
                    first_problem = first_problem or "provider_failed"
                    if reply.error_code in ("server_error", "rate_limit_exceeded"):
                        wait = backoff()
                        if wait is not None:
                            retries += 1
                            wait_total += wait
                            diagnostics["transient_retries"] += 1
                            diagnostics["provider_requests"] -= 1
                            sleep(wait)
                            continue
                    break
                if reply.status == "cancelled":
                    attempt["outcome"] = "cancelled_by_provider"
                    raise fail("cancelled_by_provider")
                if reply.status == "incomplete":
                    attempt["incomplete_reason"] = reply.incomplete_reason
                    attempt["outcome"] = (
                        "output_token_limit" if reply.incomplete_reason == "max_output_tokens"
                        else "content_filtered" if reply.incomplete_reason == "content_filter"
                        else "incomplete_response"
                    )
                    first_problem = first_problem or attempt["outcome"]
                    break

                if expected_prefix and not str(reply.model or "").lower().startswith(expected_prefix):
                    attempt["outcome"] = "unexpected_model"
                    diagnostics["model"] = reply.model
                    raise fail("unexpected_model")

                if reply.refused or not reply.text:
                    attempt["outcome"] = "refused" if reply.refused else "empty_response"
                    first_problem = first_problem or attempt["outcome"]
                    break
                try:
                    parsed = schema_model.model_validate_json(reply.text)
                except (ValidationError, ValueError):
                    attempt["outcome"] = "invalid_schema"
                    first_problem = first_problem or "invalid_schema"
                    if json_retry_used:
                        raise fail("invalid_schema") from None
                    json_retry_used = True
                    if is_last:
                        continue
                    break
                try:
                    validated = validate(parsed, profile) if validate else None
                except (ResponseRejected, ValueError) as rejection:
                    attempt["outcome"] = "validation_rejected"
                    attempt["validation_reason"] = str(rejection)[:200]
                    first_problem = first_problem or "validation_rejected"
                    break

                attempt["outcome"] = "success"
                diagnostics.update({
                    "profile_used": profile.name,
                    "model": reply.model,
                    "estimated_input_tokens": estimated,
                    "max_output_tokens": max_output,
                    "response_id": reply.response_id,
                    "response_status": reply.status,
                    "token_usage_reported_by_provider": reply.usage is not None,
                    **(reply.usage or {}),
                })
                finish("completed")
                return parsed, validated, diagnostics

    raise fail(first_problem or "no_profile_succeeded")
