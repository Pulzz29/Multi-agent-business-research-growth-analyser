"""
Conservative, claim-level checks for AI-written research prose.

These checks are deterministic and deliberately strict. They establish that
a piece of generated text:

* cites only evidence identifiers that exist in the evidence package;
* uses only numbers derivable from the evidence it cites (values, unit
  conversions, Python-computed comparisons, forecast outputs, dates);
* uses percentages only when they equal a Python-computed comparison that
  the same finding cites;
* mentions only reporting periods / fiscal labels present in the cited
  evidence;
* does not describe a trend without enough comparable periods;
* contains no causal, certain-prediction or advisory language, no URLs and
  no echoed prompt-injection text.

Passing these checks does NOT prove that an interpretation is correct. The
output is therefore labelled "automated checks passed", never "verified".
"""

import re
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation


VALIDATION_LABEL = (
    "AI interpretation — automated evidence, number, period and language "
    "checks passed; not independently verified."
)

# Shared with executive_summary (kept identical so behaviour is consistent).
BANNED_PATTERNS = [
    r"\bcaused\b", r"\bcausing\b", r"\bbecause of\b", r"\bdue to\b",
    r"\bled to\b", r"\bresulted in\b", r"\bdrove\b", r"\bdriven by\b",
    r"\bwill (?:increase|decrease|grow|decline|rise|fall|improve|deteriorate|reach|exceed)\b",
    r"\bguarantee", r"\bcertain(?:ly)? to\b",
    r"\b(?:we )?recommend(?:s|ed)? (?:buying|selling|holding|investing)\b",
    r"\b(?:buy|sell|hold) rating\b",
]

# Additional patterns for agent prose.
EXTRA_BANNED_PATTERNS = [
    r"\bas a result of\b", r"\bthanks to\b", r"\battributable to\b",
    r"\bfu?ell?ed by\b", r"\bboosted by\b", r"\bdragged down by\b",
    r"\bconfirmed (?:risk|opportunit\w*|threat)\b",
    r"\bproves?\b", r"\bproven\b",
    r"\bwill (?:likely |certainly |definitely )?(?:boost|hurt|lift|lower|raise|cut|double|halve|outperform|underperform|beat|miss)\b",
    r"\binvestors should\b", r"\byou should (?:buy|sell|invest)\b",
    r"\b(?:strong )?(?:buy|sell)\b(?= (?:the )?(?:stock|shares))",
    r"\bprice target\b",
]

LIMITATION_SAFE_PATTERNS = {r"\bdue to\b", r"\bbecause of\b"}

INJECTION_PATTERNS = [
    r"ignore (?:all |any |the )?(?:previous|prior|above) instructions",
    r"disregard (?:all |any |the )?(?:previous|prior|above)",
    r"\bsystem prompt\b",
    r"\byou are now\b",
]

TREND_PATTERN = re.compile(
    r"\b(?:sustained|consistent(?:ly)?|trend(?:s|ing)?|accelerat\w*|"
    r"decelerat\w*|steadily|momentum|streak|persistent(?:ly)?)\b",
    re.IGNORECASE,
)

# Links only: company names such as "Amazon.com" are not URLs.
URL_PATTERN = re.compile(
    r"https?://|\bwww\.|\b[a-z0-9-]+\.(?:com|net|org|io|co|news|ai)/\S",
    re.IGNORECASE,
)

EVIDENCE_TOKEN = re.compile(r"\b(?:SEC|CMP|FORECAST|NEWS|FMP|YF)_[A-Za-z0-9_]+")
ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
NUMBER_RE = re.compile(r"(?<![A-Za-z_\d.])\d[\d,]*(?:\.\d+)?")
PERCENT_RE = re.compile(
    r"(?<![A-Za-z_\d.])(\d[\d,]*(?:\.\d+)?)\s*(?:%|percent\b|per cent\b|percentage points?\b)",
    re.IGNORECASE,
)
FY_RE = re.compile(r"\bFY\s?'?(\d{2}|\d{4})\b", re.IGNORECASE)
QUARTER_YEAR_RE = re.compile(
    r"\b(?:Q([1-4])|(first|second|third|fourth) quarter)\s*(?:of\s*)?(?:fiscal\s*(?:year\s*)?|FY\s?)?(\d{4})\b",
    re.IGNORECASE,
)
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")

ATTRIBUTION_PATTERN = re.compile(
    r"\b(?:report(?:s|ed|ing)?|according to|said|says|stated|states|"
    r"announc\w*|wrote|writes|headline|describe[sd]?|claim(?:s|ed)?|"
    r"publish\w*|cites?|cited|notes?d?|indicates?d?)\b",
    re.IGNORECASE,
)

HEDGE_PATTERN = re.compile(
    r"\b(?:could|may|might|potential(?:ly)?|possibl[ey]|if|whether|"
    r"would|uncertain|unclear|not (?:yet )?(?:known|established|verified))\b",
    re.IGNORECASE,
)

FINANCIAL_TERMS = re.compile(
    r"\b(?:revenue|revenues|sales|earnings|profit\w*|margin\w*|cash flow|"
    r"income|stock|shares|valuation|guidance)\b",
    re.IGNORECASE,
)

ORDINAL_QUARTERS = {"first": 1, "second": 2, "third": 3, "fourth": 4}

# Counts, quarter numbers, list positions, "10-Q"/"10-K", 95% interval.
ALWAYS_ALLOWED_NUMBERS = {Decimal(i) for i in range(0, 13)} | {Decimal(95)}


class NarrativeRejected(ValueError):
    """A specific piece of generated text failed a check. Message is content-free."""


# ------------------------------------------------------------------ numbers

def to_decimal(value):
    try:
        result = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def normalize(value):
    dec = to_decimal(value)
    return None if dec is None else abs(dec).normalize()


def quantize(value, places, rounding=ROUND_HALF_EVEN):
    return value.quantize(Decimal(1).scaleb(-places), rounding=rounding)


def rounded_forms(value, places):
    """Both common rounding conventions (half-up and banker's)."""
    return {
        quantize(value, places, ROUND_HALF_UP).normalize(),
        quantize(value, places, ROUND_HALF_EVEN).normalize(),
    }


def usd_number_forms(value_usd):
    """Raw value plus billions (0-3 dp) and millions (0-1 dp) forms."""
    raw = to_decimal(value_usd)
    if raw is None:
        return set()
    raw = abs(raw)
    forms = {raw.normalize()}
    for divisor, places_list in (
        (Decimal(1_000_000_000), (0, 1, 2, 3)),
        (Decimal(1_000_000), (0, 1)),
    ):
        scaled = raw / divisor
        for places in places_list:
            forms |= rounded_forms(scaled, places)
    return forms


def decimal_forms(value, places=(0, 1, 2)):
    dec = to_decimal(value)
    if dec is None:
        return set()
    dec = abs(dec)
    forms = {dec.normalize()}
    for p in places:
        forms |= rounded_forms(dec, p)
    return forms


def date_number_forms(iso_date):
    """Year, month and day of an ISO date as numbers (for 'June 30, 2025')."""
    out = set()
    match = ISO_DATE.search(str(iso_date or ""))
    if match:
        for part in match.groups():
            out.add(Decimal(int(part)))
    return out


def numbers_in_text(text):
    """Numeric tokens outside ISO dates."""
    stripped = ISO_DATE.sub(" ", text)
    out = []
    for token in NUMBER_RE.findall(stripped):
        dec = normalize(token)
        if dec is not None:
            out.append((token, dec))
    return out


def percentages_in_text(text):
    out = []
    for token in PERCENT_RE.findall(text):
        dec = normalize(token)
        if dec is not None:
            out.append((token, dec))
    return out


# ------------------------------------------------------------------- checks

def sentences(text):
    text = (text or "").strip()
    if not text:
        return []
    return [s for s in SENTENCE_SPLIT.split(text) if s.strip()]


def word_count(text):
    return len((text or "").split())


def check_length(text, field, min_words=0, max_words=None, max_sentences=None, required=True):
    text = (text or "").strip()
    if required and not text:
        raise NarrativeRejected(f"{field}: empty")
    words = word_count(text)
    if text and words < min_words:
        raise NarrativeRejected(f"{field}: too short")
    if max_words is not None and words > max_words:
        raise NarrativeRejected(f"{field}: too long")
    if max_sentences is not None and len(sentences(text)) > max_sentences:
        raise NarrativeRejected(f"{field}: too many sentences")


CAUSAL_PATTERNS = {
    r"\bcaused\b", r"\bcausing\b", r"\bled to\b", r"\bresulted in\b",
    r"\bdrove\b", r"\bdriven by\b",
}

NEGATION_PATTERN = re.compile(
    r"\b(?:cannot|can't|can not|not|no|unknown|unclear|unable|without|"
    r"does not|do not|did not|neither|nor)\b",
    re.IGNORECASE,
)


def check_language(text, field, allow_limitation_phrases=False):
    lowered = (text or "").lower()
    for pattern in BANNED_PATTERNS + EXTRA_BANNED_PATTERNS:
        if allow_limitation_phrases and pattern in LIMITATION_SAFE_PATTERNS:
            continue
        if allow_limitation_phrases and pattern in CAUSAL_PATTERNS:
            # Limitations may say what the evidence cannot establish
            # ("cannot show what drove the change"); a causal word is
            # accepted only inside a negated sentence.
            for sentence in sentences(text):
                low = sentence.lower()
                if re.search(pattern, low) and not NEGATION_PATTERN.search(low):
                    raise NarrativeRejected(
                        f"{field}: causal, certain or advisory language"
                    )
            continue
        if re.search(pattern, lowered):
            raise NarrativeRejected(f"{field}: causal, certain or advisory language")
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, lowered):
            raise NarrativeRejected(f"{field}: instruction-like text")
    if URL_PATTERN.search(text or ""):
        raise NarrativeRejected(f"{field}: URL in prose")


def check_evidence_tokens(text, field, allowed_ids):
    for token in EVIDENCE_TOKEN.findall(text or ""):
        if token not in allowed_ids:
            raise NarrativeRejected(f"{field}: mentions an evidence ID it does not cite")


def check_numbers(text, field, allowed_numbers, allowed_percentages, allowed_dates):
    text = text or ""

    for match in ISO_DATE.finditer(text):
        if match.group(0) not in allowed_dates:
            raise NarrativeRejected(f"{field}: date not in cited evidence")

    for token, value in percentages_in_text(text):
        if value not in allowed_percentages:
            raise NarrativeRejected(f"{field}: percentage {token} not in cited comparisons")

    for token, value in numbers_in_text(text):
        if value in ALWAYS_ALLOWED_NUMBERS:
            continue
        if value not in allowed_numbers and value not in allowed_percentages:
            raise NarrativeRejected(f"{field}: number {token} not in cited evidence")


def check_periods(text, field, allowed_fiscal_years, allowed_quarter_years):
    text = text or ""

    for match in FY_RE.finditer(text):
        raw = match.group(1)
        year = int(raw) if len(raw) == 4 else 2000 + int(raw)
        if year not in allowed_fiscal_years:
            raise NarrativeRejected(f"{field}: fiscal year not in cited evidence")

    for match in QUARTER_YEAR_RE.finditer(text):
        quarter = int(match.group(1)) if match.group(1) else ORDINAL_QUARTERS[match.group(2).lower()]
        year = int(match.group(3))
        if (quarter, year) not in allowed_quarter_years:
            raise NarrativeRejected(f"{field}: quarter label not in cited evidence")


def check_trend_language(text, field, contiguous_periods_by_metric):
    if TREND_PATTERN.search(text or ""):
        if max(contiguous_periods_by_metric.values(), default=0) < 3:
            raise NarrativeRejected(
                f"{field}: trend language without three comparable cited periods"
            )


def check_hedged_financial_implications(text, field):
    """Sentences about financial outcomes must be hedged (news implications)."""
    for sentence in sentences(text):
        if FINANCIAL_TERMS.search(sentence) and not HEDGE_PATTERN.search(sentence):
            raise NarrativeRejected(f"{field}: unhedged financial-impact statement")


# ---------------------------------------------------------- allowance sets

class Allowance:
    """Numbers, percentages, dates and periods that cited evidence supports."""

    def __init__(self):
        self.numbers = set()
        self.percentages = set()
        self.dates = set()
        self.fiscal_years = set()
        self.quarter_years = set()

    def add_item(self, item):
        for key in ("numbers", "percentages", "dates", "fiscal_years", "quarter_years"):
            getattr(self, key).update(item.get("_allow", {}).get(key, ()))
        return self

    @classmethod
    def from_items(cls, items):
        allowance = cls()
        for item in items:
            allowance.add_item(item)
        return allowance


def build_item_allowance(
    *, usd_values=(), decimals=(), percentages=(), dates=(),
    fiscal_labels=(), calendar_labels=(), extra_numbers=(),
):
    numbers = set()
    for value in usd_values:
        numbers |= usd_number_forms(value)
    for value in decimals:
        numbers |= decimal_forms(value)
    for value in extra_numbers:
        dec = normalize(value)
        if dec is not None:
            numbers.add(dec)

    pct = set()
    for value in percentages:
        pct |= decimal_forms(value, places=(0, 1))

    date_set = set()
    for value in dates:
        if value:
            text = str(value)[:10]
            if ISO_DATE.fullmatch(text):
                date_set.add(text)
                numbers |= date_number_forms(text)

    fiscal_years, quarter_years = set(), set()
    for label in fiscal_labels:
        match = re.match(r"FY(\d{4})(?: Q([1-4]))?$", str(label or ""))
        if match:
            fy = int(match.group(1))
            fiscal_years.add(fy)
            if match.group(2):
                quarter_years.add((int(match.group(2)), fy))
            numbers.add(Decimal(fy))
    for label in calendar_labels:
        match = re.match(r"(\d{4}) Q([1-4])", str(label or ""))
        if match:
            year, q = int(match.group(1)), int(match.group(2))
            quarter_years.add((q, year))
            numbers.add(Decimal(year))

    return {
        "numbers": numbers,
        "percentages": pct,
        "dates": date_set,
        "fiscal_years": fiscal_years,
        "quarter_years": quarter_years,
    }


def text_number_allowance(text):
    """Numbers/percentages that literally appear in a source text (news)."""
    numbers = {value for _, value in numbers_in_text(text or "")}
    pcts = {value for _, value in percentages_in_text(text or "")}
    dates = {m.group(0) for m in ISO_DATE.finditer(text or "")}
    for d in dates:
        numbers |= date_number_forms(d)
    return numbers, pcts, dates


# ------------------------------------------------------------- company scope
# While AI text for one company is validated, the company and the evidence
# text sent to the model are bound to the current thread. Any other company
# from the InsightIQ universe that the text names must also be named in that
# evidence; otherwise the text is rejected as cross-company contamination.

import contextlib as _contextlib
import threading as _threading

_SCOPE = _threading.local()


@_contextlib.contextmanager
def company_scope(ticker, evidence_text):
    previous = getattr(_SCOPE, "value", None)
    _SCOPE.value = (str(ticker).upper(), str(evidence_text or ""))
    try:
        yield
    finally:
        _SCOPE.value = previous


def foreign_companies(text, ticker, evidence_text=""):
    """Universe companies (other than ``ticker``) named in text but not in evidence."""
    from app.services.company_identity import universe_identities
    from app.services.news_relevance import find_mentions

    found = []
    for other in universe_identities():
        if other.ticker == ticker:
            continue
        if find_mentions(text, other, strong_only=True) and not find_mentions(evidence_text, other):
            found.append(other.ticker)
    return found


def check_company_scope(text, field):
    scope = getattr(_SCOPE, "value", None)
    if not scope:
        return
    ticker, evidence_text = scope
    foreign = foreign_companies(text, ticker, evidence_text)
    if foreign:
        raise NarrativeRejected(
            f"{field}: names another company not present in the cited evidence ({', '.join(foreign[:3])})"
        )


def check_prose_field(
    text, field, allowance, allowed_ids, *,
    min_words=0, max_words=None, max_sentences=None,
    required=True, limitation=False, contiguous=None,
):
    check_length(text, field, min_words, max_words, max_sentences, required)
    if not (text or "").strip():
        return
    check_language(text, field, allow_limitation_phrases=limitation)
    check_evidence_tokens(text, field, allowed_ids)
    check_numbers(text, field, allowance.numbers, allowance.percentages, allowance.dates)
    check_periods(text, field, allowance.fiscal_years, allowance.quarter_years)
    check_company_scope(text, field)
    if contiguous is not None:
        check_trend_language(text, field, contiguous)


def passed_validation(checks):
    return {
        "status": "automated_checks_passed",
        "checks": list(checks),
        "label": VALIDATION_LABEL,
        "independently_verified": False,
    }
