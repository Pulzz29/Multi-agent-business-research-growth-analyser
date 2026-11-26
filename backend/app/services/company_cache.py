"""
Persistent company-data cache (table ``company_data_cache``).

* A row is valid only while ``status = 'ready'`` and ``expires_at > now``.
  ``expires_at = fetched_at + CACHE_TTL_HOURS`` is written only by a
  successful fetch; reading never changes it (no sliding expiry).
* Keys are normalised (trimmed, upper-case market and symbol; search text
  case-folded with single spaces), so a company is stored once and distinct
  symbols are never merged.
* One fetcher per key across processes and instances: a request takes a
  time-limited lease with one atomic UPDATE; others wait for its result.
  A lease left by a crashed process expires and can be taken over.
* Failed or incomplete fetches are never stored as ready rows.

Times are timezone-aware UTC. Expired rows are deleted by
``cleanup_expired`` (see cache_cleanup), and are never served before that.
"""

import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

from app.database import db

logger = logging.getLogger("insightiq.company_cache")

LEASE_SECONDS = 240.0
POLL_SECONDS = 0.5
COMPANY = "company"
SEARCH = "search"


def utc_now():
    return datetime.now(timezone.utc)


def ttl():
    from app.core.config import get_settings

    return timedelta(hours=float(get_settings().data_refresh_ttl_hours))


def normalise_market(market):
    return str(market or "").strip().upper()


def company_key(market, symbol):
    return f"{normalise_market(market)}:{str(symbol or '').strip().upper()}"


def search_key(market, query):
    text = re.sub(r"\s+", " ", str(query or "").strip()).casefold()
    return f"{normalise_market(market)}:{text}"


def _as_utc(value):
    if value is None or value == "":
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def new_owner():
    return uuid.uuid4().hex


# ------------------------------------------------------------------- reads

def get_valid(kind, key, now=None):
    """The cached entry when valid, else None:
    {"payload", "fetched_at", "expires_at"} (datetimes in UTC)."""
    now = now or utc_now()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT payload_json, fetched_at, expires_at FROM company_data_cache "
            "WHERE cache_kind = ? AND cache_key = ? AND status = 'ready' AND expires_at > ?",
            (kind, key, now),
        ).fetchone()
    if row is None:
        return None
    return {"payload": json.loads(row["payload_json"]),
            "fetched_at": _as_utc(row["fetched_at"]), "expires_at": _as_utc(row["expires_at"])}


def get_row(kind, key):
    """Raw row (any status), for diagnostics and tests."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM company_data_cache WHERE cache_kind = ? AND cache_key = ?", (kind, key),
        ).fetchone()
    if row is None:
        return None
    out = dict(row)
    for name in ("fetched_at", "expires_at", "refresh_lease_until", "created_at", "updated_at"):
        out[name] = _as_utc(out.get(name))
    return out


# ------------------------------------------------------------------ leases

def acquire_lease(kind, key, market, symbol, owner, now=None, lease_seconds=LEASE_SECONDS):
    """True when ``owner`` now holds the fetch lease for ``key``."""
    now = now or utc_now()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO company_data_cache(cache_kind, cache_key, market, symbol, status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, 'pending', ?, ?) ON CONFLICT DO NOTHING",
            (kind, key, normalise_market(market), symbol, now, now),
        )
        cursor = conn.execute(
            "UPDATE company_data_cache SET refresh_lease_until = ?, refresh_owner = ?, updated_at = ? "
            "WHERE cache_kind = ? AND cache_key = ? "
            "AND (refresh_lease_until IS NULL OR refresh_lease_until <= ? OR refresh_owner = ?)",
            (now + timedelta(seconds=lease_seconds), owner, now, kind, key, now, owner),
        )
        return cursor.rowcount == 1


def release_lease(kind, key, owner, error=None, now=None):
    """Give up the lease after a failed fetch; the stored entry is unchanged."""
    now = now or utc_now()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE company_data_cache SET refresh_lease_until = NULL, refresh_owner = NULL, "
            "last_error = ?, updated_at = ? "
            "WHERE cache_kind = ? AND cache_key = ? AND refresh_owner = ?",
            ((error or "")[:300] or None, now, kind, key, owner),
        )


def store(kind, key, market, symbol, payload, owner=None, fetched_at=None):
    """Save a complete, successful result: expiry = fetched_at + TTL.
    Returns the stored entry."""
    if payload is None:
        raise ValueError("Refusing to cache an empty result.")
    fetched_at = _as_utc(fetched_at) or utc_now()
    expires_at = fetched_at + ttl()
    text = json.dumps(payload, default=str, allow_nan=False)
    now = utc_now()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO company_data_cache(
                cache_kind, cache_key, market, symbol, status, payload_json,
                fetched_at, expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'ready', ?, ?, ?, ?, ?)
            ON CONFLICT (cache_kind, cache_key) DO UPDATE SET
                status = 'ready',
                payload_json = excluded.payload_json,
                fetched_at = excluded.fetched_at,
                expires_at = excluded.expires_at,
                market = excluded.market,
                symbol = excluded.symbol,
                refresh_lease_until = NULL,
                refresh_owner = NULL,
                last_error = NULL,
                updated_at = excluded.updated_at
            """,
            (kind, key, normalise_market(market), symbol, text, fetched_at, expires_at, now, now),
        )
    logger.info("company_cache store kind=%s key=%s expires_at=%s", kind, key, expires_at.isoformat())
    return {"payload": payload, "fetched_at": fetched_at, "expires_at": expires_at}


def wait_for(kind, key, now=None, timeout=180.0, sleep=time.sleep, clock=time.monotonic):
    """Wait until the current fetcher's lease ends, then return the entry if
    it is valid at ``now`` (the request's time), else None. Returns None when
    ``timeout`` passes first."""
    deadline = clock() + timeout
    while clock() < deadline:
        row = get_row(kind, key)
        if row is None or row.get("refresh_owner") is None or (
                row.get("refresh_lease_until") and row["refresh_lease_until"] <= utc_now()):
            return get_valid(kind, key, now)
        sleep(POLL_SECONDS)
    return None


# ----------------------------------------------------------------- cleanup

def cleanup_expired(now=None):
    """Delete expired cache rows and abandoned pending rows. Never touches
    any other table. Returns the number of rows deleted."""
    now = now or utc_now()
    with db.transaction() as conn:
        cursor = conn.execute(
            "DELETE FROM company_data_cache WHERE "
            "(refresh_lease_until IS NULL OR refresh_lease_until <= ?) AND ("
            "  (status = 'ready' AND expires_at <= ?) OR status = 'pending')",
            (now, now),
        )
        deleted = max(cursor.rowcount or 0, 0)
    if deleted:
        logger.info("company_cache cleanup deleted=%s", deleted)
    return deleted
