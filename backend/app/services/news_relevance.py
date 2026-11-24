"""
Strict, deterministic company-news relevance (no LLM, no network).

An article is accepted for a company only when that company is the subject
of the headline, or a directly involved party in a material business event
named in the headline. Simply mentioning the company is not enough.

Name matching (see company_identity.py):
* distinctive names (e.g. "Microsoft", "Tata Consultancy Services") match
  case-insensitively as whole words;
* ambiguous names (ordinary words or surnames such as "Apple", "Oracle",
  "Target", "Ford") match only when capitalised, outside known non-company
  collocations ("Oracle Park", "price target", "Gov. Greg Abbott"), and with
  corporate context next to them ("Oracle shares", "Apple unveils",
  "Shares of Target", "Abbott's ...") or a distinctive name of the same
  company elsewhere in the text;
* tickers count in explicit formats ($ORCL, NYSE: ORCL, (ORCL)); a plain
  upper-case ticker counts only when it is not an ordinary word.

Reject rules: company not named in the headline (description-only
mentions), another company is the headline subject, watchlists/listicles,
routine fund-holding changes, generic market wraps, articles outside the
coverage window or without a publication date, and duplicate articles or
duplicate events. The decision, score, reasons, event type and rule
version are returned so every acceptance and rejection is explainable.
"""

import re as _re
from datetime import datetime, timedelta, timezone
from functools import lru_cache


@lru_cache(maxsize=32768)
def _compiled(pattern, flags=0):
    return _re.compile(pattern, flags)


class _CachedRe:
    """``re`` with a large compiled-pattern cache. Name patterns are built per
    company, so a universe-wide check needs thousands of distinct patterns,
    far more than ``re``'s own 512-entry cache (which then recompiles on every
    call and made a 44-company page take a minute)."""

    def __getattr__(self, name):
        return getattr(_re, name)

    def search(self, pattern, string, flags=0):
        return _compiled(pattern, flags).search(string)

    def match(self, pattern, string, flags=0):
        return _compiled(pattern, flags).match(string)

    def fullmatch(self, pattern, string, flags=0):
        return _compiled(pattern, flags).fullmatch(string)

    def finditer(self, pattern, string, flags=0):
        return _compiled(pattern, flags).finditer(string)

    def findall(self, pattern, string, flags=0):
        return _compiled(pattern, flags).findall(string)

    def sub(self, pattern, repl, string, count=0, flags=0):
        return _compiled(pattern, flags).sub(repl, string, count)

    def split(self, pattern, string, maxsplit=0, flags=0):
        return _compiled(pattern, flags).split(string, maxsplit)


re = _CachedRe()
from urllib.parse import urlsplit, urlunsplit

from app.services.company_identity import ambiguity_data

RELEVANCE_VERSION = "2026-11-24.3"
NO_NEWS_MESSAGE = "No verified company-specific news is available for the selected period."

EVENT_PATTERNS = (
    ("earnings", r"\b(earnings|results|quarter(ly)?|q[1-4]|fy\d{2,4}|revenue|revenues|profit|profits|net income|"
                 r"eps|beats|misses|tops estimates|guidance|(raises|lifts|cuts|lowers) (its )?outlook|sales)\b"),
    ("mergers_acquisitions", r"\b(acquir\w*|acquisition|merger|merges?|takeover|buyout|divest\w*|to buy|"
                             r"buys?|stake sale|spin[- ]?off|demerger)\b"),
    ("partnership", r"\b(partner\w*|collaborat\w*|alliance|joint venture|tie[- ]?up|ties up|teams up|"
                    r"agreement with|pact)\b"),
    ("financing", r"\b(bonds?|notes offering|debt|loan|funding|raises \$|raises rs|ipo|listing|buyback|"
                  r"repurchase|dividend|rights issue|qip|credit rating|rating downgrade|rating upgrade|"
                  r"capital raise|fund ?raise)\b"),
    ("product", r"\b(launch\w*|unveil\w*|introduc\w*|rolls? out|release[sd]?|debuts?|new model|"
                r"announces? new)\b"),
    ("regulatory", r"\b(fda|sec|sebi|rbi|cci|ftc|doj|antitrust|regulator\w*|approv\w*|probe|investigation|"
                   r"fine[sd]?|penalt\w*|recall\w*(?!\s+(how|that|when|his|her|their|the day))|licen[cs]e|ban|"
                   r"sanction\w*|tariff\w*)\b"),
    ("legal", r"\b(lawsuit|sues?|sued|court|settle\w*|verdict|litigation|class action|patent)\b"),
    ("leadership", r"\b(ceo|cfo|chief executive|chairman|appoint\w*|resign\w*|steps down|succession|"
                   r"names .{1,40} as)\b"),
    ("operations", r"\b(layoffs?|job cuts|cuts jobs|plant|factory|expansion|capex|data cent(er|re)s?|"
                   r"outage|strike|orders? (worth|for|from)|order book|wins|bags|secures|contracts?|"
                   r"deliver(y|ies|s|ed)|production|capacity)\b"),
    ("analyst_rating", r"\b(upgrade[sd]?|downgrade[sd]?|price target|rating|overweight|underweight|"
                       r"outperform|underperform)\b"),
)
INVOLVEMENT_EVENTS = {"mergers_acquisitions", "partnership", "legal", "regulatory", "financing", "operations"}

WATCHLIST_PATTERNS = (
    r"\b(stocks?|shares) to (buy|watch|sell|avoid|track|consider)\b",
    r"\btop \d+ (stocks|shares|picks)\b",
    r"\b\d+ (stocks|shares) (to|that|for|under|with)\b",
    r"\bstocks? in (focus|news)\b",
    r"\b(buzzing|trending|hot|top) stocks\b",
    r"\bmultibagger\b",
    r"\bpenny stocks?\b",
    r"\bstock (picks|recommendations|ideas)\b",
    r"\bstocks? under (rs|₹|\$)",
    r"\b(trade|trading) setup\b",
    r"\bmarket live\b|\blive updates\b|\bstock market today\b",
    r"\bamong \d+ (stocks|companies|shares)\b",
    r"^from .{1,60}\bto\b.{1,60}:",
    r"\btop holding\b",
    r"\b(billionaire|hedge fund|fund manager|investor)\b.{0,60}\b(exited|exits|sold|sells|dumped|bought|buys|trims|adds)\b",
    r"\bstocks (trimmed|added|bought|sold|dumped) by\b",
    r"\bwhich (goes|will go|is going) higher\b|\bprice prediction\b|\bpredicts? .{0,30}\bvs\.? ",
    r"\bmarket\b.{0,40}\b(competitive analysis|size|share report|research report|key players|outlook \d{4})\b",
    r"\b(identifies|profiles) (leading|key|top) (technology )?(companies|players|vendors)\b",
)
FUND_HOLDING_PATTERNS = (
    r"\b(acquires|buys|sells|trims|raises|lowers|boosts|cuts|reduces|increases|grows|adds to)\b"
    r".{0,40}\b(stake|position|holdings?)\b",
    r"\b(stake|position|holdings?) in\b.{0,80}\b(llc|lp|l\.p\.|advisors?|capital|management|partners|"
    r"wealth|investments?|fund|trust|bank)\b",
    r"\b(shares|stock|stake) (sold|bought|purchased|acquired|unloaded|dumped|offloaded|added) by\b",
    r"\b(unloaded|dumped|offloaded) by (rep|sen|senator|representative)\b",
    r"\b(llc|advisors|capital management|wealth management|asset management)\b.{0,60}\b(stake|shares|position)\b",
)
MARKET_WRAP_PATTERN = (
    r"\b(sensex|nifty|dow jones|dow|nasdaq composite|s&p 500|wall street|stock market|markets? (close|open|wrap)|"
    r"closing bell|opening bell)\b"
)
PERSONAL_STORY_PATTERN = (
    r"\b(ex-partner|ex partner|former partner|ex-wife|ex-husband|girlfriend|boyfriend|banned from contacting|"
    r"restraining order|domestic abuse|stalking|fitness routine|morning routine|workout|diet|net worth|"
    r"mansion|wedding|divorce|affair claims|dating|delivery (man|boy|partner|agent|executive|worker)s?|"
    r"(clears|cracks) .{0,30}exam|(he|she) worked at|quit (his|her) job|started a business|recalls how|"
    r"shares (his|her) (experience|journey|story)|inspiring|success story)\b"
)
# A headline that opens with another (possibly unlisted) company as subject:
# "Anthropic stock slides ...", "Zavya's ...".
LEADING_SUBJECT_PATTERN = r"^([A-Z][\w&.\-]*(?:\s+[A-Z][\w&.\-]*){0,3})(?:'s|’s|\s+(?:stock|shares|inc|corp|ltd)\b)"
COMPARATIVE_PATTERN = r"\b(vs\.?|versus|rival\w*|than|like|such as|including|compared with|outpaces?|overtakes?)\b"
COMPARISON_ARTICLE_PATTERN = (
    r"\b(vs\.?|versus|critical contrast|contrast|comparison|head[- ]to[- ]head|side[- ]by[- ]side|"
    r"which is (the )?better|analy[sz]ing .{2,80} (&|and) )"
)
EXCHANGE_TICKER_PATTERN = r"\(\s*(?:NYSE|NASDAQ|Nasdaq|NYSEARCA|NYSEAMERICAN|NSE|BSE|OTC|TSX|LSE)\s*:\s*[A-Z0-9.\-]+\s*\)"
SUBORDINATE_OPENING = r"^\s*(\w+\s+){0,3}(after|before|as|while|despite|amid|since|when)\b"

_WORD = r"[A-Za-z0-9&]"


# ----------------------------------------------------------------- matching

def _token_after(text, end):
    m = re.match(r"\s*('s|’s)?\s*([A-Za-z0-9&.\-]+)?", text[end:])
    possessive = bool(m and m.group(1))
    token = (m.group(2) or "").strip(".,:;!?").lower() if m else ""
    return possessive, token


def _text_before(text, start, chars=30):
    return text[max(0, start - chars):start].lower()


def _word_before(text, start):
    m = re.search(r"([A-Za-z.&'\-]+)\s*$", text[:start])
    return m.group(1) if m else ""


def _in_collocation(text, start, end, data):
    lowered = text.lower()
    window = lowered[max(0, start - 25):min(len(lowered), end + 25)]
    offset = max(0, start - 25)
    for phrase in data["collocations"]:
        for m in re.finditer(re.escape(phrase), window):
            a, b = m.start() + offset, m.end() + offset
            if a <= start < b or a < end <= b or (start <= a and b <= end):
                return True
    return False


def _ticker_mentions(text, identity):
    base = re.escape(identity.ticker_base)
    exchange = r"(?:NYSE|NASDAQ|Nasdaq|NYSE\s?American|NYSEArca|NSE|BSE|OTC)"
    patterns = [
        rf"\${base}(?!{_WORD})",
        rf"{exchange}\s*:\s*{base}(?!{_WORD})",
        rf"\(\s*{base}\s*\)",
        rf"\(\s*{exchange}\s*:\s*{base}\s*\)",
    ]
    if not identity.ticker_ambiguous and len(identity.ticker_base) >= 3:
        patterns.append(rf"(?<!{_WORD}){base}(?!{_WORD})")
    out = []
    for pattern in patterns:
        for m in re.finditer(pattern, text):  # case-sensitive
            out.append((m.start(), m.end(), identity.ticker_base, "ticker"))
    return out


def _has_corporate_context(text, start, end, data):
    possessive, after = _token_after(text, end)
    if possessive:
        return True
    if after and (after in data["corporate_suffixes"] or after in data["corporate_cues_after"]):
        return True
    # "Apple Maps launches ...", "Amazon, BigBasket face ...": look past up to two
    # capitalised product or company words (or a comma) for a business verb.
    rest = re.findall(r"[A-Za-z0-9&.'’\-]+|,", text[end:end + 60])
    for index, token in enumerate(rest[:3]):
        if token == "," or (token[:1].isupper() and index < 2):
            continue
        if token.strip(".,:;!?").lower() in data["corporate_cues_after"]:
            return True
        break
    # Headline grammar: "Meta rethinks ...", "Lowe's beats ..." - a name that
    # opens the text followed by a lower-case third-person verb is its subject.
    if start == 0 and re.match(r"\s+[a-z]{3,}s\b", text[end:end + 20]):
        return True
    before = _text_before(text, start).rstrip()
    return any(re.search(rf"(^|[^a-z0-9]){re.escape(cue)}$", before) for cue in data["corporate_cues_before"])


def _non_company_sense(text, start, end, form, data):
    if _in_collocation(text, start, end, data):
        return "collocation"
    if re.match(r"-[A-Za-z]", text[end:end + 2]):
        return "hyphenated_word"
    _, after = _token_after(text, end)
    if after in data["place_and_venue_words"]:
        return "place_or_venue"
    if re.match(r"\s+of\s+[A-Z]", text[end:end + 6]):
        return "epithet"
    before = _text_before(text, start)
    if any(re.search(rf"(^|\W){re.escape(title)}\s*$", before) for title in data["person_titles"]):
        return "person_title"
    word = form.text.lower()
    if word in data["common_surnames"]:
        prev = _word_before(text, start)
        if (prev and prev[:1].isupper() and prev.lower() not in data["corporate_cues_before"]
                and prev.lower().rstrip(".") not in {"the", "a", "an"} and not prev.isupper()):
            return "person_name"
    return None


def find_mentions(text, identity, *, strong_only=False):
    """
    Company mentions in ``text`` as dicts {start, end, name, kind, strength}.
    strength: 'strong' (distinctive name, legal name or ticker format) or
    'contextual' (ambiguous name with corporate context).
    """
    data = ambiguity_data()
    text = str(text or "")
    found = []
    strong_present = False
    pending = []

    for start, end, name, kind in _ticker_mentions(text, identity):
        found.append({"start": start, "end": end, "name": name, "kind": kind, "strength": "strong"})
        strong_present = True

    for form in identity.names:
        escaped = re.escape(form.text).replace(r"\ ", r"\s+")
        if not form.ambiguous:
            flags = 0 if form.acronym else re.IGNORECASE
            for m in re.finditer(rf"(?<!{_WORD}){escaped}(?!{_WORD})", text, flags):
                if _in_collocation(text, m.start(), m.end(), data):
                    continue
                found.append({"start": m.start(), "end": m.end(), "name": form.text,
                              "kind": form.source, "strength": "strong"})
                strong_present = True
        elif not strong_only:
            variants = {form.text, form.text.upper()} if not form.acronym else {form.text}
            for variant in variants:
                if not variant[:1].isupper():
                    continue
                for m in re.finditer(rf"(?<!{_WORD}){re.escape(variant)}(?!{_WORD}|-[A-Za-z])", text):
                    pending.append((m.start(), m.end(), form))

    for start, end, form in pending:
        if _non_company_sense(text, start, end, form, data):
            continue
        if strong_present or _has_corporate_context(text, start, end, data):
            found.append({"start": start, "end": end, "name": form.text,
                          "kind": form.source, "strength": "contextual"})

    found.sort(key=lambda m: (m["start"], -(m["end"] - m["start"])))
    deduped, last_end = [], -1
    for m in found:
        if m["start"] >= last_end:
            deduped.append(m)
            last_end = m["end"]
    return deduped


def mentions_company(text, identity):
    return bool(find_mentions(text, identity))


# ------------------------------------------------------------- classification

def event_type(text):
    lowered = str(text or "").lower()
    for name, pattern in EVENT_PATTERNS:
        if re.search(pattern, lowered):
            return name
    return None


def _first_sentence(text):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    m = re.match(r"(.{20,400}?[.!?])(\s|$)", text)
    return m.group(1) if m else text[:400]


def _other_companies_in(text, identity, others):
    hits = []
    for other in others or ():
        if other.ticker == identity.ticker:
            continue
        mentions = find_mentions(text, other)
        if mentions:
            hits.append((mentions[0]["start"], other.ticker))
    return sorted(hits)


def parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def assess_article(article, identity, *, others=None, now=None, max_age_days=45):
    """Accept/reject one article for ``identity`` with explainable reasons."""
    now = now or datetime.now(timezone.utc)
    title = re.sub(r"\s+", " ", str(article.get("title") or "")).strip()
    description = str(article.get("description") or "")
    reasons = []

    def result(decision, score, reason, primary=False, event=None, matched=()):
        return {
            "decision": decision,
            "score": round(score, 2),
            "reason": reason,
            "reasons": reasons + [reason],
            "primary_subject": primary,
            "event_type": event,
            "matched_names": sorted(set(matched)),
            "relevance_version": RELEVANCE_VERSION,
        }

    if not title:
        return result("reject", 0.0, "missing_headline")

    published = parse_time(article.get("published_at"))
    if published is None:
        return result("reject", 0.0, "missing_publication_date")
    if published > now + timedelta(hours=6):
        return result("reject", 0.0, "publication_date_in_future")
    if max_age_days is not None and published < now - timedelta(days=max_age_days):
        return result("reject", 0.0, "outside_coverage_period")

    lowered_title = title.lower()
    title_mentions = find_mentions(title, identity)
    first_sentence = _first_sentence(description)
    headline_event = event_type(title)

    if any(re.search(p, lowered_title) for p in WATCHLIST_PATTERNS):
        return result("reject", 0.05, "watchlist_or_listicle")
    if re.search(PERSONAL_STORY_PATTERN, lowered_title):
        return result("reject", 0.05, "personal_or_lifestyle_story")
    if any(re.search(p, lowered_title) for p in FUND_HOLDING_PATTERNS):
        return result("reject", 0.05, "routine_fund_holding_change")

    others_in_title = _other_companies_in(title, identity, others)
    if len({t for _, t in others_in_title}) >= 2 and title_mentions:
        return result("reject", 0.1, "multi_company_roundup",
                      matched=[m["name"] for m in title_mentions])

    listed_tickers = len(re.findall(EXCHANGE_TICKER_PATTERN, title))
    if (title_mentions and (listed_tickers >= 2 or others_in_title)
            and re.search(COMPARISON_ARTICLE_PATTERN, lowered_title)
            and headline_event not in INVOLVEMENT_EVENTS):
        return result("reject", 0.1, "comparison_article", matched=[m["name"] for m in title_mentions])

    # "Days after ..., Meta's AI chief ...": the subject is in the main clause.
    main_clause_offset = 0
    if title_mentions and re.search(SUBORDINATE_OPENING, lowered_title) and "," in title:
        main_offset = title.rfind(",") + 1
        main = title[main_offset:]
        ours_main = [m for m in title_mentions if m["start"] >= main_offset]
        others_main = _other_companies_in(main, identity, others)
        if others_main and (not ours_main or others_main[0][0] + main_offset < ours_main[0]["start"]):
            return result("reject", 0.2, "another_company_is_subject",
                          matched=[m["name"] for m in title_mentions])
        if ours_main:
            # Our company leads the main clause: companies named only in the
            # subordinate opening are not the headline's subject.
            main_clause_offset = main_offset

    if not title_mentions:
        # Strict rule: a company named only in the description (however early)
        # is not the article's subject.
        if find_mentions(description, identity) or find_mentions(first_sentence, identity):
            return result("reject", 0.1, "company_not_in_headline")
        return result("reject", 0.0, "company_not_mentioned")

    if main_clause_offset:
        ours_first = next(m["start"] for m in title_mentions if m["start"] >= main_clause_offset)
        first_other = next((pos for pos, _ in others_in_title if pos >= main_clause_offset), None)
    else:
        ours_first = title_mentions[0]["start"]
        first_other = others_in_title[0][0] if others_in_title else None
    leading = re.match(LEADING_SUBJECT_PATTERN, title)
    # "Mark Zuckerberg's Meta ..." is a person's company, not another subject.
    if leading and not main_clause_offset and ours_first > leading.end() + 2 and first_other is None:
        # An unlisted company opens the headline as its subject.
        first_other = 0
    matched = [m["name"] for m in title_mentions]

    if re.search(MARKET_WRAP_PATTERN, lowered_title) and not headline_event:
        return result("reject", 0.15, "generic_market_commentary", matched=matched)

    if first_other is not None and first_other < ours_first:
        between = title[first_other:ours_first].lower()
        comparative = re.search(COMPARATIVE_PATTERN, between) or re.search(COMPARATIVE_PATTERN, lowered_title)
        if headline_event in INVOLVEMENT_EVENTS and not comparative:
            reasons.append("directly_involved_party")
            return result("accept", 0.8, "company_party_to_material_event", primary=False,
                          event=headline_event, matched=matched)
        return result("reject", 0.2, "another_company_is_subject", matched=matched)

    if headline_event:
        return result("accept", 0.95, "company_is_headline_subject", primary=True,
                      event=headline_event, matched=matched)
    return result("accept", 0.8, "company_is_headline_subject", primary=True,
                  event="company_news", matched=matched)


# --------------------------------------------------------------- duplicates

def canonical_url(value):
    try:
        parts = urlsplit(str(value or "").strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    host = host[4:] if host.startswith("www.") else host
    if host.startswith("m.") and host.count(".") >= 2:
        host = host[2:]
    path = re.sub(r"/+$", "", parts.path or "") or "/"
    return urlunsplit(("https", host, path, "", ""))


_STOP = frozenset("""a an the and or of to in on for with at by from as is are was were be its it this that
after before over under amid new says said will may could would""".split())


def title_tokens(title):
    words = re.findall(r"[a-z0-9]+", str(title or "").lower())
    return frozenset(w for w in words if len(w) >= 3 and w not in _STOP)


def same_event(a_title, a_time, b_title, b_time, threshold=0.8, hours=72, identical_days=14):
    ta, tb = title_tokens(a_title), title_tokens(b_title)
    if not ta or not tb:
        return False
    overlap = len(ta & tb) / len(ta | tb)
    if overlap < threshold:
        return False
    window = identical_days * 24 if ta == tb else hours
    if a_time and b_time and abs((a_time - b_time).total_seconds()) > window * 3600:
        return False
    return True


# ------------------------------------------------------------ apply to rows

def _url_of(row):
    return row.get("article_url") or row.get("source_url") or row.get("url")


def filter_company_news(rows, identity, *, others=None, now=None, max_age_days=None):
    """
    Apply the relevance rules plus article/event de-duplication to stored rows.

    Returns (accepted, rejected). Accepted rows are copies annotated with
    relevance fields, newest first; the earliest copy of a duplicated
    article/event is kept. Rejected entries carry the row and the reason.
    Nothing is written or deleted.
    """
    from app.core.config import get_settings
    from app.services.company_identity import universe_identities

    if max_age_days is None:
        try:
            max_age_days = get_settings().news_max_age_days
        except Exception:
            max_age_days = 45
    if others is None:
        others = universe_identities()
    now = now or datetime.now(timezone.utc)

    candidates, rejected = [], []
    for row in rows or []:
        verdict = assess_article(row, identity, others=others, now=now, max_age_days=max_age_days)
        if verdict["decision"] == "accept":
            candidates.append({**row, "relevance": verdict})
        else:
            rejected.append({"row": row, "reason": verdict["reason"], "relevance": verdict})

    candidates.sort(key=lambda r: (parse_time(r.get("published_at")) or now, r.get("id") or r.get("database_id") or 0))
    kept, seen_urls = [], set()
    for row in candidates:
        url = canonical_url(_url_of(row))
        published = parse_time(row.get("published_at"))
        if url and url in seen_urls:
            rejected.append({"row": row, "reason": "duplicate_article", "relevance": row["relevance"]})
            continue
        duplicate_of = next((k for k in kept if same_event(
            k.get("title"), parse_time(k.get("published_at")), row.get("title"), published)), None)
        if duplicate_of is not None:
            rejected.append({"row": row, "reason": "duplicate_event", "relevance": row["relevance"]})
            continue
        if url:
            seen_urls.add(url)
        kept.append(row)
    kept.sort(key=lambda r: parse_time(r.get("published_at")) or now, reverse=True)
    return kept, rejected


def load_company_news(conn, ticker, *, limit=30, candidates=300, now=None):
    """
    Read a company's stored news and keep only articles that pass the current
    relevance rules (hidden rows stay in the database untouched).
    Returns (accepted_rows, summary).
    """
    from collections import Counter

    from app.services.company_identity import identity_for

    rows = conn.execute(
        """
        SELECT id, company_ticker, provider, provider_article_id, title, description,
               article_url, source_name, published_at, retrieved_at,
               relevance_score AS stored_relevance_score,
               relevance_reason AS stored_relevance_reason
        FROM news_articles
        WHERE company_ticker = ?
        ORDER BY published_at DESC, id DESC
        LIMIT ?
        """,
        (ticker, candidates),
    ).fetchall()
    rows = [dict(r) for r in rows]
    if not rows:
        return [], {"stored": 0, "accepted": 0, "hidden": 0, "hidden_by_reason": {},
                    "relevance_version": RELEVANCE_VERSION, "message": NO_NEWS_MESSAGE}
    kept, rejected = filter_company_news(rows, identity_for(ticker), now=now)
    for row in kept:
        if row.get("company_ticker") != ticker:  # defensive: never mix companies
            raise ValueError("News row belongs to a different company.")
    summary = {
        "stored": len(rows),
        "accepted": len(kept),
        "hidden": len(rejected),
        "hidden_by_reason": dict(Counter(r["reason"] for r in rejected)),
        "relevance_version": RELEVANCE_VERSION,
        "message": None if kept else NO_NEWS_MESSAGE,
    }
    return kept[:limit], summary
