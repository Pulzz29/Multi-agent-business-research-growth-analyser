import json
import math
import re
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from groq import RateLimitError


MAX_RETRIES = 5
MAX_TOTAL_WAIT_SECONDS = 120.0
MAX_SINGLE_WAIT_SECONDS = 65.0

_REQUEST_LOCK = threading.RLock()
_EVENTS_LOCK = threading.Lock()
_RETRY_EVENTS = []


class GroqRateLimitExhausted(RuntimeError):
    pass


def retry_after_seconds(error):
    """
    Read server-provided Retry-After, or the
    explicit retry duration in Groq's 429 message.
    """
    response = getattr(error, "response", None)

    if response is not None:
        headers = getattr(response, "headers", {}) or {}

        raw = headers.get("retry-after")

        if raw:
            try:
                seconds = float(raw)

                if math.isfinite(seconds) and seconds >= 0:
                    return seconds
            except (TypeError, ValueError):
                try:
                    retry_at = parsedate_to_datetime(raw)

                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(
                            tzinfo=timezone.utc
                        )

                    seconds = (
                        retry_at
                        - datetime.now(timezone.utc)
                    ).total_seconds()

                    return max(0.0, seconds)

                except (TypeError, ValueError, OverflowError):
                    pass

    message = str(error)

    match = re.search(
        r"(?:please\s+)?try\s+again\s+in\s+"
        r"(\d+(?:\.\d+)?)\s*(ms|milliseconds?|s|seconds?)",
        message,
        flags=re.IGNORECASE,
    )

    if match:
        amount = float(match.group(1))
        unit = match.group(2).lower()

        if unit.startswith("m"):
            return amount / 1000.0

        return amount

    return None


def record_retry_event(event):
    # No API keys, request bodies, or complete
    # provider exceptions are written to diagnostics.
    with _EVENTS_LOCK:
        _RETRY_EVENTS.append(event)

        if len(_RETRY_EVENTS) > 200:
            del _RETRY_EVENTS[:-200]


def retry_events_since(index):
    with _EVENTS_LOCK:
        return list(_RETRY_EVENTS[index:])


def retry_event_count():
    with _EVENTS_LOCK:
        return len(_RETRY_EVENTS)


def guarded_chat_completion(client, **kwargs):
    """
    Synchronous, shared-process rate-limit protection.

    Retry only genuine HTTP 429 rate-limit errors.
    Never silently substitute generated data.

    Hold a shared lock through requests/recovery so
    concurrent in-process agents do not start new
    requests while a rate-limit recovery is active.
    """
    started = time.monotonic()
    retries = 0

    with _REQUEST_LOCK:
        while True:
            try:
                return client.chat.completions.create(
                    **kwargs
                )

            except RateLimitError as error:
                if retries >= MAX_RETRIES:
                    raise GroqRateLimitExhausted(
                        "Groq token limit remained unavailable "
                        f"after {retries} retries. "
                        "The research run was not completed."
                    ) from error

                suggested = retry_after_seconds(error)

                if suggested is None:
                    # Fallback only when provider retry
                    # guidance is missing. No fixed 65s sleep.
                    suggested = min(
                        2.0 * (2 ** retries),
                        20.0,
                    )

                    guidance = "bounded_exponential_fallback"

                else:
                    guidance = "provider_retry_guidance"

                # Small scheduling margin to avoid
                # retrying before the rolling window clears.
                wait_seconds = max(
                    0.0,
                    suggested + 0.25,
                )

                if wait_seconds > MAX_SINGLE_WAIT_SECONDS:
                    raise GroqRateLimitExhausted(
                        "Groq requested a retry delay outside "
                        "the configured single-wait budget."
                    ) from error

                elapsed = time.monotonic() - started

                if (
                    elapsed + wait_seconds
                    > MAX_TOTAL_WAIT_SECONDS
                ):
                    raise GroqRateLimitExhausted(
                        "Groq rate-limit recovery would exceed "
                        "the configured total waiting budget."
                    ) from error

                retries += 1

                record_retry_event({
                    "timestamp_utc": datetime.now(
                        timezone.utc
                    ).isoformat(),
                    "provider": "groq",
                    "model": kwargs.get("model"),
                    "status_code": 429,
                    "retry_number": retries,
                    "wait_seconds": round(
                        wait_seconds, 3
                    ),
                    "guidance": guidance,
                })

                print(
                    "[Groq 429] Token limit reached; "
                    f"retry {retries}/{MAX_RETRIES} "
                    f"using {guidance} after "
                    f"{wait_seconds:.2f}s."
                )

                # This wait occurs only after a real 429.
                time.sleep(wait_seconds)
