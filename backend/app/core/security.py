"""
HTTP hardening and request telemetry for the API.

* Secret redaction: a logging filter removes configured secret values,
  bearer tokens, key/password assignments and URL credentials from every
  log record.
* Rate limiting: sliding one-minute window per client and route class
  (RATE_LIMIT_PER_MINUTE, RATE_LIMIT_EXPENSIVE_PER_MINUTE for actions that
  call providers or AI). Limits are per process; health checks are exempt.
* Proxy trust: the client address is the one uvicorn resolved, which honours
  X-Forwarded-For only from TRUSTED_PROXY_IPS (see server_entrypoint).
* Security headers on every response; HSTS when the request arrived over
  HTTPS; optional redirect of plain HTTP (FORCE_HTTPS=true).
* Request telemetry: one trace per request (X-Trace-Id response header), an
  "api" span, one structured log line and an audit event for every
  state-changing request (no bodies, no credentials).
"""

import logging
import os
import re
import threading
import time
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, RedirectResponse

SECRET_ENV_NAMES = (
    "API_ACCESS_KEY", "AZURE_OPENAI_API_KEY", "GROQ_API_KEY", "NEWSDATA_API_KEY", "FINNHUB_API_KEY",
    "FMP_API_KEY", "DB_PASSWORD", "DATABASE_URL",
)
_PATTERNS = (
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\b(api[_-]?key|apikey|token|password|passwd|secret|access[_-]?key)\b(\s*[=:]\s*)"
                r"(\"?)[^\s\"&,;]{4,}"), r"\1\2\3[REDACTED]"),
    (re.compile(r"(?i)(postgres(?:ql)?://[^:/\s]+:)[^@\s]+@"), r"\1[REDACTED]@"),
    (re.compile(r"\b(gsk|sk)-?_?[A-Za-z0-9]{16,}\b"), "[REDACTED]"),
)


def redact(text):
    if not text:
        return text
    for name in SECRET_ENV_NAMES:
        value = (os.getenv(name) or "").strip()
        if len(value) >= 6:
            text = text.replace(value, "[REDACTED]")
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg, record.args = cleaned, ()
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


_REDACTION_INSTALLED = {"done": False}


def install_redaction():
    """Attach the filter to every handler of the root and uvicorn loggers."""
    if _REDACTION_INSTALLED["done"]:
        return
    flt = RedactingFilter()
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access", "insightiq"):
        logger = logging.getLogger(name)
        logger.addFilter(flt)
        for handler in logger.handlers:
            handler.addFilter(flt)
    logging.getLogger().addFilter(flt)
    _REDACTION_INSTALLED["done"] = True


# ---------------------------------------------------------------- limiting

EXPENSIVE = re.compile(
    r"^/api/v1/(jobs|research|companies/[^/]+/(refresh|summary|brief|reports/[^/]+/(summary|brief))"
    r"|markets/[^/]+/(companies/search|companies/[^/]+/(data|refresh)|quotes/refresh|sectors/.+)"
    r"|news/refresh)"
)


class RateLimiter:
    def __init__(self):
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key, limit, now=None, window=60.0):
        """(allowed, retry_after_seconds)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] >= window:
                hits.popleft()
            if len(hits) >= limit:
                return False, max(1, int(window - (now - hits[0])) + 1)
            hits.append(now)
            return True, 0

    def reset(self):
        with self._lock:
            self._hits.clear()


LIMITER = RateLimiter()


def _limit_for(method, path):
    if method in ("POST", "PUT", "PATCH", "DELETE") and EXPENSIVE.match(path):
        return "expensive", int(os.getenv("RATE_LIMIT_EXPENSIVE_PER_MINUTE", "20"))
    return "default", int(os.getenv("RATE_LIMIT_PER_MINUTE", "240"))


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}


class HardeningMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        from app.services import telemetry

        path = request.url.path
        method = request.method
        client_ip = request.client.host if request.client else None
        https = request.url.scheme == "https"

        if os.getenv("FORCE_HTTPS", "").lower() in ("1", "true", "yes") and not https \
                and not path.startswith("/health"):
            return RedirectResponse(str(request.url.replace(scheme="https")), status_code=308)

        if not path.startswith("/health") and os.getenv("RATE_LIMIT_ENABLED", "true").lower() != "false":
            bucket, limit = _limit_for(method, path)
            allowed, retry = LIMITER.check(f"{client_ip}|{bucket}", limit)
            if not allowed:
                telemetry.log_event("rate_limited", method=method, path=path, bucket=bucket,
                                    client=telemetry.client_hash(client_ip))
                return _secure(JSONResponse({"detail": "Too many requests. Try again shortly."},
                                            status_code=429, headers={"Retry-After": str(retry)}), https)

        with telemetry.span("api", f"{method} {path}", method=method) as extra:
            started = time.perf_counter()
            response = await call_next(request)
            route = request.scope.get("route")
            template = getattr(route, "path", None) or "unmatched"
            extra.update({"route": template, "status_code": response.status_code})
            if response.status_code >= 500:
                extra["_status"] = "error"
            trace_id = telemetry.current_trace_id()
        duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        response.headers["X-Trace-Id"] = trace_id or ""
        telemetry.log_event("http_request", trace_id=trace_id, method=method, route=template,
                            status=response.status_code, duration_ms=duration_ms,
                            client=telemetry.client_hash(client_ip))
        if method in ("POST", "PUT", "PATCH", "DELETE") and path.startswith("/api/"):
            outcome = "ok" if response.status_code < 400 else f"http_{response.status_code}"
            telemetry.audit(f"{method} {template}", target=path, outcome=outcome, client_ip=client_ip,
                            details={"status_code": response.status_code, "trace_id": trace_id})
        return _secure(response, https)


def _secure(response, https):
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    response.headers.setdefault("Cache-Control", "no-store")
    if https:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


def uvicorn_options():
    """Host, proxy trust and TLS options for uvicorn from the environment."""
    options = {
        "host": os.getenv("INSIGHTIQ_API_HOST", "127.0.0.1"),
        "proxy_headers": True,
        "forwarded_allow_ips": os.getenv("TRUSTED_PROXY_IPS", "127.0.0.1"),
        "server_header": False,
    }
    certfile, keyfile = os.getenv("SSL_CERTFILE", "").strip(), os.getenv("SSL_KEYFILE", "").strip()
    if certfile or keyfile:
        if not (certfile and keyfile and os.path.isfile(certfile) and os.path.isfile(keyfile)):
            raise RuntimeError("SSL_CERTFILE and SSL_KEYFILE must both point to readable files.")
        options.update(ssl_certfile=certfile, ssl_keyfile=keyfile)
        ca = os.getenv("SSL_CA_CERTS", "").strip()
        if ca:
            options["ssl_ca_certs"] = ca
    return options
