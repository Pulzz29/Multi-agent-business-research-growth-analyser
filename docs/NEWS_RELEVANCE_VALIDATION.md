# Company-specific news: identity and relevance

When a company is opened or researched, InsightIQ shows only news in which that company is the **subject of the headline**, or a directly involved party in a material business event. If nothing qualifies, the page says:

> No verified company-specific news is available for the selected period.

## What was wrong before

The old rule counted a company as mentioned if its first name word or its ticker appeared anywhere in the headline, description or keywords. Business words were matched as substrings. Real stored results included:
- Tamil crime stories under META, because their footer said "Facebook (Meta)";
- "Meta Pixel" cookie-banner posts;
- a sock deal under AMZN;
- the word "eternal" matched to Eternal Ltd;
- GST "input tax credit (ITC)" stories under ITC Ltd;
- auto-generated "X vs. Y" stock comparisons, fund-holding filings and stock-tips columns;
- syndicated duplicates.

Dashboard cards showed every stored row, even ones scored 0.05.

## How it works now

| Part | Where |
|---|---|
| Company identity | `backend/app/services/company_identity.py`. Display name, legal name (SEC registry or provider profile), CIK, exchange, currency, and verified aliases from `core/market_universe.json` (US `aliases`, India `news_terms`). Every name records its source; no alias is invented. |
| Ambiguity data | `backend/app/core/name_ambiguity.json`. General English: common words, surnames, word tickers, non-company collocations ("Oracle Park", "price target", "apple pie"), corporate suffixes and cues, person titles, place words. It contains no per-company rules. |
| Decision rules | `backend/app/services/news_relevance.py`. Deterministic and versioned (`RELEVANCE_VERSION`), with no AI and no network. |

**Name matching**
- Distinctive names match as whole words, case-insensitively.
- Ambiguous names (Apple, Oracle, Meta, Target, Ford, Lucid, Eternal, Titan, ITC, GM…) count only when all of these hold:
  - the name is capitalised;
  - it is not part of a known non-company phrase, a hyphenated word, a place or venue, an epithet ("Oracle of Omaha"), or a person's name or title ("Gov. Greg Abbott");
  - corporate context is next to it, for example "Oracle shares", "Apple unveils", "Shares of Target", "Abbott's", or a headline-initial "Meta rethinks".
- Prepositions alone are not context: "from ITC loss" and "worked at TCS" do not count.
- Tickers count in explicit formats (`$ORCL`, `NYSE: ORCL`, `(ORCL)`). A plain upper-case ticker counts only when it is not an ordinary word.

**Accept** an article only when the company is:
- **the subject of the headline:** it is the first organisation named, or the main clause's subject after a subordinate opening ("Days after …, Meta's AI chief …"); or
- **a direct party to a material event in the headline:** M&A, partnership, legal, regulatory, financing or operations, and the headline is not comparative.

**Reject** an article for any of these reasons:

| Reason code | Covers |
|---|---|
| company named only in the description | `company_not_in_headline` |
| another company is the subject | `another_company_is_subject` |
| several companies in a roundup | `multi_company_roundup` |
| automatic comparisons ("X (NASDAQ:…) vs. Y (NYSE:…)") | `comparison_article` |
| watchlists, listicles, "among 15 stocks", price predictions, industry market reports | `watchlist_or_listicle` |
| fund and politician holding filings, "top holding" | `routine_fund_holding_change` |
| personal and human-interest stories | `personal_or_lifestyle_story` |
| market wraps with no company event | `generic_market_commentary` |
| outside the coverage window | `outside_coverage_period` (`INSIGHTIQ_NEWS_MAX_AGE_DAYS`, default 45) |
| missing date | `missing_publication_date` |
| duplicate article (canonical URL ignoring scheme, `www.` and query) | `duplicate_article` |
| duplicate event (≥ 80% headline-token overlap within 72 h; identical headlines within 14 days) | `duplicate_event` |

## Where it applies

| Path | Behaviour |
|---|---|
| NewsData.io ingestion | headline search (`qInTitle`) on the company's names, kept within NewsData's 100-character query limit; every article is checked before storing; rejections are counted by reason |
| Finnhub news ingestion | same rules; rejections counted and logged |
| Stored-news reads | SEC company page, market company page and the news-status API show only articles that pass the **current** rules. Older stored rows that now fail are **hidden, never deleted**, and the API reports `stored_article_count`, `hidden_article_count` and `hidden_by_reason`. |
| Research | the news agent receives only accepted articles; the strategic agent only headline-attributed ones; the executive summary re-checks a report's approved articles |
| AI output | AI text naming another universe company that is not in the cited evidence is rejected (`narrative_validation.company_scope`) |

## Evidence

**Automated tests:** `tests/test_news_relevance.py` has 97 cases, all passing. Every case uses a real company identity, built from the universe or, for Abbott Laboratories, from its real legal name and ticker.

| Group | Cases (accept ✓ / reject ✗) |
|---|---|
| Oracle | ✓ "Oracle shares jump…", ✓ "Shares of Oracle fall…"; ✗ "Giants beat Dodgers at Oracle Park", ✗ "the Oracle of Omaha…" |
| Apple | ✓ "Apple unveils iPhone 18…", ✓ "EU fines Apple over App Store rules", ✓ "Apple Maps launches…"; ✗ apple harvest, apple pie, Big Apple |
| Meta | ✓ "Meta Platforms to spend more…", ✓ "Meta rethinks 2030 net-zero goal…"; ✗ meta-analysis, Meta Pixel, "Facebook (Meta)" boilerplate |
| Abbott | ✓ "Abbott beats quarterly estimates…", ✓ "Abbott recalls some FreeStyle Libre sensors"; ✗ "Gov. Greg Abbott signs…", ✗ "Governor Abbott deploys…" |
| Word names and tickers | ✓ Target, Ford, Lucid, Lowe's, Costco, Eternal business news; ✗ "price target", Harrison Ford, lucid dreaming, "a low not seen since", "the real cost of", "the Eternal City" |
| India and US | ✓ "ITC Ltd Q2 results"; ✗ "ITC before deadline" (tax credit); TCS vs Tata Motors vs Tata Communications kept apart; Nestle SA ≠ Nestle India; Unilever plc ≠ Hindustan Unilever |
| Subject and materiality | ✗ "Walmart outpaces Amazon…" for AMZN (✓ for WMT); ✗ competitor- or description-only mentions, industry news, watchlists, fund filings, roundups, price predictions |
| Material events | ✓ earnings, financing, product, M&A, partnership, regulatory, with the event type recorded |
| Hygiene | duplicates (URL and event), missing description, missing or old dates, conflicting headline and description |
| Live-data regressions | the real stored false positives listed above |

**Stored articles under the current rules** (version `2026-11-24.3`; `docs/validation_evidence/news_relevance_stored.json`, read-only):

| | Count |
|---|---|
| Stored articles | 473 |
| Shown (accepted) | 307 |
| Hidden (kept in the database, not shown or used) | 166 |
| — duplicate event | 64 |
| — company not in headline | 33 |
| — company not mentioned | 29 |
| — watchlist or listicle (incl. price predictions, market reports) | 12 |
| — personal or lifestyle story | 10 |
| — another company is the subject | 7 |
| — routine fund-holding change | 7 |
| — comparison article | 4 |

Version `2026-11-24.3` fixed a false negative found during live validation. In "Days after telling Apple CEO …, Meta's AI chief …", the subordinate-clause check already identified Meta as the subject, but a later check still treated the earlier-named Apple as the subject, which hid a genuine Meta article. Companies named only in a subordinate opening are now ignored once our company leads the main clause, and a regression case covers the stored headline.

The JSON file lists accepted and hidden examples for every company. Hidden counts change slightly as rules are refined, because each run applies the current version.

## Known limitations

- The rules are deterministic pattern rules, not language understanding. Unusual phrasing can still be misjudged in either direction. The design prefers hiding a borderline article to showing an unrelated one.
- Headline search depends on NewsData.io's coverage. Several Indian companies currently have no qualifying recent news.
- Finnhub company news needs a valid Finnhub key (see `docs/validation_evidence/provider_verification.json`).
