import asyncio
import hashlib
import json
import logging
import random
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.core.config import get_settings
from app.database.db import json_text
from app.database.sqlite_manager import (
    connect, transaction, utc_now, sync_snapshot_to_drive,
    create_recovery_point,
)

logger = logging.getLogger(__name__)

NEWS_ENDPOINT = "https://newsdata.io/api/1/latest"
RETRYABLE_HTTP = {429, 500, 502, 503, 504}


class NewsIngestionError(RuntimeError):
    pass


def parse_retry_after(value):
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            parsed = parsedate_to_datetime(value)
            return max(
                0.0,
                (parsed - datetime.now(timezone.utc)).total_seconds(),
            )
        except (TypeError, ValueError, OverflowError):
            return None


def normalized_article_url(value):
    if not isinstance(value, str):
        return None

    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return None

    if (
        parsed.scheme.lower() not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return None

    host = parsed.hostname.lower()
    if host.startswith("www."):
        host = host[4:]
    netloc = host

    if parsed.port:
        netloc += f":{parsed.port}"

    path = parsed.path or "/"

    # A canonical publisher link is used for duplicate detection.
    return urlunsplit(
        ("https", netloc, path.rstrip("/") or "/", "", "")
    )


def parse_published_at(value):
    if not isinstance(value, str) or not value.strip():
        return None

    try:
        parsed = datetime.fromisoformat(
            value.strip().replace("Z", "+00:00")
        )
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc).isoformat()


def validate_article(article):
    if not isinstance(article, dict):
        return None

    title = str(article.get("title") or "").strip()
    article_url = str(article.get("link") or "").strip()
    canonical_url = normalized_article_url(article_url)

    if not title or not canonical_url:
        return None

    provider_id = str(article.get("article_id") or "").strip()

    # An absent provider ID is explicitly represented by a
    # deterministic URL-derived identifier, not a fictional source ID.
    if not provider_id:
        provider_id = "url_sha256:" + hashlib.sha256(
            canonical_url.encode("utf-8")
        ).hexdigest()

    return {
        "provider_article_id": provider_id,
        "title": title,
        "article_url": article_url,
        "canonical_url": canonical_url,
        "description": article.get("description"),
        "source_name": article.get("source_name"),
        "published_at": parse_published_at(
            article.get("pubDate")
        ),
        "provider_id_supplied": bool(article.get("article_id")),
    }


class NewsDataClient:
    def __init__(self, max_attempts=3):
        self.settings = get_settings()
        self.max_attempts = max_attempts

        self.headers = {
            "X-ACCESS-KEY": (
                self.settings.newsdata_api_key.get_secret_value()
            ),
            "Accept": "application/json",
        }

    async def get_page(self, client, params):
        for attempt in range(self.max_attempts):
            try:
                response = await client.get(
                    NEWS_ENDPOINT,
                    params=params,
                    headers=self.headers,
                    follow_redirects=False,
                )
            except httpx.RequestError as exc:
                if attempt == self.max_attempts - 1:
                    raise NewsIngestionError(
                        "NewsData.io network request failed."
                    ) from exc

                await asyncio.sleep(
                    min(2 ** attempt + random.random(), 10)
                )
                continue

            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise NewsIngestionError(
                        "NewsData.io returned invalid JSON."
                    ) from exc

                if (
                    not isinstance(payload, dict)
                    or payload.get("status") != "success"
                    or not isinstance(payload.get("results"), list)
                ):
                    raise NewsIngestionError(
                        "NewsData.io returned an unsuccessful "
                        "or invalid response."
                    )

                return payload

            if response.status_code not in RETRYABLE_HTTP:
                raise NewsIngestionError(
                    f"NewsData.io returned HTTP {response.status_code}."
                )

            if attempt == self.max_attempts - 1:
                raise NewsIngestionError(
                    f"NewsData.io returned HTTP {response.status_code} "
                    "after bounded retries."
                )

            retry_after = parse_retry_after(
                response.headers.get("Retry-After")
            )

            if retry_after is not None:
                if retry_after > 120:
                    raise NewsIngestionError(
                        "Provider Retry-After exceeds the retry window."
                    )
                delay = retry_after
            else:
                delay = min(2 ** attempt + random.random(), 10)

            await asyncio.sleep(delay)

        raise NewsIngestionError("NewsData.io attempts exhausted.")


NEWSDATA_QUERY_MAX_CHARS = 100  # NewsData.io rejects longer q / qInTitle values (HTTP 422)


def news_query_terms(identity, search_terms=None, limit=5, max_chars=NEWSDATA_QUERY_MAX_CHARS):
    """
    Quoted headline-search terms for NewsData.io: the company's distinctive
    names first, then its ambiguous one-word names (headline matches are
    re-checked by the relevance rules). A longer form that contains a kept
    shorter term is redundant for a phrase search ("Infosys Ltd" is matched by
    "Infosys") and is dropped; the joined query stays within NewsData.io's
    100-character limit.
    """
    import re as _re

    candidates = []
    for term in list(search_terms or []) + [n.text for n in identity.names if not n.ambiguous] + [
            n.text for n in identity.names if n.ambiguous and not n.acronym]:
        cleaned = str(term or "").strip().strip('"')
        if cleaned and cleaned.lower() not in {t.lower() for t in candidates}:
            candidates.append(cleaned)
    if not candidates:
        candidates = [identity.display_name]

    # Shorter forms first, so longer forms that contain them are recognised as redundant.
    ordered = sorted(candidates, key=lambda t: (len(t), candidates.index(t)))
    kept = []
    for term in ordered:
        if any(_re.search(rf"(?<![A-Za-z0-9]){_re.escape(k)}(?![A-Za-z0-9])", term, _re.IGNORECASE) for k in kept):
            continue
        kept.append(term)
    kept.sort(key=candidates.index)  # restore priority order

    terms = []
    for term in kept[:limit]:
        query = " OR ".join(f'"{t}"' for t in terms + [term])
        if len(query) <= max_chars:
            terms.append(term)
    return terms or [kept[0][: max_chars - 2]]


async def refresh_news_company(
    ticker,
    max_articles=30,
    max_pages=10,
    force_refresh=False,
    search_terms=None,
    country=None,
):
    """
    search_terms / country are optional and used for companies outside the
    original SEC set (for example NSE-listed companies): the query becomes
    the quoted terms, and relevance accepts a match on any of them.
    """
    ticker = str(ticker).upper().strip()
    search_terms = [
        str(t).strip() for t in (search_terms or []) if str(t or "").strip()
    ][:3]


    if not (1 <= max_articles <= 200):
        raise ValueError("max_articles must be between 1 and 200.")

    if not (1 <= max_pages <= 20):
        raise ValueError("max_pages must be between 1 and 20.")

    settings = get_settings()

    with connect() as conn:
        company = conn.execute(
            """
            SELECT ticker, name
            FROM companies
            WHERE ticker = ?
            """,
            (ticker,),
        ).fetchone()

        if company is None:
            raise NewsIngestionError(
                "Company has not been registered from SEC data. "
                "Refresh SEC first."
            )

        if not force_refresh:
            recent = conn.execute(
                """
                SELECT completed_at
                FROM source_refreshes
                WHERE company_ticker = ?
                  AND source = 'NEWSDATA'
                  AND status = 'success'
                ORDER BY id DESC LIMIT 1
                """,
                (ticker,),
            ).fetchone()

            if recent and recent["completed_at"]:
                refreshed_at = datetime.fromisoformat(
                    recent["completed_at"]
                )

                age = (
                    datetime.now(timezone.utc) - refreshed_at
                ).total_seconds()

                existing = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM news_articles
                    WHERE company_ticker = ?
                    """,
                    (ticker,),
                ).fetchone()[0]

                if (
                    existing > 0
                    and 0 <= age < settings.news_cache_hours * 3600
                ):
                    return {
                        "ticker": ticker,
                        "source": "NEWSDATA",
                        "status": "cached",
                        "records_fetched": 0,
                        "records_inserted": 0,
                        "existing_records": existing,
                        "last_refreshed_at": recent["completed_at"],
                        "coverage": "previous_refresh_reused",
                    }

    # Verified company identity: drives both the provider query (headline
    # search on the company's names) and the strict relevance decision.
    from app.services.company_identity import identity_for, universe_identities
    from app.services.news_relevance import assess_article

    identity = identity_for(ticker)
    others = universe_identities()
    query_terms = news_query_terms(identity, search_terms)

    refresh_started = utc_now()

    with transaction() as conn:
        cursor = conn.execute(
            """
            INSERT INTO source_refreshes (
                company_ticker, source, started_at, status
            ) VALUES (?, 'NEWSDATA', ?, 'running')
            RETURNING id
            """,
            (ticker, refresh_started),
        )
        refresh_id = cursor.fetchone()[0]

    pages_fetched = 0
    raw_fetched = 0
    rejected = 0
    irrelevant = 0
    rejected_reasons = {}
    duplicate_in_batch = 0
    accepted = []
    seen_ids = set()
    seen_urls = set()
    next_page = None
    end_reason = "unknown"

    try:
        provider = NewsDataClient()

        params = {
            # Headline search: the strict relevance rules require the company
            # to be named in the headline, so body-only matches are not fetched.
            "qInTitle": " OR ".join(f'"{term}"' for term in query_terms),
            "language": "en",
        }

        if country:
            params["country"] = str(country).lower()

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                settings.request_timeout_seconds,
                connect=15.0,
            ),
            trust_env=True,
        ) as http_client:

            for page_number in range(max_pages):
                page_params = dict(params)

                if next_page:
                    page_params["page"] = next_page

                payload = await provider.get_page(
                    http_client,
                    page_params,
                )

                pages_fetched += 1
                articles = payload["results"]
                raw_fetched += len(articles)

                for article in articles:
                    valid = validate_article(article)

                    if valid is None:
                        rejected += 1
                        continue

                    provider_id = valid["provider_article_id"]
                    canonical_url = valid["canonical_url"]

                    if (
                        provider_id in seen_ids
                        or canonical_url in seen_urls
                    ):
                        duplicate_in_batch += 1
                        continue

                    seen_ids.add(provider_id)
                    seen_urls.add(canonical_url)

                    verdict = assess_article(
                        {
                            "title": valid["title"],
                            "description": valid["description"],
                            "published_at": valid["published_at"],
                        },
                        identity,
                        others=others,
                        max_age_days=settings.news_max_age_days,
                    )

                    if verdict["decision"] != "accept":
                        irrelevant += 1
                        rejected_reasons[verdict["reason"]] = (
                            rejected_reasons.get(verdict["reason"], 0) + 1
                        )
                        continue

                    valid["relevance_score"] = verdict["score"]
                    valid["relevance_reason"] = verdict["reason"]
                    valid["metadata"] = {
                        "relevance_version": verdict["relevance_version"],
                        "event_type": verdict["event_type"],
                        "primary_subject": verdict["primary_subject"],
                        "provider_id_supplied": valid[
                            "provider_id_supplied"
                        ],
                        "canonical_url": canonical_url,
                        "language": article.get("language"),
                        "category": article.get("category"),
                        "duplicate_flag": article.get("duplicate"),
                        "source_id": article.get("source_id"),
                        "keywords": article.get("keywords"),
                        "pubDateTZ": article.get("pubDateTZ"),
                    }

                    accepted.append(valid)

                    if len(accepted) >= max_articles:
                        end_reason = "article_limit_reached"
                        break

                if len(accepted) >= max_articles:
                    break

                new_page = payload.get("nextPage")

                if not new_page:
                    end_reason = "provider_has_no_more_pages"
                    break

                if new_page == next_page:
                    raise NewsIngestionError(
                        "Provider returned a repeated pagination cursor."
                    )

                next_page = str(new_page)

            else:
                end_reason = "page_limit_reached"

        # SQL insertion is transactional; no partially inserted batch.
        with transaction() as conn:
            inserted = 0
            already_stored = 0

            for item in accepted:
                existing = conn.execute(
                    f"""
                    SELECT id
                    FROM news_articles
                    WHERE company_ticker = ?
                      AND (
                        provider_article_id = ?
                        OR article_url = ?
                        OR {json_text(conn, "raw_metadata_json", "canonical_url")} = ?
                      )
                    LIMIT 1
                    """,
                    (
                        ticker,
                        item["provider_article_id"],
                        item["article_url"],
                        item["canonical_url"],
                    ),
                ).fetchone()

                if existing:
                    already_stored += 1
                    continue

                cursor = conn.execute(
                    """
                    INSERT INTO news_articles (
                        company_ticker,
                        provider,
                        provider_article_id,
                        title,
                        description,
                        article_url,
                        source_name,
                        published_at,
                        retrieved_at,
                        relevance_score,
                        relevance_reason,
                        raw_metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        ticker,
                        "NEWSDATA",
                        item["provider_article_id"],
                        item["title"],
                        item["description"],
                        item["article_url"],
                        item["source_name"],
                        item["published_at"],
                        utc_now(),
                        item["relevance_score"],
                        item["relevance_reason"],
                        json.dumps(
                            item["metadata"],
                            ensure_ascii=False,
                        ),
                    ),
                )

                if cursor.rowcount == 1:
                    inserted += 1

            conn.execute(
                """
                UPDATE source_refreshes
                SET status = 'success',
                    completed_at = ?,
                    records_fetched = ?,
                    records_inserted = ?
                WHERE id = ?
                """,
                (
                    utc_now(),
                    raw_fetched,
                    inserted,
                    refresh_id,
                ),
            )

        snapshot = sync_snapshot_to_drive(
            create_timestamped_backup=True
        )

        # BUG B1: make refreshed news part of a verified recovery point.
        recovery_point = create_recovery_point("news_refresh:" + ticker)

        return {
            "ticker": ticker,
            "source": "NEWSDATA",
            "status": "success",
            "provider_pages_fetched": pages_fetched,
            "records_fetched": raw_fetched,
            "records_relevant": len(accepted),
            "records_inserted": inserted,
            "records_already_stored": already_stored,
            "records_rejected": rejected,
            "records_irrelevant": irrelevant,
            "irrelevant_by_reason": rejected_reasons,
            "duplicates_in_batch": duplicate_in_batch,
            "coverage": end_reason,
            "snapshot_verified": snapshot["integrity_ok"],
            "recovery_point_verified": recovery_point["verified"],
        }

    except Exception as exc:
        with transaction() as conn:
            conn.execute(
                """
                UPDATE source_refreshes
                SET status = 'failed',
                    completed_at = ?,
                    records_fetched = ?,
                    error_category = ?,
                    error_message = ?
                WHERE id = ?
                """,
                (
                    utc_now(),
                    raw_fetched,
                    type(exc).__name__,
                    str(exc)[:500],
                    refresh_id,
                ),
            )

        sync_snapshot_to_drive()
        raise
