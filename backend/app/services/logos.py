"""
Company logo metadata, resolved from each company's own stored provider
profile (no extra provider calls; profiles are cached in market_companies).

Order of candidates (the browser falls back on load errors):
1. the logo URL the market-data provider returned for exactly this symbol
   (Finnhub `logo`, FMP `image`), accepted only from the provider's own hosts;
2. the company's official website (provider profile `website`): its own
   `/apple-touch-icon.png` and `/favicon.ico`;
3. none -> the UI shows the company's initials.

Logos are never matched by name, never taken from another symbol, and no
per-company URLs are maintained by hand.
"""

import re
from urllib.parse import urlsplit

from app.services import market_registry as registry
from app.services import market_storage as storage

PROVIDER_LOGO_HOSTS = (
    "static.finnhub.io", "static2.finnhub.io", "finnhub.io",
    "financialmodelingprep.com", "images.financialmodelingprep.com",
)
_HOST_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
MAX_SYMBOLS = 100


def website_domain(website):
    """'https://www.tcs.com/en' -> 'www.tcs.com'; None when not a valid public host."""
    text = str(website or "").strip()
    if not text:
        return None
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme not in ("http", "https") or not _HOST_RE.match(host):
        return None
    if host in ("localhost",) or host.endswith((".local", ".internal")):
        return None
    return host


def provider_logo(url):
    text = str(url or "").strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not any(host == h or host.endswith("." + h) for h in PROVIDER_LOGO_HOSTS):
        return None
    if not parts.path or parts.path == "/":
        return None
    return text


def resolve(symbol, stored=None):
    """Logo metadata for one symbol from its own stored profile."""
    symbol = registry.require_symbol(symbol)
    stored = stored if stored is not None else storage.get_company(symbol)
    profile = (stored or {}).get("profile") or {}
    domain = website_domain(profile.get("website"))
    logo = provider_logo(profile.get("logo_url"))
    candidates = []
    if logo:
        candidates.append({"url": logo, "source": "provider"})
    if domain:
        candidates.append({"url": f"https://{domain}/apple-touch-icon.png", "source": "official_website"})
        candidates.append({"url": f"https://{domain}/favicon.ico", "source": "official_website"})
    return {"symbol": symbol, "name": (stored or {}).get("name"), "domain": domain, "candidates": candidates}


def resolve_many(symbols):
    clean = []
    for symbol in symbols[:MAX_SYMBOLS]:
        try:
            clean.append(registry.require_symbol(symbol))
        except Exception:
            continue
    return {s: resolve(s) for s in dict.fromkeys(clean)}
