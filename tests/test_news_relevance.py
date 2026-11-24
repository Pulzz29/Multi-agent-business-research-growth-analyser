"""
Strict company-specific news relevance: regression cases (accept and reject).

Identities are the real companies (from the curated universe, or built with
the company's real legal name and ticker for Abbott Laboratories, which is not
in the universe). Headlines are test inputs written in the style of real
coverage; they are never stored anywhere. Offline and deterministic.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.services.company_identity import build_identity, universe_identities
from app.services.news_relevance import (
    assess_article,
    canonical_url,
    filter_company_news,
    find_mentions,
    same_event,
)

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
UNIVERSE = universe_identities()
BY_TICKER = {i.ticker: i for i in UNIVERSE}
ABBOTT = build_identity("ABT", "US", "Abbott Laboratories", aliases=["Abbott"],
                        legal_name="ABBOTT LABORATORIES", cik="0000001800", currency="USD")
OTHERS = UNIVERSE + (ABBOTT,)


def article(title, description="", hours_ago=5, url=None):
    return {
        "title": title,
        "description": description,
        "published_at": (NOW - timedelta(hours=hours_ago)).isoformat(),
        "article_url": url or f"https://news.example.invalid/{abs(hash(title))}",
    }


def verdict(ticker_or_identity, title, description="", **kw):
    identity = BY_TICKER[ticker_or_identity] if isinstance(ticker_or_identity, str) else ticker_or_identity
    return assess_article(article(title, description, **kw), identity, others=OTHERS, now=NOW)


def accepted(*args, **kw):
    return verdict(*args, **kw)["decision"] == "accept"


# ------------------------------------------------------- ambiguous names

@pytest.mark.parametrize("title", [
    "Oracle shares jump after cloud revenue beats estimates",
    "Oracle Corp to invest $10 billion in new data centers",
    "Oracle unveils AI database agents for enterprise customers",
    "Shares of Oracle fall as capex outlook rises",
])
def test_oracle_corporation_is_recognised(title):
    assert accepted("ORCL", title)


@pytest.mark.parametrize("title,reason", [
    ("Giants beat Dodgers 5-3 at Oracle Park", "company_not_mentioned"),
    ("Fans pack Oracle Park for season finale", "company_not_mentioned"),
    ("The Oracle of Omaha explains his latest letter to shareholders", "company_not_mentioned"),
    ("Warren Buffett, the Oracle of Omaha, trims stake in Apple", None),
])
def test_oracle_park_and_oracle_of_omaha_are_not_oracle_corporation(title, reason):
    result = verdict("ORCL", title)
    assert result["decision"] == "reject", result
    if reason:
        assert result["reason"] == reason


@pytest.mark.parametrize("title", [
    "Apple unveils iPhone 18 with new AI features",
    "Apple's services revenue hits record in fourth quarter",
    "EU fines Apple over App Store rules",
    "Apple Inc. plans $110 billion share buyback",
])
def test_apple_inc_is_recognised(title):
    assert accepted("AAPL", title)


@pytest.mark.parametrize("title", [
    "Local apple harvest draws crowds to orchards this weekend",
    "How to bake the perfect apple pie",
    "Big Apple restaurants brace for winter slowdown",
    "An apple a day: what nutritionists really say",
])
def test_unrelated_uses_of_apple_are_rejected(title):
    assert not accepted("AAPL", title)


@pytest.mark.parametrize("title", [
    "Meta Platforms to spend more on AI infrastructure in 2027",
    "Meta shares slide after capex guidance",
    "Meta launches new Ray-Ban smart glasses",
])
def test_meta_platforms_is_recognised(title):
    assert accepted("META", title)


@pytest.mark.parametrize("title,description", [
    ("New meta-analysis links sleep and memory", ""),
    ("Meta-analysis questions vitamin D benefits", ""),
    ("Chennai police arrest two in theft case",
     "Follow Tamil News on Facebook (Meta), X and Instagram for the latest updates."),
    ("How to install the Meta Pixel on your website", ""),
])
def test_unrelated_uses_of_meta_are_rejected(title, description):
    assert not accepted("META", title, description)


@pytest.mark.parametrize("title", [
    "Gov. Greg Abbott signs Texas property tax bill",
    "Governor Abbott deploys National Guard to border",
    "Greg Abbott says Texas will appeal the ruling",
])
def test_governor_greg_abbott_is_not_abbott_laboratories(title):
    assert not accepted(ABBOTT, title)


@pytest.mark.parametrize("title", [
    "Abbott beats quarterly estimates on medical device demand",
    "Abbott recalls some FreeStyle Libre sensors",
    "Abbott Laboratories wins FDA approval for new heart device",
    "Abbott shares rise after raising full-year outlook",
])
def test_genuine_abbott_business_headlines_are_accepted(title):
    assert accepted(ABBOTT, title)


@pytest.mark.parametrize("ticker,title,expected", [
    ("TGT", "Target cuts annual sales forecast as shoppers pull back", True),
    ("TGT", "Analysts raise price target for Nvidia ahead of earnings", False),
    ("F", "Ford recalls 300,000 pickups over brake issue", True),
    ("F", "Harrison Ford returns for one last adventure", False),
    ("LCID", "Lucid Group raises $1.75 billion in convertible notes", True),
    ("LCID", "Five tips for lucid dreaming tonight", False),
    ("ETERNAL.NS", "Eternal's Blinkit expands quick commerce to 50 new cities", True),
    ("ETERNAL.NS", "Rome, the Eternal City, welcomes record tourists", False),
])
def test_common_word_company_names(ticker, title, expected):
    assert accepted(ticker, title) is expected


# ------------------------------------------------------ tickers and similar names

@pytest.mark.parametrize("ticker,title,expected", [
    ("LOW", "Lowe's beats profit estimates as pro sales grow", True),
    ("LOW", "Mortgage rates fall to a low not seen since 2022", False),
    ("COST", "Costco quarterly sales rise 8% on membership growth", True),
    ("COST", "The real cost of owning an electric car", False),
    ("ORCL", "Why $ORCL could rally after its Q2 earnings", True),
])
def test_word_tickers_and_ticker_formats(ticker, title, expected):
    assert accepted(ticker, title) is expected


def test_indian_and_us_identities_do_not_collide():
    # ITC (India) is a common US acronym (investment tax credit / trade commission).
    assert accepted("ITC.NS", "ITC Ltd Q2 results: net profit rises 6% on cigarette volumes")
    assert not accepted("ITC.NS", "Solar developers rush to claim the ITC before deadline")
    # Tata group companies are distinct legal entities.
    assert accepted("TCS.NS", "TCS bags $1 billion deal from UK insurer")
    assert not accepted("TCS.NS", "Tata Motors launches new electric SUV")
    assert not accepted("TATACOMM.NS", "Tata Consultancy Services posts higher quarterly profit")


def test_parent_company_news_is_not_attributed_to_listed_subsidiary():
    assert not accepted("NESTLEIND.NS", "Nestle SA cuts sales outlook as demand weakens in Europe")
    assert accepted("NESTLEIND.NS", "Nestle India Q2 profit rises on price increases")
    assert not accepted("HINDUNILVR.NS", "Unilever plc names new chief executive")


# ------------------------------------------------------- subject and materiality

def test_competitor_focused_article_is_rejected():
    result = verdict("AMZN", "Walmart outpaces Amazon in grocery delivery growth")
    assert result["decision"] == "reject" and result["reason"] == "another_company_is_subject"
    assert accepted("WMT", "Walmart outpaces Amazon in grocery delivery growth")


def test_incidental_mention_in_description_only_is_rejected():
    result = verdict("MSFT", "Five productivity tips for remote workers",
                     "Tools from Microsoft, Google and others can help.")
    assert result["decision"] == "reject" and result["reason"] == "company_not_in_headline"


@pytest.mark.parametrize("title", [
    "Semiconductor industry sales rise 20% year over year",
    "Global smartphone shipments fall in the third quarter",
])
def test_generic_industry_articles_are_rejected(title):
    assert not accepted("NVDA", title)


@pytest.mark.parametrize("title", [
    "3 tech stocks to buy now: Nvidia, Microsoft and Apple",
    "Top 10 stocks to watch this week including Nvidia",
    "Nvidia among buzzing stocks in Monday trade",
    "Sensex, Nifty close higher; Infosys gains",
])
def test_watchlists_and_market_wraps_are_rejected(title):
    for ticker in ("NVDA", "MSFT", "AAPL", "INFY.NS"):
        assert not accepted(ticker, title), (ticker, title)


@pytest.mark.parametrize("title", [
    "Vanguard Group Inc. raises stake in Microsoft Corporation",
    "Smith Wealth Management LLC acquires new position in Nvidia Corporation",
    "Microsoft shares sold by Acme Capital Advisors LLC",
])
def test_routine_fund_holding_changes_are_rejected(title):
    ticker = "NVDA" if "Nvidia" in title else "MSFT"
    assert verdict(ticker, title)["reason"] == "routine_fund_holding_change"


@pytest.mark.parametrize("ticker,title,event", [
    ("MSFT", "Microsoft quarterly revenue rises 15% on Azure growth", "earnings"),
    ("TSLA", "Tesla prices $5 billion bond offering", "financing"),
    ("AAPL", "Apple launches new MacBook Pro with M6 chip", "product"),
    ("GOOGL", "Alphabet to acquire cybersecurity firm in $32 billion deal", "mergers_acquisitions"),
    ("NVDA", "Nvidia partners with Reliance to build AI data centres in India", "partnership"),
    ("LLY", "FDA approves Eli Lilly's new obesity drug", "regulatory"),
])
def test_genuine_material_events_are_accepted_with_event_type(ticker, title, event):
    result = verdict(ticker, title)
    assert result["decision"] == "accept", result
    assert result["event_type"] == event


def test_party_to_material_event_is_accepted_when_named_second():
    result = verdict("RELIANCE.NS", "Nvidia partners with Reliance Industries to build AI data centres in India")
    assert result["decision"] == "accept" and result["reason"] == "company_party_to_material_event"
    assert result["primary_subject"] is False


def test_conflicting_headline_and_description_identity():
    # Headline is about Pfizer; description mentions Merck in passing.
    result = verdict("MRK", "Pfizer wins approval for RSV vaccine",
                     "The decision puts Pfizer ahead of Merck and GSK in the market.")
    assert result["decision"] == "reject"
    assert not accepted("MRK", "Merck KGaA raises guidance for electronics unit")


def test_missing_description_is_handled():
    assert accepted("NFLX", "Netflix raises subscription prices in the US", description="")
    assert verdict("NFLX", "", description="Netflix raises prices")["reason"] == "missing_headline"


def test_stale_and_undated_articles_are_rejected():
    assert verdict("MSFT", "Microsoft unveils new Surface devices", hours_ago=24 * 90)["reason"] == "outside_coverage_period"
    undated = {"title": "Microsoft unveils new Surface devices", "published_at": None}
    assert assess_article(undated, BY_TICKER["MSFT"], others=OTHERS, now=NOW)["reason"] == "missing_publication_date"


# ------------------------------------------------------------ duplicates

def test_duplicate_articles_and_events_are_collapsed():
    rows = [
        {"id": 1, **article("Microsoft to cut 6,000 jobs in restructuring", hours_ago=10,
                             url="https://www.example.invalid/story?utm=1")},
        {"id": 2, **article("Microsoft to cut 6,000 jobs in restructuring", hours_ago=8,
                             url="http://example.invalid/story/")},
        {"id": 3, **article("Microsoft to cut 6,000 jobs in a restructuring", hours_ago=6,
                             url="https://other.invalid/a")},
        {"id": 4, **article("Microsoft launches new Copilot features for Office", hours_ago=2)},
    ]
    kept, rejected = filter_company_news(rows, BY_TICKER["MSFT"], others=OTHERS, now=NOW)
    assert [r["id"] for r in kept] == [4, 1]
    reasons = {r["row"]["id"]: r["reason"] for r in rejected}
    assert reasons == {2: "duplicate_article", 3: "duplicate_event"}
    assert canonical_url("http://www.Example.invalid/story/?x=1") == "https://example.invalid/story"


def test_same_event_requires_time_proximity():
    a = NOW
    assert same_event("Infosys wins large deal from European bank", a,
                      "Infosys wins large deal from European bank", a - timedelta(hours=10))
    # Identical headlines are one event within 14 days, near-identical ones within 72 hours.
    assert same_event("Infosys wins large deal from European bank", a,
                      "Infosys wins large deal from European bank", a - timedelta(days=10))
    assert not same_event("Infosys wins large deal from European bank", a,
                          "Infosys wins large deal from European bank", a - timedelta(days=20))
    assert not same_event("Infosys wins large deal from European bank", a,
                          "Infosys wins large deal from big European bank", a - timedelta(days=10))


def test_mentions_report_their_strength():
    strong = find_mentions("Oracle Corporation reports results", BY_TICKER["ORCL"])
    assert strong and strong[0]["strength"] == "strong"
    contextual = find_mentions("Oracle shares rise", BY_TICKER["ORCL"])
    assert contextual and contextual[0]["strength"] == "contextual"
    assert find_mentions("A visit to Oracle Park", BY_TICKER["ORCL"]) == []


# ------------------------------------------- patterns found in stored articles

def test_subject_of_main_clause_after_subordinate_opening():
    title = ("Days after telling Apple CEO, 'great doing business with you', "
             "Meta's AI chief Alexandr Wang takes a dig")
    assert verdict("AAPL", title)["reason"] == "another_company_is_subject"
    # ...and the main-clause company is the subject (stored headline, curly apostrophe).
    stored = ("Days after telling Apple CEO, 'great doing business with you', Meta’s AI chief "
              "Alexandr Wang takes a dig at iPhone maker; says: Yet another...")
    for text in (title, stored):
        result = verdict("META", text)
        assert result["decision"] == "accept" and result["primary_subject"], result


@pytest.mark.parametrize("ticker,title", [
    ("GOOGL", "Alphabet (NASDAQ:GOOG) vs. Grindr (NYSE:GRND) Critical Contrast"),
    ("META", "Analyzing WEBTOON Entertainment (NASDAQ:WBTN) & Meta Platforms (NASDAQ:META)"),
])
def test_auto_generated_comparison_articles_are_rejected(ticker, title):
    assert verdict(ticker, title)["reason"] == "comparison_article"


@pytest.mark.parametrize("ticker,title", [
    ("AAPL", "Apple Maps launches revamped features in India with 3D landmarks"),
    ("AMZN", "Amazon, BigBasket Face FDA Action In Pune Over Food Safety Violations"),
])
def test_product_and_multi_party_regulatory_headlines_are_accepted(ticker, title):
    assert accepted(ticker, title)


def test_retail_deal_listing_on_amazon_is_rejected():
    assert not accepted("AMZN", "PUMA Men's Sock 8-Pack Just $7.99 Shipped on Amazon (Reg. $20)")


def test_identical_headlines_days_apart_are_one_event():
    title = "Alphabet Stock Just Hit a 1-Year Low. Why Is Google's Parent Borrowing So Much Money?"
    rows = [{"id": 1, **article(title, hours_ago=100, url="https://a.invalid/1")},
            {"id": 2, **article(title, hours_ago=20, url="https://b.invalid/2")}]
    kept, rejected = filter_company_news(rows, BY_TICKER["GOOGL"], others=OTHERS, now=NOW)
    assert [r["id"] for r in kept] == [1] and rejected[0]["reason"] == "duplicate_event"


@pytest.mark.parametrize("ticker,title,expected", [
    ("META", "Meta rethinks 2030 net-zero goal as AI data center buildout drives record emissions surge", True),
    ("META", "Mark Zuckerberg's Meta Platforms is pouring up to $145 billion into AI this year", True),
    ("NVDA", "Anthropic stock slides with rest of market after big-ticker IPO",
     False),
    ("MSFT", "Man used Microsoft Teams to hound ex-partner he is banned from contacting", False),
    ("MSFT", "Decoding Microsoft CEO Satya Nadella's 30 Minutes Fitness Routine", False),
    ("MSFT", "Microsoft (NASDAQ:MSFT) Stock Unloaded by Rep. Thomas H. Kean, Jr.", False),
])
def test_subject_grammar_personal_stories_and_disclosures(ticker, title, expected):
    assert accepted(ticker, title, "Nvidia was among the stocks that fell." if ticker == "NVDA" else "") is expected


def test_description_only_mention_never_qualifies_even_with_headline_event():
    result = verdict("NVDA", "Chipmaker shares slide after export curbs",
                     "Nvidia led the decline as new export rules took effect.")
    assert result["decision"] == "reject" and result["reason"] == "company_not_in_headline"


def test_newsdata_queries_fit_the_provider_limit_and_drop_redundant_forms():
    """Regression: NewsData.io answered HTTP 422 for queries over 100 characters
    (e.g. "Tata Consultancy Services" OR ... OR "Tata Consultancy Services Limited")."""
    from app.services.news_ingestion import NEWSDATA_QUERY_MAX_CHARS, news_query_terms

    for identity in UNIVERSE:
        terms = news_query_terms(identity)
        query = " OR ".join(f'"{t}"' for t in terms)
        assert terms and len(query) <= NEWSDATA_QUERY_MAX_CHARS, (identity.ticker, query)
    assert news_query_terms(BY_TICKER["INFY.NS"]) == ["Infosys"]
    assert news_query_terms(BY_TICKER["TCS.NS"]) == ["Tata Consultancy Services", "TCS"]


@pytest.mark.parametrize("ticker,title", [
    ("ITC.NS", "CBIC draft GST proposal to shield buyers from ITC loss"),
    ("ETERNAL.NS", "Kanchipuram Blinkit delivery man clears CA exam; recalls how he rented a bicycle"),
    ("ETERNAL.NS", "Blinkit Delivery Partner Earns Rs 39,000 In First Month; Shares His Experience"),
    ("TCS.NS", "She worked at TCS and Accenture, then started a business from home with Rs 11,000"),
    ("TCS.NS", "Adani Green Energy, TCS and HCL Tech among 15 stocks trimmed by mutual funds in October"),
    ("META", "Mark Zuckerberg Meta AI Predicts LINK Vs. AVAX: Which Goes Higher in 2027?"),
    ("MSFT", "Clinical AI Model Governance Market Competitive Analysis: FMI Identifies Leading Technology Companies"),
])
def test_stored_false_positives_found_in_live_data_are_rejected(ticker, title):
    """Regression cases taken from real stored NewsData articles (2026-11-24)."""
    assert not accepted(ticker, title), title


@pytest.mark.parametrize("ticker,title", [
    ("F", "Ford recalls 300,000 pickups over brake issue"),
    ("ABT", "Abbott recalls some FreeStyle Libre sensors"),
    ("ITC.NS", "ITC Ltd Q2 results: net profit rises 6% on cigarette volumes"),
    ("MSFT", "Pinterest announces partnership with Microsoft Ads"),
])
def test_genuine_events_still_accepted_after_tightening(ticker, title):
    identity = ABBOTT if ticker == "ABT" else ticker
    assert accepted(identity, title), title


@pytest.mark.parametrize("ticker,title", [
    ("MSFT", "Billionaire Andreas Halvorsen Exited Microsoft and Made TSMC His Top Holding at $1.8 Billion"),
    ("MSFT", "From Microsoft to OpenAI: Mapping Nvidia's vast AI network"),
])
def test_portfolio_moves_and_from_to_roundups_are_rejected(ticker, title):
    assert not accepted(ticker, title), title
