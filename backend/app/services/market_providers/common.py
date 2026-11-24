"""
Shared plumbing for market-data provider adapters.

* Bounded retries on transport errors, HTTP 429 (honouring Retry-After up to
  a ceiling) and 5xx responses.
* A per-provider request spacing limiter (requests per minute).
* Every outgoing request is counted in ``provider_usage`` so daily quotas
  can be enforced.
* Errors carry a short ``safe_message``. Provider responses, URLs and keys
  are never placed in messages or logs.
"""

import logging
import math
import threading
import time
from decimal import Decimal, InvalidOperation

import httpx


logger = logging.getLogger("insightiq.market_providers")

MAX_RETRY_AFTER_SECONDS = 60.0


class ProviderError(RuntimeError):
    safe_message = "The data provider request failed."

    def __init__(self, safe_message=None):
        super().__init__(safe_message or self.safe_message)
        if safe_message:
            self.safe_message = safe_message


class ProviderNotConfigured(ProviderError):
    safe_message = "The data provider API key is not configured."


class ProviderQuotaExceeded(ProviderError):
    safe_message = "The data provider's daily request budget is used up."


class ProviderNoData(ProviderError):
    safe_message = "The data provider returned no data for this symbol."


class ProviderAccessDenied(ProviderError):
    safe_message = "The data provider rejected the request (key or plan does not cover this data)."


class RateLimiter:
    """Spaces requests to at most ``per_minute`` per minute (thread-safe)."""

    def __init__(self, per_minute, clock=time.monotonic, sleep=time.sleep):
        self.interval = 60.0 / max(1, per_minute)
        self.clock, self.sleep = clock, sleep
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self):
        with self._lock:
            now = self.clock()
            wait = self._next - now
            if wait > 0:
                self.sleep(wait)
                now = self.clock()
            self._next = max(now, self._next) + self.interval


_LIMITERS = {}
_LIMITERS_LOCK = threading.Lock()


def limiter_for(provider, per_minute):
    with _LIMITERS_LOCK:
        limiter = _LIMITERS.get(provider)
        if limiter is None:
            limiter = _LIMITERS[provider] = RateLimiter(per_minute)
        return limiter


def to_decimal_text(value):
    """Exact decimal text for provider numbers; None for missing/NaN."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        value = repr(value)
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    text = format(number.normalize(), "f")
    return text if "." not in text else text.rstrip("0").rstrip(".") or "0"


def to_float(value):
    text = to_decimal_text(value)
    return None if text is None else float(text)


def _retry_after(response):
    raw = response.headers.get("retry-after")
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def get_json(
    client, url, *, provider, limiter, params=None, headers=None,
    max_attempts=3, sleep=time.sleep, usage_hook=None,
):
    for attempt in range(max_attempts):
        limiter.acquire()
        if usage_hook:
            usage_hook(provider)
        try:
            response = client.get(url, params=params, headers=headers, follow_redirects=False)
        except (httpx.TimeoutException, httpx.TransportError):
            if attempt == max_attempts - 1:
                raise ProviderError(f"{provider} could not be reached.") from None
            sleep(min(2 ** attempt, 8))
            continue

        status = response.status_code
        if status == 200:
            try:
                return response.json()
            except ValueError:
                raise ProviderError(f"{provider} returned an unreadable response.") from None
        if status in (401, 402, 403):
            raise ProviderAccessDenied(
                f"{provider} rejected the request (HTTP {status}): the API key or plan "
                "does not cover this data."
            )
        if status == 404:
            raise ProviderNoData(f"{provider} has no data for this symbol.")
        if status == 429 or status >= 500:
            if attempt == max_attempts - 1:
                raise ProviderError(f"{provider} is busy or unavailable (HTTP {status}).")
            delay = _retry_after(response) if status == 429 else None
            if delay is not None and delay > MAX_RETRY_AFTER_SECONDS:
                raise ProviderError(f"{provider} asked to retry much later (rate limit).")
            sleep(delay if delay is not None else min(2 ** attempt, 8))
            continue
        raise ProviderError(f"{provider} returned HTTP {status}.")

    raise ProviderError(f"{provider} request attempts exhausted.")
