"""
Additive SQLite storage for multi-market company data.

Tables are created with CREATE TABLE IF NOT EXISTS; existing tables are
never altered. Provider values are stored as exact text with their
currency, provider name, source field and retrieval time, so every number
shown in a comparison can be traced to a provider record.
"""

import json
import threading
from datetime import datetime, timezone

from app.database.sqlite_manager import connect, transaction


_INIT_LOCK = threading.Lock()
_INITIALISED = set()

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS market_companies (
        symbol TEXT PRIMARY KEY,
        market TEXT NOT NULL CHECK (market IN ('US', 'IN')),
        name TEXT NOT NULL,
        exchange TEXT,
        currency TEXT,
        sector_key TEXT,
        provider_sector TEXT,
        provider_industry TEXT,
        cik TEXT,
        in_universe INTEGER NOT NULL DEFAULT 0,
        profile_provider TEXT,
        profile_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (symbol) REFERENCES companies(ticker)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_market_companies_market_sector ON market_companies(market, sector_key)",
    """
    CREATE TABLE IF NOT EXISTS market_quotes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        provider TEXT NOT NULL,
        currency TEXT,
        price TEXT,
        market_cap TEXT,
        pe_ratio TEXT,
        pb_ratio TEXT,
        dividend_yield_pct TEXT,
        beta TEXT,
        source_fields_json TEXT,
        as_of TEXT,
        fetched_at TEXT NOT NULL,
        FOREIGN KEY (symbol) REFERENCES companies(ticker)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_market_quotes_symbol ON market_quotes(symbol, fetched_at)",
    """
    CREATE TABLE IF NOT EXISTS market_statements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        provider TEXT NOT NULL,
        statement TEXT NOT NULL,
        period_type TEXT NOT NULL CHECK (period_type IN ('annual', 'quarterly')),
        period_end TEXT NOT NULL,
        fiscal_year TEXT,
        metric TEXT NOT NULL,
        value_text TEXT NOT NULL,
        currency TEXT,
        source_field TEXT,
        fetched_at TEXT NOT NULL,
        UNIQUE (symbol, provider, period_type, period_end, metric, value_text),
        FOREIGN KEY (symbol) REFERENCES companies(ticker)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_market_statements_symbol ON market_statements(symbol, period_type, period_end)",
    """
    CREATE TABLE IF NOT EXISTS provider_usage (
        provider TEXT NOT NULL,
        usage_day TEXT NOT NULL,
        requests INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (provider, usage_day)
    )
    """,
)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def ensure_market_storage():
    """Create the market tables once per database file (idempotent)."""
    from app.database.sqlite_manager import working_database_path

    path = working_database_path()
    key = str(path)
    if key in _INITIALISED:
        return
    if not path.is_file():
        # Never create a bare database here: startup recovery restores the
        # runtime database only when the file is missing.
        raise RuntimeError("The runtime database is not initialised; start the backend first.")
    with _INIT_LOCK:
        if key in _INITIALISED:
            return
        with transaction() as conn:
            for statement in SCHEMA:
                conn.execute(statement)
            # Additive migration (idempotent): company-level refresh time, UTC.
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(market_companies)")}
            if "last_refreshed_at" not in columns:
                conn.execute("ALTER TABLE market_companies ADD COLUMN last_refreshed_at TEXT")
        _INITIALISED.add(key)


def set_last_refreshed_at(symbol, timestamp_utc_iso):
    """Record a successful company-level refresh (UTC ISO-8601)."""
    ensure_market_storage()
    with transaction() as conn:
        conn.execute(
            "UPDATE market_companies SET last_refreshed_at = ? WHERE symbol = ?",
            (timestamp_utc_iso, symbol),
        )


# ------------------------------------------------------------- companies

def register_company(*, symbol, market, name, exchange=None, currency=None, sector_key=None,
                     provider_sector=None, provider_industry=None, cik=None,
                     in_universe=False, profile_provider=None, profile=None):
    """Insert or update a company. Never changes existing `companies` rows."""
    ensure_market_storage()
    now = utc_now()
    with transaction() as conn:
        # `companies.cik` is NOT NULL; non-SEC companies store '' (documented).
        conn.execute(
            "INSERT OR IGNORE INTO companies(ticker, name, cik, created_at) VALUES (?, ?, ?, ?)",
            (symbol, name, cik or "", now),
        )
        existing = conn.execute(
            "SELECT * FROM market_companies WHERE symbol = ?", (symbol,)
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO market_companies(
                    symbol, market, name, exchange, currency, sector_key,
                    provider_sector, provider_industry, cik, in_universe,
                    profile_provider, profile_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (symbol, market, name, exchange, currency, sector_key, provider_sector,
                 provider_industry, cik, int(bool(in_universe)), profile_provider,
                 json.dumps(profile, default=str) if profile else None, now, now),
            )
        else:
            conn.execute(
                """
                UPDATE market_companies SET
                    name = COALESCE(?, name),
                    exchange = COALESCE(?, exchange),
                    currency = COALESCE(?, currency),
                    sector_key = COALESCE(sector_key, ?),
                    provider_sector = COALESCE(?, provider_sector),
                    provider_industry = COALESCE(?, provider_industry),
                    cik = COALESCE(?, cik),
                    in_universe = MAX(in_universe, ?),
                    profile_provider = COALESCE(?, profile_provider),
                    profile_json = COALESCE(?, profile_json),
                    updated_at = ?
                WHERE symbol = ?
                """,
                (name, exchange, currency, sector_key, provider_sector, provider_industry,
                 cik, int(bool(in_universe)), profile_provider,
                 json.dumps(profile, default=str) if profile else None, now, symbol),
            )
    return get_company(symbol)


def get_company(symbol):
    ensure_market_storage()
    with connect() as conn:
        row = conn.execute("SELECT * FROM market_companies WHERE symbol = ?", (symbol,)).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["in_universe"] = bool(item["in_universe"])
    try:
        item["profile"] = json.loads(item.pop("profile_json") or "null")
    except ValueError:
        item["profile"] = None
    return item


def list_companies(market, sector_key=None):
    ensure_market_storage()
    sql = "SELECT symbol FROM market_companies WHERE market = ?"
    params = [market]
    if sector_key:
        sql += " AND sector_key = ?"
        params.append(sector_key)
    with connect() as conn:
        rows = conn.execute(sql + " ORDER BY symbol", params).fetchall()
    return [get_company(r["symbol"]) for r in rows]


# ---------------------------------------------------------------- quotes

QUOTE_FIELDS = ("price", "market_cap", "pe_ratio", "pb_ratio", "dividend_yield_pct", "beta")


def insert_quote(symbol, provider, quote):
    ensure_market_storage()
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO market_quotes(
                symbol, provider, currency, price, market_cap, pe_ratio, pb_ratio,
                dividend_yield_pct, beta, source_fields_json, as_of, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                symbol, provider, quote.get("currency"),
                *(None if quote.get(f) is None else str(quote[f]) for f in QUOTE_FIELDS),
                json.dumps(quote.get("source_fields") or {}),
                quote.get("as_of"), utc_now(),
            ),
        )


def latest_quote(symbol):
    ensure_market_storage()
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM market_quotes WHERE symbol = ? ORDER BY fetched_at DESC, id DESC LIMIT 1",
            (symbol,),
        ).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["source_fields"] = json.loads(item.pop("source_fields_json") or "{}")
    return item


# ------------------------------------------------------------ statements

def insert_statement_rows(rows):
    """rows: dicts with symbol, provider, statement, period_type, period_end,
    fiscal_year, metric, value_text, currency, source_field. Returns inserted count."""
    ensure_market_storage()
    now = utc_now()
    with transaction() as conn:
        before = conn.total_changes
        conn.executemany(
            """
            INSERT OR IGNORE INTO market_statements(
                symbol, provider, statement, period_type, period_end, fiscal_year,
                metric, value_text, currency, source_field, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (r["symbol"], r["provider"], r["statement"], r["period_type"], r["period_end"],
                 r.get("fiscal_year"), r["metric"], r["value_text"], r.get("currency"),
                 r.get("source_field"), now)
                for r in rows
            ],
        )
        return conn.total_changes - before


def statement_values(symbol, period_type="annual"):
    """Latest stored value per (period_end, metric): {period_end: {metric: row}}."""
    ensure_market_storage()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM market_statements
            WHERE symbol = ? AND period_type = ?
            ORDER BY period_end, fetched_at, id
            """,
            (symbol, period_type),
        ).fetchall()
    out = {}
    for row in rows:
        out.setdefault(row["period_end"], {})[row["metric"]] = dict(row)
    return out


# --------------------------------------------------------- provider usage

def record_provider_request(provider):
    ensure_market_storage()
    day = datetime.now(timezone.utc).date().isoformat()
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO provider_usage(provider, usage_day, requests) VALUES (?, ?, 1)
            ON CONFLICT(provider, usage_day) DO UPDATE SET requests = requests + 1
            """,
            (provider, day),
        )


def provider_requests_today(provider):
    ensure_market_storage()
    day = datetime.now(timezone.utc).date().isoformat()
    with connect() as conn:
        row = conn.execute(
            "SELECT requests FROM provider_usage WHERE provider = ? AND usage_day = ?",
            (provider, day),
        ).fetchone()
    return int(row["requests"]) if row else 0


# ------------------------------------------------------- refresh logging

def start_refresh(symbol, source):
    with transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO source_refreshes(company_ticker, source, started_at, status) "
            "VALUES (?, ?, ?, 'running')",
            (symbol, source, utc_now()),
        )
        return cursor.lastrowid


def finish_refresh(refresh_id, ok, fetched=0, inserted=0, error=None):
    with transaction() as conn:
        if ok:
            conn.execute(
                "UPDATE source_refreshes SET status='success', completed_at=?, "
                "records_fetched=?, records_inserted=? WHERE id=?",
                (utc_now(), fetched, inserted, refresh_id),
            )
        else:
            conn.execute(
                "UPDATE source_refreshes SET status='failed', completed_at=?, "
                "records_fetched=?, error_category=?, error_message=? WHERE id=?",
                (utc_now(), fetched, type(error).__name__ if error else "Error",
                 # Provider exceptions are never stored verbatim (may echo URLs).
                 getattr(error, "safe_message", None) or "Provider request failed.",
                 refresh_id),
            )


def last_success(symbol, source):
    with connect() as conn:
        row = conn.execute(
            "SELECT completed_at FROM source_refreshes WHERE company_ticker=? AND source=? "
            "AND status='success' ORDER BY id DESC LIMIT 1",
            (symbol, source),
        ).fetchone()
    return row["completed_at"] if row else None
