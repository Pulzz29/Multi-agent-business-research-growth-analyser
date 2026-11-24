import asyncio
import hashlib
import json
import logging
import random
import time

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

import httpx

from app.core.config import get_settings
from app.database.sqlite_manager import (
    connect,
    transaction,
    utc_now,
    sync_snapshot_to_drive,
    create_recovery_point,
)


logger = logging.getLogger(__name__)

SEC_REGISTRY_URL = "https://www.sec.gov/files/company_tickers.json"

# These are SEC US-GAAP taxonomy concepts, not financial values.
FINANCIAL_CONCEPTS = frozenset({
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
    "SalesRevenueServicesNet",
    "NetCashProvidedByUsedInOperatingActivities",
    "OperatingIncomeLoss",
    "NetIncomeLoss",
    "GrossProfit",
    "Assets",
    "Liabilities",
    "StockholdersEquity",
    "CostOfRevenue",
    "CostOfGoodsAndServicesSold",
    "OperatingExpenses",
    "ResearchAndDevelopmentExpense",
    # Per-share values (unit USD/shares), for EPS growth.
    "EarningsPerShareDiluted",
    "EarningsPerShareBasic",
})

# Companies on the validated SEC pipeline (the original six plus every US
# universe company whose SEC filings reconcile); see sec_coverage.py.
from app.services.sec_coverage import SEC_VALIDATED as SUPPORTED_TICKERS  # noqa: E402

PERIODIC_FORMS = frozenset({"10-Q", "10-K", "10-Q/A", "10-K/A"})

TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}


class SecIngestionError(RuntimeError):
    pass


class SecRateLimiter:
    """Conservatively restrict requests across concurrent SEC operations."""

    def __init__(self, requests_per_second=8):
        if requests_per_second <= 0:
            raise ValueError("Request rate must be positive.")

        self.interval = 1.0 / requests_per_second
        self._next_request_at = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next_request_at - now)

            if wait:
                await asyncio.sleep(wait)

            self._next_request_at = (
                max(time.monotonic(), self._next_request_at)
                + self.interval
            )


def retry_after_seconds(header):
    if not header:
        return None

    try:
        return max(0.0, float(header))
    except (ValueError, TypeError):
        pass

    try:
        retry_time = parsedate_to_datetime(header)
        current_time = datetime.now(timezone.utc)
        return max(
            0.0,
            (retry_time - current_time).total_seconds(),
        )
    except (TypeError, ValueError, OverflowError):
        return None


def make_source_key(ticker, taxonomy, concept, unit, observation):
    source_identity = {
        "ticker": ticker,
        "taxonomy": taxonomy,
        "concept": concept,
        "unit": unit,
        "start": observation.get("start"),
        "end": observation.get("end"),
        "value": observation.get("val"),
        "accn": observation.get("accn"),
        "form": observation.get("form"),
        "filed": observation.get("filed"),
        "fy": observation.get("fy"),
        "fp": observation.get("fp"),
        "frame": observation.get("frame"),
    }

    serialized = json.dumps(
        source_identity,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )

    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def validate_financial_observation(observation):
    if not isinstance(observation, dict):
        return False

    if not isinstance(observation.get("end"), str):
        return False

    if not observation.get("accn") or not observation.get("filed"):
        return False

    value = observation.get("val")

    if isinstance(value, bool):
        return False

    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return False

    return decimal_value.is_finite()


class SecClient:
    def __init__(self, requests_per_second=8, max_attempts=4):
        self.settings = get_settings()
        self.max_attempts = max_attempts
        self.limiter = SecRateLimiter(requests_per_second)

        user_agent = self.settings.sec_user_agent.strip()
        if not user_agent or "@" not in user_agent:
            raise SecIngestionError(
                "SEC_USER_AGENT must identify the application and contact."
            )

        self.headers = {
            "User-Agent": user_agent,
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
        }

    async def get_json(self, client, url):
        hostname = urlparse(url).hostname

        if hostname not in {"www.sec.gov", "data.sec.gov"}:
            raise SecIngestionError(
                "SEC request target is not an allowed SEC host."
            )

        for attempt in range(self.max_attempts):
            await self.limiter.acquire()

            try:
                response = await client.get(url, headers=self.headers)

            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == self.max_attempts - 1:
                    raise SecIngestionError(
                        "SEC network request failed after bounded retries."
                    ) from exc

                await asyncio.sleep(
                    min(2 ** attempt + random.random(), 15)
                )
                continue

            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise SecIngestionError(
                        "SEC response was not valid JSON."
                    ) from exc

            if response.status_code not in TRANSIENT_HTTP_CODES:
                raise SecIngestionError(
                    f"SEC returned HTTP {response.status_code}."
                )

            if attempt == self.max_attempts - 1:
                raise SecIngestionError(
                    f"SEC returned HTTP {response.status_code} "
                    "after bounded retries."
                )

            provider_delay = retry_after_seconds(
                response.headers.get("Retry-After")
            )

            if provider_delay is not None:
                # Respect actual SEC instructions, with a bounded retry.
                if provider_delay > 120:
                    raise SecIngestionError(
                        "SEC Retry-After exceeds the configured "
                        "per-request retry window."
                    )
                delay = provider_delay
            else:
                delay = min(2 ** attempt + random.random(), 15)

            await asyncio.sleep(delay)

        raise SecIngestionError("SEC request attempts exhausted.")

    async def fetch_registry(self, client):
        payload = await self.get_json(client, SEC_REGISTRY_URL)

        if not isinstance(payload, dict):
            raise SecIngestionError(
                "Unexpected SEC company registry structure."
            )

        companies = {}

        for item in payload.values():
            if not isinstance(item, dict):
                continue

            ticker = str(item.get("ticker") or "").upper().strip()
            cik = item.get("cik_str")
            name = item.get("title")

            if ticker and cik is not None and name:
                try:
                    cik_int = int(cik)
                except (TypeError, ValueError):
                    continue

                if cik_int > 0:
                    companies[ticker] = {
                        "ticker": ticker,
                        "cik": str(cik_int).zfill(10),
                        "name": str(name).strip(),
                    }

        if not companies:
            raise SecIngestionError(
                "SEC registry did not provide valid company records."
            )

        return companies

    async def fetch_latest_periodic_filing(self, client, cik):
        """
        Latest 10-Q/10-K (or amendment) from the SEC submissions index.

        The submissions document is far smaller than companyfacts, so it is
        used to decide whether a full companyfacts download is needed.
        """
        if not (isinstance(cik, str) and
                len(cik) == 10 and cik.isdigit()):
            raise ValueError("CIK must be a 10-digit SEC identifier.")

        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        payload = await self.get_json(client, url)

        recent = (
            (payload or {}).get("filings", {}).get("recent")
            if isinstance(payload, dict) else None
        )

        if not isinstance(recent, dict):
            raise SecIngestionError(
                "SEC submissions response has no recent filings."
            )

        forms = recent.get("form") or []
        accessions = recent.get("accessionNumber") or []
        filed = recent.get("filingDate") or []
        report_dates = recent.get("reportDate") or []

        latest = None

        for index, form in enumerate(forms):
            if form not in PERIODIC_FORMS:
                continue
            try:
                candidate = {
                    "form": form,
                    "accession": str(accessions[index]),
                    "filing_date": str(filed[index]),
                    "report_date": (
                        str(report_dates[index])
                        if index < len(report_dates) else None
                    ),
                }
            except IndexError:
                continue
            if latest is None or candidate["filing_date"] > latest["filing_date"]:
                latest = candidate

        return latest

    async def fetch_companyfacts(self, client, cik):
        if not (isinstance(cik, str) and
                len(cik) == 10 and cik.isdigit()):
            raise ValueError("CIK must be a 10-digit SEC identifier.")

        url = (
            f"https://data.sec.gov/api/xbrl/companyfacts/"
            f"CIK{cik}.json"
        )

        payload = await self.get_json(client, url)

        if not isinstance(payload, dict):
            raise SecIngestionError(
                "Invalid SEC company facts response."
            )

        if not isinstance(payload.get("facts"), dict):
            raise SecIngestionError(
                "SEC company facts response has no facts object."
            )

        returned_cik = payload.get("cik")

        if returned_cik is not None:
            try:
                if int(returned_cik) != int(cik):
                    raise SecIngestionError(
                        "Company facts CIK does not match request."
                    )
            except (TypeError, ValueError) as exc:
                raise SecIngestionError(
                    "Company facts contains invalid CIK."
                ) from exc

        return payload, url


def normalize_sec_facts(ticker, payload, source_url, retrieved_at):
    rows = []
    rejected = 0

    us_gaap = payload.get("facts", {}).get("us-gaap", {})

    if not isinstance(us_gaap, dict):
        raise SecIngestionError(
            "SEC company facts US-GAAP section is invalid."
        )

    for concept, concept_data in us_gaap.items():
        if concept not in FINANCIAL_CONCEPTS:
            continue

        units = concept_data.get("units", {})

        if not isinstance(units, dict):
            continue

        for unit, observations in units.items():
            if not isinstance(observations, list):
                continue

            for observation in observations:
                if not validate_financial_observation(observation):
                    rejected += 1
                    continue

                source_key = make_source_key(
                    ticker,
                    "us-gaap",
                    concept,
                    unit,
                    observation,
                )

                rows.append((
                    ticker,
                    source_key,
                    "us-gaap",
                    concept,
                    unit,
                    str(observation["val"]),
                    observation.get("start"),
                    observation["end"],
                    observation.get("fy"),
                    observation.get("fp"),
                    observation.get("form"),
                    observation.get("accn"),
                    observation.get("filed"),
                    observation.get("frame"),
                    source_url,
                    retrieved_at,
                ))

    return rows, rejected


def _record_failed_refresh(ticker, started_at, exc):
    """Best-effort failed-attempt row (only for a registered company)."""
    try:
        with transaction() as conn:
            known = conn.execute(
                "SELECT 1 FROM companies WHERE ticker = ?", (ticker,)
            ).fetchone()
            if known:
                conn.execute(
                    """
                    INSERT INTO source_refreshes(
                        company_ticker, source, started_at,
                        completed_at, status, error_category,
                        error_message
                    )
                    VALUES (?, 'SEC', ?, ?, 'failed', ?, ?)
                    """,
                    (
                        ticker,
                        started_at,
                        utc_now(),
                        type(exc).__name__,
                        str(exc)[:500],
                    ),
                )
    except Exception:
        logger.warning("Could not record failed SEC refresh for %s", ticker)


async def refresh_sec_company(ticker, force_refresh=False):
    ticker = str(ticker).upper().strip()

    from app.services.sec_coverage import sec_eligible

    if not sec_eligible(ticker):
        raise ValueError(
            f"Unsupported company ticker: {ticker}"
        )

    settings = get_settings()
    started_at = utc_now()

    # This source freshness check prevents redundant network calls.
    if not force_refresh:
        with connect() as conn:
            recent = conn.execute(
                """
                SELECT completed_at
                FROM source_refreshes
                WHERE company_ticker = ?
                  AND source = 'SEC'
                  AND status = 'success'
                ORDER BY id DESC
                LIMIT 1
                """,
                (ticker,),
            ).fetchone()

            if recent and recent["completed_at"]:
                refreshed_at = datetime.fromisoformat(
                    recent["completed_at"]
                )

                age_seconds = (
                    datetime.now(timezone.utc) - refreshed_at
                ).total_seconds()

                if (
                    0 <= age_seconds
                    < settings.sec_cache_hours * 3600
                ):
                    count = conn.execute(
                        """
                        SELECT COUNT(*)
                        FROM sec_facts
                        WHERE company_ticker = ?
                        """,
                        (ticker,),
                    ).fetchone()[0]

                    if count > 0:
                        return {
                            "ticker": ticker,
                            "source": "SEC",
                            "status": "cached",
                            "records_fetched": 0,
                            "records_inserted": 0,
                            "existing_records": count,
                            "last_refreshed_at": recent[
                                "completed_at"
                            ],
                        }

    client_logic = SecClient()

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(
            settings.request_timeout_seconds,
            connect=15.0,
        ),
        follow_redirects=False,
    ) as http_client:

        # Reuse the stored, previously verified ticker->CIK mapping
        # instead of downloading the full SEC registry every time.
        # companyfacts still verifies that the returned CIK matches.
        with connect() as conn:
            stored = conn.execute(
                "SELECT ticker, name, cik FROM companies WHERE ticker = ?",
                (ticker,),
            ).fetchone()

        stored_cik = str(stored["cik"] or "") if stored else ""

        if (
            stored
            and stored["name"]
            and len(stored_cik) == 10
            and stored_cik.isdigit()
            and not force_refresh
        ):
            company = {
                "ticker": ticker,
                "cik": stored_cik,
                "name": stored["name"],
            }
        else:
            try:
                registry = await client_logic.fetch_registry(http_client)
            except Exception as exc:
                # Record the failed attempt so freshness reporting is accurate.
                _record_failed_refresh(ticker, started_at, exc)
                raise

            company = registry.get(ticker)

        if not company:
            raise SecIngestionError(
                f"{ticker} was not found in the SEC registry."
            )

        with transaction() as conn:
            conn.execute(
                """
                INSERT INTO companies(
                    ticker, name, cik, created_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    name = excluded.name,
                    cik = excluded.cik
                """,
                (
                    ticker,
                    company["name"],
                    company["cik"],
                    utc_now(),
                ),
            )

            cursor = conn.execute(
                """
                INSERT INTO source_refreshes(
                    company_ticker, source,
                    started_at, status
                )
                VALUES (?, 'SEC', ?, 'running')
                """,
                (ticker, started_at),
            )

            refresh_id = cursor.lastrowid

        try:
            latest_filing = None

            if not force_refresh:
                try:
                    latest_filing = (
                        await client_logic.fetch_latest_periodic_filing(
                            http_client,
                            company["cik"],
                        )
                    )
                except SecIngestionError:
                    # Fall back to the full companyfacts download.
                    latest_filing = None

            if latest_filing is not None:
                with connect() as conn:
                    already_stored = conn.execute(
                        """
                        SELECT 1 FROM sec_facts
                        WHERE company_ticker = ? AND accession = ?
                        LIMIT 1
                        """,
                        (ticker, latest_filing["accession"]),
                    ).fetchone()

                if already_stored:
                    with transaction() as conn:
                        conn.execute(
                            """
                            UPDATE source_refreshes
                            SET status = 'success',
                                completed_at = ?,
                                records_fetched = 0,
                                records_inserted = 0
                            WHERE id = ?
                            """,
                            (utc_now(), refresh_id),
                        )

                    sync_snapshot_to_drive()

                    return {
                        "ticker": ticker,
                        "company_name": company["name"],
                        "cik": company["cik"],
                        "source": "SEC",
                        "status": "no_new_filings",
                        "records_fetched": 0,
                        "records_inserted": 0,
                        "latest_filing": latest_filing,
                        "retrieved_at": utc_now(),
                    }

            payload, source_url = (
                await client_logic.fetch_companyfacts(
                    http_client,
                    company["cik"],
                )
            )

            retrieved_at = utc_now()

            rows, rejected = normalize_sec_facts(
                ticker,
                payload,
                source_url,
                retrieved_at,
            )

            if not rows:
                raise SecIngestionError(
                    "No valid supported US-GAAP financial facts "
                    "were returned by the SEC."
                )

            with transaction() as conn:
                before = conn.total_changes

                conn.executemany(
                    """
                    INSERT OR IGNORE INTO sec_facts(
                        company_ticker,
                        source_key,
                        taxonomy,
                        concept,
                        unit,
                        value_text,
                        period_start,
                        period_end,
                        fiscal_year,
                        fiscal_period,
                        form,
                        accession,
                        filed_at,
                        sec_frame,
                        source_url,
                        retrieved_at
                    )
                    VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    rows,
                )

                inserted = conn.total_changes - before

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
                        len(rows),
                        inserted,
                        refresh_id,
                    ),
                )

            snapshot = sync_snapshot_to_drive(
                create_timestamped_backup=True
            )

            # BUG B1: make ingested SEC facts part of a verified recovery point.
            recovery_point = create_recovery_point("sec_refresh:" + ticker)

            return {
                "ticker": ticker,
                "company_name": company["name"],
                "cik": company["cik"],
                "source": "SEC",
                "status": "success",
                "records_fetched": len(rows),
                "records_inserted": inserted,
                "records_rejected": rejected,
                "source_url": source_url,
                "retrieved_at": retrieved_at,
                "snapshot_verified": snapshot["integrity_ok"],
                "recovery_point_verified": recovery_point["verified"],
                "latest_filing": latest_filing,
            }

        except Exception as exc:
            with transaction() as conn:
                conn.execute(
                    """
                    UPDATE source_refreshes
                    SET status = 'failed',
                        completed_at = ?,
                        error_category = ?,
                        error_message = ?
                    WHERE id = ?
                    """,
                    (
                        utc_now(),
                        type(exc).__name__,
                        str(exc)[:500],
                        refresh_id,
                    ),
                )

            sync_snapshot_to_drive()
            raise
