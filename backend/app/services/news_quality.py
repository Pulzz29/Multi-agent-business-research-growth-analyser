import re
from urllib.parse import urlparse


class NewsQualityError(ValueError):
    pass


def company_identity(ticker):
    """Verified identity used for headline attribution (see company_identity.py)."""
    from app.services.company_identity import identity_for

    try:
        return identity_for(ticker)
    except Exception:
        raise NewsQualityError("Unsupported company ticker.") from None


def company_names(ticker):
    return tuple(n.text for n in company_identity(ticker).names)


def explicit_company_mention(text, ticker):
    """True when ``text`` names the company under the strict matching rules."""
    from app.services.news_relevance import mentions_company

    return mentions_company(text, company_identity(ticker))


def validate_article_source(article, available_by_id, ticker):
    evidence_id = article.get("evidence_id")

    if evidence_id not in available_by_id:
        raise NewsQualityError(
            "Selected article is absent from source evidence."
        )

    original = available_by_id[evidence_id]

    if (
        article.get("ticker") != ticker
        or original.get("ticker") != ticker
    ):
        raise NewsQualityError(
            "Article company identifier mismatch."
        )

    for field in (
        "database_id",
        "title",
        "description",
        "source_url",
        "published_at",
        "publisher",
        "relevance_score",
        "relevance_reason",
    ):
        if article.get(field) != original.get(field):
            raise NewsQualityError(
                "Source metadata mismatch: " + field
            )

    parsed = urlparse(article["source_url"])

    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise NewsQualityError(
            "Invalid article source URL."
        )

    if not article.get("title"):
        raise NewsQualityError(
            "Missing article headline."
        )

    return True


def classify_article(article, ticker):
    title = article.get("title") or ""
    description = article.get("description") or ""

    # Conservative attribution policy:
    # Only a company explicitly named in the headline
    # qualifies as direct company-focused evidence.
    if explicit_company_mention(title, ticker):
        category = "direct_company_coverage"
        strategic_eligible = True

    elif explicit_company_mention(description, ticker):
        category = "company_mentioned_in_context"
        strategic_eligible = False

    else:
        category = "general_industry_context"
        strategic_eligible = False

    title_lower = title.lower()

    # An event theme is not a unique article or verified fact.
    # Themes help avoid presenting related headlines
    # as independent events.
    event_theme = "other"

    if any(
        word in title_lower
        for word in ("outage", "downtime", "disruption")
    ):
        event_theme = "service_disruption"

    elif any(
        word in title_lower
        for word in ("earnings", "revenue", "profit")
    ):
        event_theme = "financial_reporting"

    elif any(
        word in title_lower
        for word in ("launch", "release", "product")
    ):
        event_theme = "product_development"

    elif any(
        word in title_lower
        for word in ("lawsuit", "antitrust", "regulation")
    ):
        event_theme = "regulatory_development"

    return {
        **article,
        "evidence_category": category,
        "company_specific_evidence_eligible": strategic_eligible,
        "event_theme": event_theme,
        "event_independence_verified": False,
        "business_impact_verified": False,
    }


def assess_news_report(report, ticker):
    if report.get("ticker") != ticker:
        raise NewsQualityError(
            "News report company mismatch."
        )

    if report.get("status") != "completed":
        raise NewsQualityError(
            "News Agent report was not completed."
        )

    available = report.get("available_articles") or []
    selected = report.get("selected_articles") or []

    if not available or not selected:
        raise NewsQualityError(
            "No news evidence available for assessment."
        )

    available_by_id = {
        article["evidence_id"]: article
        for article in available
    }

    if len(available_by_id) != len(available):
        raise NewsQualityError(
            "Duplicate available evidence identifiers."
        )

    seen = set()
    primary = []
    contextual = []

    for article in selected:
        evidence_id = article["evidence_id"]

        if evidence_id in seen:
            raise NewsQualityError(
                "Duplicate selected article."
            )

        seen.add(evidence_id)

        validate_article_source(
            article,
            available_by_id,
            ticker,
        )

        classified = classify_article(
            article,
            ticker,
        )

        if classified["company_specific_evidence_eligible"]:
            primary.append(classified)
        else:
            contextual.append(classified)

    # Reporting similar article themes does not prove
    # whether two publishers describe the same event.
    themes = {}

    for article in primary:
        theme = article["event_theme"]
        themes.setdefault(theme, []).append(
            article["evidence_id"]
        )

    repeated_themes = {
        theme: ids
        for theme, ids in themes.items()
        if theme != "other" and len(ids) > 1
    }

    return {
        "ticker": ticker,
        "news_run_id": report["run_id"],
        "status": "validated",
        "primary_company_evidence": primary,
        "contextual_articles": contextual,
        "repeated_event_themes": repeated_themes,
        "eligible_primary_count": len(primary),
        "contextual_count": len(contextual),
        "source_attribution_policy": (
            "Direct company coverage requires an explicit "
            "company name in the stored headline."
        ),
        "limitations": [
            "Headline company attribution does not prove "
            "the truth of all statements in an article.",
            "General industry articles must not be "
            "attributed to the company as direct events.",
            "Repeated themes do not independently verify "
            "distinct incidents or their business impact.",
            "NewsData.io pagination and coverage limitations "
            "remain unresolved.",
        ],
    }
