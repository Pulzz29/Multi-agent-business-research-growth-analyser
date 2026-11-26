-- 24-hour company-data cache (temporary rows; the permanent company records
-- stay in companies / market_companies and the market_* tables).
--
-- cache_kind 'company': cache_key = '<MARKET>:<SYMBOL>' (normalised upper case)
-- cache_kind 'search' : cache_key = '<MARKET>:<normalised search text>'
--
-- A row is valid only while status = 'ready' AND expires_at > now().
-- expires_at = fetched_at + CACHE_TTL_HOURS, set only by a successful fetch;
-- reading a row never changes it. refresh_lease_until / refresh_owner make
-- one request (in any instance) the fetcher while others wait.

CREATE TABLE company_data_cache (
    cache_kind TEXT NOT NULL CHECK (cache_kind IN ('company', 'search')),
    cache_key TEXT NOT NULL,
    market TEXT NOT NULL,
    symbol TEXT,
    status TEXT NOT NULL CHECK (status IN ('ready', 'pending')),
    payload_json TEXT,
    fetched_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    refresh_lease_until TIMESTAMPTZ,
    refresh_owner TEXT,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (cache_kind, cache_key),
    CHECK (status <> 'ready' OR (payload_json IS NOT NULL AND fetched_at IS NOT NULL AND expires_at > fetched_at))
);
CREATE INDEX ix_company_data_cache_expires ON company_data_cache(expires_at);
CREATE INDEX ix_company_data_cache_symbol ON company_data_cache(market, symbol);
