"""
Verified company identity used to decide whether text is about a company.

An identity combines, for one ticker:

* the curated universe entry (core/market_universe.json): display name,
  sector, and verified aliases ('aliases' for US, 'news_terms' for India);
* the stored registry rows: the SEC registry name and CIK (``companies``) and
  the provider profile name, exchange and currency (``market_companies``);
* language-level ambiguity flags computed from core/name_ambiguity.json.

Nothing here invents a name: every name form comes from one of those sources
and records where it came from. Matching rules live in news_relevance.py.
"""

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

AMBIGUITY_PATH = Path(__file__).resolve().parents[1] / "core" / "name_ambiguity.json"

_SUFFIX_RE = re.compile(
    r"[,\s]+(inc\.?|incorporated|corp\.?|corporation|co\.?|company|companies|ltd\.?|limited|plc|"
    r"n\.v\.|s\.a\.|llc|lp|holdings?)\s*$",
    re.IGNORECASE,
)
_PAREN_RE = re.compile(r"\s*\(([^)]*)\)")


@lru_cache(maxsize=1)
def ambiguity_data():
    data = json.loads(AMBIGUITY_PATH.read_text(encoding="utf-8"))
    return {
        "common_words": frozenset(w.lower() for w in data["common_words"]),
        "common_surnames": frozenset(w.lower() for w in data["common_surnames"]),
        "common_word_tickers": frozenset(w.upper() for w in data["common_word_tickers"]),
        "collocations": tuple(sorted((c.lower() for c in data["collocations"]), key=len, reverse=True)),
        "corporate_suffixes": frozenset(w.lower() for w in data["corporate_suffixes"]),
        "corporate_cues_after": frozenset(w.lower() for w in data["corporate_cues_after"]),
        "corporate_cues_before": tuple(sorted((w.lower() for w in data["corporate_cues_before"]), key=len, reverse=True)),
        "person_titles": tuple(sorted((w.lower() for w in data["person_titles"]), key=len, reverse=True)),
        "place_and_venue_words": frozenset(w.lower() for w in data["place_and_venue_words"]),
    }


@dataclass(frozen=True)
class NameForm:
    text: str
    source: str          # legal_name | display_name | alias | former_name | short_name
    ambiguous: bool      # needs corporate context to count
    acronym: bool = False


@dataclass(frozen=True)
class CompanyIdentity:
    ticker: str
    market: str
    display_name: str
    legal_name: str
    names: tuple = ()
    ticker_base: str = ""
    ticker_ambiguous: bool = False
    exchange: str | None = None
    country: str = ""
    currency: str = ""
    cik: str | None = None
    sector_key: str | None = None
    verification: str = "registry"
    sources: tuple = field(default_factory=tuple)

    def public(self):
        return {
            "ticker": self.ticker,
            "market": self.market,
            "display_name": self.display_name,
            "legal_name": self.legal_name,
            "names": [n.text for n in self.names],
            "ambiguous_names": [n.text for n in self.names if n.ambiguous],
            "ticker_ambiguous": self.ticker_ambiguous,
            "exchange": self.exchange,
            "country": self.country,
            "currency": self.currency,
            "cik": self.cik,
            "sector_key": self.sector_key,
            "verification": self.verification,
            "sources": list(self.sources),
        }


def strip_legal_suffix(name):
    text = _PAREN_RE.sub("", str(name or "")).strip()
    text = re.sub(r"^\s*the\s+", "", text, flags=re.IGNORECASE)
    previous = None
    while previous != text:
        previous = text
        text = _SUFFIX_RE.sub("", text).strip(" ,.&")
        text = re.sub(r"\s+(and|&)$", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def _tidy_registry_name(name):
    """SEC registry names are upper case ('ORACLE CORP'); keep a readable form."""
    text = re.sub(r"\s+", " ", str(name or "")).strip()
    if text and text.isupper() and len(text) > 4:
        text = " ".join(w if len(w) <= 3 and w.isalpha() and w not in ("INC", "CORP", "LTD", "CO") else w.title()
                        for w in text.split(" "))
    return text


def is_ambiguous_word(word):
    data = ambiguity_data()
    w = str(word or "").strip().lower().replace("’", "'")
    w = re.sub(r"'s$", "", w)
    return w in data["common_words"] or w in data["common_surnames"]


def _name_form(text, source, data):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    words = re.findall(r"[A-Za-z0-9&'.\-]+", text)
    acronym = len(words) == 1 and text.replace("&", "").replace(".", "").isupper() and len(text) <= 5
    if acronym:
        ambiguous = text.upper() in data["common_word_tickers"] or len(text.replace("&", "")) <= 2
    elif len(words) == 1:
        ambiguous = is_ambiguous_word(text)
    else:
        # Multi-word names are distinctive unless every word is an ordinary
        # word and none is a corporate form ("Apple Inc." and "General Motors"
        # are distinctive; a phrase of only common words is not).
        has_corporate_word = any(w.lower().rstrip(".,") in data["corporate_suffixes"] for w in words)
        ambiguous = not has_corporate_word and all(is_ambiguous_word(w) for w in words)
    return NameForm(text=text, source=source, ambiguous=ambiguous, acronym=acronym)


def build_identity(ticker, market, name, *, aliases=(), legal_name=None, former_names=(),
                   cik=None, exchange=None, currency=None, sector_key=None,
                   verification="registry", sources=()):
    """Pure constructor (no database); also used by tests with real company data."""
    data = ambiguity_data()
    ticker = str(ticker).upper()
    display = re.sub(r"\s+", " ", str(name or ticker)).strip()
    legal = _tidy_registry_name(legal_name) if legal_name else display
    forms = []
    seen = set()

    def add(text, source):
        text = re.sub(r"\s+", " ", str(text or "")).strip(" ,")
        if len(text) < 2 or text.lower() in seen:
            return
        seen.add(text.lower())
        forms.append(_name_form(text, source, data))

    for alias in aliases or ():
        add(alias, "alias")
    add(strip_legal_suffix(display), "short_name")
    add(display, "display_name")
    add(legal, "legal_name")
    add(strip_legal_suffix(legal), "short_name")
    # Names in parentheses in the display name are brand names (e.g. "(Nykaa)").
    for inner in _PAREN_RE.findall(display):
        add(inner, "display_name")
    for former in former_names or ():
        add(strip_legal_suffix(_tidy_registry_name(former)), "former_name")

    base = ticker.split(".")[0]
    ticker_ambiguous = base in data["common_word_tickers"] or len(base.replace("&", "")) <= 2
    country = {"US": "United States", "IN": "India"}.get(market, "")
    return CompanyIdentity(
        ticker=ticker, market=market, display_name=display, legal_name=legal,
        names=tuple(forms), ticker_base=base, ticker_ambiguous=ticker_ambiguous,
        exchange=exchange, country=country, currency=currency or "", cik=cik or None,
        sector_key=sector_key, verification=verification, sources=tuple(sources),
    )


def identity_for(ticker):
    """Identity of a registered or universe company (read-only database access)."""
    from app.services import market_registry as registry
    from app.services import market_storage

    ticker = registry.require_symbol(ticker)
    sources = []
    stored = {}
    try:
        stored = market_storage.get_company(ticker) or {}
    except Exception:
        stored = {}
    market = stored.get("market") or registry.market_of(ticker)
    entry = registry.universe_entry(market, ticker) or {}
    if entry:
        sources.append("universe")
    if stored:
        sources.append("market_companies")

    sec_row = _sec_company_row(ticker)
    if sec_row:
        sources.append("sec_registry")

    name = entry.get("name") or stored.get("name") or (sec_row or {}).get("name")
    if not name:
        raise registry.RegistryError("Unknown company.")
    aliases = list(entry.get("aliases") or entry.get("news_terms") or [])
    legal = (sec_row or {}).get("name") or stored.get("name") or name
    cik = (sec_row or {}).get("cik") or stored.get("cik")
    verification = "sec_registry" if sec_row and (sec_row.get("cik") or "").strip() else (
        "provider_profile" if stored else "universe_only")
    return build_identity(
        ticker, market, name, aliases=aliases, legal_name=legal,
        cik=cik, exchange=stored.get("exchange"),
        currency=stored.get("currency") or registry.market_config(market)["currency"],
        sector_key=entry.get("sector_key") or stored.get("sector_key"),
        verification=verification, sources=sources,
    )


def _sec_company_row(ticker):
    try:
        from app.database.sqlite_manager import connect

        with connect() as conn:
            row = conn.execute(
                "SELECT ticker, name, cik FROM companies WHERE ticker = ?", (ticker,)
            ).fetchone()
        if row and (row["cik"] or "").strip():
            return dict(row)
    except Exception:
        return None
    return None


def universe_identities(market=None):
    """Identities built from the curated universe only (no database), cached."""
    return _universe_identities(market)


@lru_cache(maxsize=4)
def _universe_identities(market):
    from app.services import market_registry as registry

    out = []
    for m in ([market] if market else list(registry.MARKETS)):
        cfg = registry.market_config(m)
        for sector_key, sector in cfg["sectors"].items():
            for c in sector["companies"]:
                out.append(build_identity(
                    c["symbol"], m, c["name"], aliases=c.get("aliases") or c.get("news_terms") or (),
                    currency=cfg["currency"], sector_key=sector_key, verification="universe_only",
                    sources=("universe",),
                ))
    return tuple(out)
