"""
Company universe and registry for the US and Indian markets.

Read functions merge the curated universe (core/market_universe.json) with
companies stored in ``market_companies`` and never write to the database.
Companies found through live provider search are registered only by an
explicit refresh (POST) action.
"""

import json
import re
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path

from app.services import market_storage


UNIVERSE_PATH = Path(__file__).resolve().parents[1] / "core" / "market_universe.json"
MARKETS = ("US", "IN")
SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9&.\-]{0,19}$")
SECTOR_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
LEGAL_SUFFIXES = re.compile(
    r"\b(inc\.?|incorporated|corp\.?|corporation|co\.?|company|ltd\.?|limited|plc|n\.v\.|s\.a\.|group|holdings?)\b",
    re.IGNORECASE,
)

# Companies on the validated SEC research pipeline (live set; see sec_coverage.py).
from app.services.sec_coverage import SEC_VALIDATED  # noqa: E402


class RegistryError(ValueError):
    pass


class UnknownMarket(RegistryError):
    pass


class UnknownSector(RegistryError):
    pass


@lru_cache(maxsize=1)
def load_universe(path=None):
    data = json.loads(Path(path or UNIVERSE_PATH).read_text(encoding="utf-8"))
    for market in MARKETS:
        if market not in data.get("markets", {}):
            raise RegistryError(f"Universe has no {market} market.")
        for sector_key, sector in data["markets"][market]["sectors"].items():
            if not SECTOR_KEY_RE.match(sector_key):
                raise RegistryError(f"Invalid sector key {sector_key}.")
            for company in sector["companies"]:
                if not SYMBOL_RE.match(company["symbol"]):
                    raise RegistryError(f"Invalid symbol {company['symbol']}.")
    return data


def require_market(market):
    market = str(market or "").strip().upper()
    if market not in MARKETS:
        raise UnknownMarket("Unknown market. Use US or IN.")
    return market


def require_symbol(symbol):
    symbol = str(symbol or "").strip().upper()
    if not SYMBOL_RE.match(symbol):
        raise RegistryError("Invalid company symbol.")
    return symbol


def market_config(market):
    return load_universe()["markets"][require_market(market)]


def markets():
    data = load_universe()
    return [
        {
            "id": key,
            "label": cfg["label"],
            "currency": cfg["currency"],
            "providers": cfg["providers"],
            "sector_count": len(cfg["sectors"]),
            "universe_company_count": sum(len(s["companies"]) for s in cfg["sectors"].values()),
        }
        for key, cfg in data["markets"].items()
    ]


def sector_config(market, sector_key):
    sectors = market_config(market)["sectors"]
    key = str(sector_key or "").strip().lower()
    if key not in sectors:
        raise UnknownSector("Unknown sector for this market.")
    return key, sectors[key]


def universe_entry(market, symbol):
    for sector_key, sector in market_config(market)["sectors"].items():
        for company in sector["companies"]:
            if company["symbol"] == symbol:
                return {**company, "sector_key": sector_key, "sector_label": sector["label"]}
    return None


def map_provider_sector(market, provider_sector, provider_industry):
    """Map provider sector/industry labels to an InsightIQ sector key (or None)."""
    sectors = market_config(market)["sectors"]
    mapping = load_universe().get("provider_sector_map", {})
    for label in (provider_industry, provider_sector):
        text = str(label or "").strip().lower()
        if not text:
            continue
        for sector_key in sectors:
            if text in (mapping.get(sector_key) or []):
                return sector_key
    return None


def _stored_companies(market):
    try:
        return market_storage.list_companies(market)
    except Exception:
        return []


def _number(text):
    try:
        return Decimal(str(text)) if text is not None else None
    except (InvalidOperation, ValueError):
        return None


def sector_members(market, sector_key, stored=None):
    """Universe companies plus registered companies mapped to the sector."""
    key, sector = sector_config(market, sector_key)
    members = {c["symbol"]: {**c, "in_universe": True} for c in sector["companies"]}
    for stored in (stored if stored is not None else _stored_companies(market)):
        if stored.get("sector_key") == key and stored["symbol"] not in members:
            members[stored["symbol"]] = {
                "symbol": stored["symbol"], "name": stored["name"], "in_universe": False,
            }
    return key, sector, list(members.values())


def sectors(market, search=None):
    market = require_market(market)
    query = str(search or "").strip().lower()
    out = []
    stored = _stored_companies(market)
    for key, sector in market_config(market)["sectors"].items():
        if query and query not in sector["label"].lower() and query not in key:
            continue
        _, _, members = sector_members(market, key, stored=stored)
        out.append({
            "key": key,
            "label": sector["label"],
            "company_count": len(members),
            "companies": [
                {"symbol": m["symbol"], "name": m["name"], "in_universe": m.get("in_universe", False)}
                for m in members
            ],
        })
    return out


def company_summary(market, symbol):
    """Merged universe + stored view of one company (read-only)."""
    market = require_market(market)
    symbol = require_symbol(symbol)
    entry = universe_entry(market, symbol)
    stored = market_storage.get_company(symbol)
    if stored and stored["market"] != market:
        stored = None
    if entry is None and stored is None:
        return None
    sector_key = (stored or {}).get("sector_key") or (entry or {}).get("sector_key")
    sector_label = None
    if sector_key:
        sector_label = market_config(market)["sectors"].get(sector_key, {}).get("label")
    quote = market_storage.latest_quote(symbol) if stored else None
    return {
        "symbol": symbol,
        "market": market,
        "name": (stored or {}).get("name") or entry["name"],
        "sector_key": sector_key,
        "sector_label": sector_label,
        "in_universe": entry is not None,
        "registered": stored is not None,
        "exchange": (stored or {}).get("exchange"),
        "currency": (stored or {}).get("currency") or market_config(market)["currency"],
        "provider_sector": (stored or {}).get("provider_sector"),
        "provider_industry": (stored or {}).get("provider_industry"),
        "profile": (stored or {}).get("profile"),
        "quote": quote,
        "research_profile": "sec_validated" if symbol in SEC_VALIDATED else "provider",
        "news_terms": (entry or {}).get("news_terms"),
    }


def search_local(market, query, limit=20):
    market = require_market(market)
    text = str(query or "").strip().lower()
    if not text:
        return []
    seen, results = set(), []
    candidates = []
    for sector_key, sector in market_config(market)["sectors"].items():
        for company in sector["companies"]:
            candidates.append((company["symbol"], company["name"], sector["label"], True))
    for stored in _stored_companies(market):
        label = market_config(market)["sectors"].get(stored.get("sector_key") or "", {}).get("label")
        candidates.append((stored["symbol"], stored["name"], label, False))
    for symbol, name, sector_label, in_universe in candidates:
        if symbol in seen:
            continue
        if text in symbol.lower() or text in str(name).lower():
            seen.add(symbol)
            results.append({
                "symbol": symbol, "name": name, "sector_label": sector_label,
                "in_universe": in_universe,
                "starts_with": symbol.lower().startswith(text) or str(name).lower().startswith(text),
            })
    results.sort(key=lambda r: (not r["starts_with"], r["symbol"]))
    return [{k: v for k, v in r.items() if k != "starts_with"} for r in results[:limit]]


def top_companies(market, limit=5):
    """Largest companies by the latest stored market capitalisation."""
    market = require_market(market)
    rows = []
    for stored in _stored_companies(market):
        quote = market_storage.latest_quote(stored["symbol"])
        cap = _number((quote or {}).get("market_cap"))
        if cap is None or cap <= 0:
            continue
        if (quote.get("currency") or market_config(market)["currency"]) != market_config(market)["currency"]:
            continue
        sector = market_config(market)["sectors"].get(stored.get("sector_key") or "", {})
        rows.append({
            "symbol": stored["symbol"],
            "name": stored["name"],
            "sector_label": sector.get("label"),
            "market_cap": str(cap),
            "price": quote.get("price"),
            "currency": quote.get("currency") or market_config(market)["currency"],
            "quote_provider": quote.get("provider"),
            "quote_fetched_at": quote.get("fetched_at"),
        })
    rows.sort(key=lambda r: (-Decimal(r["market_cap"]), r["symbol"]))
    universe_size = sum(len(s["companies"]) for s in market_config(market)["sectors"].values())
    return {
        "market": market,
        "companies": rows[:limit],
        "companies_with_market_cap": len(rows),
        "universe_size": universe_size,
        "basis": "latest stored market capitalisation from the market's quote provider",
    }


def research_profile(ticker):
    """
    "sec_validated" for the six SEC-validated companies, "provider" for any
    other company registered in market_companies, otherwise None.
    """
    ticker = str(ticker or "").strip().upper()
    if ticker in SEC_VALIDATED:
        return "sec_validated"
    if not SYMBOL_RE.match(ticker):
        return None
    try:
        stored = market_storage.get_company(ticker)
    except Exception:
        return None
    return "provider" if stored else None


def is_research_ticker(ticker):
    return research_profile(ticker) is not None


def market_of(ticker):
    if str(ticker or "").upper() in SEC_VALIDATED:
        return "US"
    stored = market_storage.get_company(str(ticker).upper())
    return (stored or {}).get("market") or ("IN" if str(ticker).upper().endswith((".NS", ".BO")) else "US")


def currency_of(ticker):
    if str(ticker or "").upper() in SEC_VALIDATED:
        return "USD"
    stored = market_storage.get_company(str(ticker).upper()) or {}
    return stored.get("currency") or market_config(market_of(ticker))["currency"]


def evidence_safe(ticker):
    """Ticker form usable inside evidence identifiers (letters, digits, _)."""
    return re.sub(r"[^A-Za-z0-9]", "_", str(ticker or "").upper())


def company_display_names(ticker):
    """Names used for headline attribution of provider companies."""
    ticker = str(ticker or "").upper()
    stored = market_storage.get_company(ticker) or {}
    entry = universe_entry(stored.get("market") or market_of(ticker), ticker) or {}
    terms = news_terms_for({**stored, **entry, "symbol": ticker})
    base = ticker.split(".")[0]
    names = [t for t in terms if t]
    if base and base not in names and len(base) >= 3:
        names.append(base)
    return tuple(names)


def news_terms_for(company):
    terms = [t for t in (company.get("news_terms") or []) if t]
    if terms:
        return terms[:3]
    name = LEGAL_SUFFIXES.sub("", str(company.get("name") or "")).replace(",", " ")
    name = re.sub(r"^\s*the\s+", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+", " ", name).strip(" .&")
    return [name] if name else [company["symbol"]]
