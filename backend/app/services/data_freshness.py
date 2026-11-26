"""
Freshness decision shared by features that cache provider data.

Rule (UTC only):
    no timestamp / unreadable timestamp  -> refresh ("never_refreshed")
    age equal to or greater than the TTL -> refresh ("stale"); data is invalid exactly TTL after it was fetched
    otherwise (age < TTL)                -> serve stored data ("fresh")

With a 24-hour TTL, 23:59:59 is fresh and exactly 24:00:00 is stale.
"""

from datetime import datetime, timedelta, timezone


def utc_now():
    """Current time in UTC (a function so tests can mock the clock)."""
    return datetime.now(timezone.utc)


def parse_utc(value):
    """ISO-8601 text -> aware UTC datetime; naive values are treated as UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def configured_ttl_hours():
    from app.core.config import get_settings

    return float(get_settings().data_refresh_ttl_hours)


def needs_refresh(last_refreshed_at, now=None, ttl_hours=None):
    """Return (refresh: bool, reason: str, age_hours: float | None)."""
    now = now or utc_now()
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware (UTC).")
    ttl_hours = configured_ttl_hours() if ttl_hours is None else float(ttl_hours)

    last = parse_utc(last_refreshed_at)
    if last is None:
        return True, "never_refreshed", None

    age = now.astimezone(timezone.utc) - last
    age_hours = age.total_seconds() / 3600
    if age >= timedelta(hours=ttl_hours):  # invalid exactly TTL after the fetch
        return True, "stale", age_hours
    return False, "fresh", age_hours
