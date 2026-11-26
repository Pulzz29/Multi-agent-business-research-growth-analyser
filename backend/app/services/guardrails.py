"""
Reusable AI guardrails (deterministic, no model calls).

* clean_input / input_violation: length cap, control and zero-width character
  removal, and detection of prompt injection, prompt extraction, role-play and
  jailbreaks, model/tool tampering, credential requests, code/SQL/shell/path
  payloads, markup and encoded payloads.
* scrub_untrusted: removes instruction-like sentences from fetched data
  (reports, news, provider text) before it reaches a model (indirect injection).
* output_leak: scans a model answer for system-prompt text, secrets, env
  variable names/values, file paths and markup before it is returned.
* valid_ticker: strict pattern for anything used in a query, URL or path.
* allow: per-user sliding-window rate limits.

These checks reduce, but cannot eliminate, prompt-injection risk.
"""

import os
import re
import unicodedata

GUARDRAIL_VERSION = "2026.11.26-1"
MAX_INPUT_CHARS = 1000
TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9&.\-]{0,19}$")
_ZERO_WIDTH = re.compile(r"[​-‏ -‮⁠-⁤﻿]")

INPUT_RULES = [
    ("instruction_override", re.compile(
        r"\b(ignore|disregard|forget|override|bypass)\b[^.?!]{0,40}\b(instruction|instructions|rules|guardrails|"
        r"prompt|prompts|polic(y|ies)|restrictions|previous|prior|above|earlier|before)\b", re.I)),
    ("prompt_disclosure", re.compile(
        r"\b(system|hidden|initial|developer|internal)\s+(prompt|message|instructions?)\b|"
        r"\b(reveal|show|print|repeat|output|tell me|what (is|are))\b[^.?!]{0,30}\b(your|the)\s+"
        r"(prompt|instructions|rules|configuration)\b", re.I)),
    ("role_manipulation", re.compile(
        r"\byou are now\b|\bact as\b|\bpretend (to be|you)\b|\brole-?play\b|\bdeveloper mode\b|\bjailbreak|"
        r"\bdo anything now\b|\bDAN\b|\bunfiltered\b|\bno restrictions\b", re.I)),
    ("model_or_tool_tampering", re.compile(
        r"\b(change|switch|swap|set|override|use a different)\b[^.?!]{0,20}\b(your|the)\s+"
        r"(model|temperature|tools?|functions?|system|settings)\b|\bcall (the |a )?(tool|function)\b|"
        r"\brun (this|the following) (code|command|script)\b|\bexecute\b", re.I)),
    ("cross_user_access", re.compile(
        r"\b(another|other|different) users?'?s?\b|\bsomeone else'?s\b|\b(all|every) users?\b|"
        r"\busers?'? (chats?|conversations?|histor(y|ies)|data|passwords?|hashes)\b|"
        r"\bbypass (the )?(auth|authentication|login|security|confirmation)\b|\badmin(istrator)? "
        r"(access|mode|privileges|rights)\b", re.I)),
    ("unauthorized_operation", re.compile(
        r"\b(every|all|each) compan(y|ies)\b[^.?!]{0,40}\b(research|run|start|generate)\b|"
        r"\b(research|run|start|generate)\b[^.?!]{0,40}\b(every|all|each) compan(y|ies)\b|"
        r"\bwithout (asking|confirmation|confirming|permission|approval)\b|"
        r"\b(disable|turn off|remove)\b[^.?!]{0,20}\b(guardrails?|safety|filters?|validation|restrictions)\b", re.I)),
    ("code_execution", re.compile(
        r"\b(select|delete|update|insert)\b[^.?!]{0,40}\b(from|into|set)\b|\bdrop\s+table\b|\bunion\s+select\b|;\s*--|"
        r"\brm\s+-rf\b|\b(curl|wget|powershell|cmd\.exe|bash\s+-c|subprocess|os\.system|eval\()|\$\(|`|"
        r"(^|\s)/etc/|[a-z]:\\\\|\.\./|\bfile://", re.I)),
    ("credential_request", re.compile(
        r"\bapi[\s_-]?keys?\b|\bsecret[\s_-]?keys?\b|\bpasswords?\b|\bpasswd\b|\baccess[\s_-]?tokens?\b|\bbearer\b|"
        r"\bcredentials?\b|(^|\s)\.env\b|\benv(ironment)? var(iable)?s?\b|\bconnection string\b|\bdatabase_url\b|"
        r"\bprivate key\b|\bsession (cookie|token)\b", re.I)),
    ("markup", re.compile(r"<\s*/?\s*(script|iframe|img|svg|object|embed|style)\b|javascript:|\bon\w+\s*=", re.I)),
    ("encoded_payload", re.compile(r"[A-Za-z0-9+/]{60,}={0,2}|(\\x[0-9a-f]{2}){8,}|(%[0-9a-f]{2}){8,}", re.I)),
]

ADVICE_RE = re.compile(r"\bshould (i|we)\b[^.?!]{0,30}\b(buy|sell|invest|hold|short)\b|\bis it a good "
                       r"(buy|investment|time)\b|\bprice target\b|\b(buy|sell) (or|vs\.?) (sell|hold|buy)\b", re.I)
ADVICE_NOTE = "This is report-based information, not financial advice."

_UNTRUSTED_PATTERNS = [rule for name, rule in INPUT_RULES if name in (
    "instruction_override", "prompt_disclosure", "role_manipulation", "model_or_tool_tampering", "markup")]
_SENTENCE = re.compile(r"[^.!?\n]*[.!?\n]?")

_SECRET_NAME = re.compile(r"(KEY|SECRET|PASSWORD|TOKEN|CREDENTIAL|DATABASE_URL)", re.I)
OUTPUT_RULES = [
    ("prompt_text", re.compile(r"\bsystem prompt\b|\bmy (instructions|rules|prompt)\b|\buntrusted_data\b|"
                               r"<\s*/?\s*(report_data|user_question|conversation)\s*>", re.I)),
    ("secret_like", re.compile(r"\bsk-[A-Za-z0-9_-]{8,}|\bgsk_[A-Za-z0-9]{8,}|\bBearer\s+[A-Za-z0-9._-]{8,}|"
                               r"-----BEGIN [A-Z ]*KEY-----|\bapi[\s_-]?key\b|\bpassword\b", re.I)),
    ("env_name", re.compile(r"\b(AZURE_OPENAI_[A-Z_]+|API_ACCESS_KEY|DB_PASSWORD|DATABASE_URL|"
                            r"[A-Z][A-Z0-9]*_(API_KEY|SECRET|PASSWORD|TOKEN))\b")),
    ("file_path", re.compile(r"(?<![A-Za-z])[A-Za-z]:\\[^\s]+|(^|\s)/(etc|home|usr|var|app|root|tmp)/\S+|"
                             r"\b\S+\.(py|env|sqlite3|pem|key|ini|cfg|yaml|yml)\b", re.I)),
    ("markup", re.compile(r"<\s*(script|iframe|img|svg|object|embed)|javascript:", re.I)),
]


def clean_input(text):
    """NFKC, no control or zero-width characters, collapsed whitespace."""
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = _ZERO_WIDTH.sub("", text)
    text = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    return re.sub(r"\s+", " ", text).strip()


def input_violation(text):
    """Name of the first rule the (cleaned) input breaks, or None."""
    if len(text) > MAX_INPUT_CHARS:
        return "too_long"
    for name, pattern in INPUT_RULES:
        if pattern.search(text):
            return name
    return None


FABRICATION_RULES = [
    ("fabrication_request", re.compile(
        r"\b(invent|make up|made[- ]up|fabricate|fake|realistic (number|figure|value)|hypothetical (number|figure))\b|"
        r"\bcreate\b[^.?!]{0,30}\bcitations?\b|\bcitations?\b[^.?!]{0,30}\beven if\b", re.I)),
    ("unsupported_forecast_request", re.compile(
        r"\beven if\b[^.?!]{0,60}\b(insufficient|unavailable|missing|not available|no data|not enough)\b", re.I)),
    ("guarantee_request", re.compile(r"\bguarantee|\bpromise (me )?that\b|\b100% (sure|certain)\b", re.I)),
]


def fabrication_request(text):
    """Rule name when the user asks for invented values, citations, forecasts
    beyond the evidence or guarantees (answered without any model call)."""
    for name, pattern in FABRICATION_RULES:
        if pattern.search(text or ""):
            return name
    return None


def wants_advice(text):
    return bool(ADVICE_RE.search(text or ""))


def scrub_text(text):
    """Drop sentences of fetched data that read like instructions to a model."""
    text = _ZERO_WIDTH.sub("", str(text))
    if not any(p.search(text) for p in _UNTRUSTED_PATTERNS):
        return text
    kept = [s for s in _SENTENCE.findall(text) if s and not any(p.search(s) for p in _UNTRUSTED_PATTERNS)]
    return ("".join(kept).strip() + " [instruction-like text removed]").strip()


def scrub_untrusted(value):
    """Recursively scrub every string in untrusted data."""
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, list):
        return [scrub_untrusted(v) for v in value]
    if isinstance(value, dict):
        return {k: scrub_untrusted(v) for k, v in value.items()}
    return value


def _secret_values():
    return [v for k, v in os.environ.items() if _SECRET_NAME.search(k) and v and len(v) >= 8]


def output_leak(text, extra_forbidden=()):
    """Name of the first leak found in a model answer, or None."""
    for name, pattern in OUTPUT_RULES:
        if pattern.search(text or ""):
            return name
    for value in _secret_values():
        if value in text:
            return "secret_value"
    for fragment in extra_forbidden:
        if fragment and fragment.lower() in (text or "").lower():
            return "prompt_text"
    return None


def valid_ticker(value):
    """Upper-cased ticker when it matches the strict pattern, else None."""
    ticker = str(value or "").strip().upper()
    return ticker if TICKER_RE.match(ticker) else None


_LIMITER = None


def allow(user_id, action, limit, window_seconds):
    """Per-user sliding-window rate limit (reuses the app's RateLimiter)."""
    global _LIMITER
    from app.core.security import RateLimiter

    if _LIMITER is None:
        _LIMITER = RateLimiter()
    allowed, _ = _LIMITER.check(f"{action}|{user_id}", int(limit), window=float(window_seconds))
    return allowed


def reset():
    """Test helper: clear rate-limit state."""
    if _LIMITER is not None:
        _LIMITER.reset()
