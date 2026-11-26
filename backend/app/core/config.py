from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="INSIGHTIQ_",
        extra="ignore",
        case_sensitive=False,
    )

    project_root: Path = str(__import__("pathlib").Path(__file__).resolve().parents[3])

    # ---- LLM (all AI research writing) ------------------------------
    # Served by Azure OpenAI. LLM_MODEL is the one setting to change to switch
    # models: on Azure it is the deployment name chosen in Microsoft Foundry.
    # AZURE_OPENAI_DEPLOYMENT is accepted as its older name; LLM_MODEL wins
    # when both are set. The model actually serving each request is read from
    # the response and recorded.
    azure_openai_api_key: SecretStr = Field(
        validation_alias="AZURE_OPENAI_API_KEY"
    )
    azure_openai_endpoint: str = Field(
        validation_alias="AZURE_OPENAI_ENDPOINT"
    )
    llm_model: str = Field(
        validation_alias=AliasChoices("LLM_MODEL", "AZURE_OPENAI_DEPLOYMENT")
    )
    # auto: Chat Completions, switching to the Responses API (background mode)
    # for models that only support it. chat / responses force one API.
    llm_api: str = Field(default="auto", validation_alias=AliasChoices("LLM_API", "INSIGHTIQ_LLM_API"))
    # Sampling temperature; dropped automatically for models that reject it.
    llm_temperature: float | None = Field(
        default=0.2, ge=0, le=2,
        validation_alias=AliasChoices("LLM_TEMPERATURE", "INSIGHTIQ_LLM_TEMPERATURE"),
    )
    # Optional cap on every call's output tokens. Blank: the per-call caps
    # below, lowered automatically to the model's limit when it reports one.
    llm_max_output_tokens: int | None = Field(
        default=None, ge=256, le=200000,
        validation_alias=AliasChoices("LLM_MAX_OUTPUT_TOKENS", "INSIGHTIQ_LLM_MAX_OUTPUT_TOKENS"),
    )
    # Timeout of each individual HTTP request.
    llm_timeout_seconds: float = Field(
        default=180.0, gt=0, le=900,
        validation_alias=AliasChoices(
            "LLM_TIMEOUT_SECONDS", "INSIGHTIQ_LLM_TIMEOUT_SECONDS", "INSIGHTIQ_AZURE_HTTP_TIMEOUT_SECONDS",
        ),
    )
    # Retries (exponential backoff) for HTTP 429, 5xx, timeouts and connection errors.
    llm_max_retries: int = Field(
        default=3, ge=0, le=10,
        validation_alias=AliasChoices("LLM_MAX_RETRIES", "INSIGHTIQ_LLM_MAX_RETRIES"),
    )
    # Reasoning effort for reasoning models. Blank: not sent.
    llm_reasoning_effort: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "LLM_REASONING_EFFORT", "INSIGHTIQ_LLM_REASONING_EFFORT", "INSIGHTIQ_AZURE_REASONING_EFFORT",
        ),
    )
    # The model reported for every response must start with this; a response
    # from any other model is rejected, never used silently. Unset, it
    # defaults to LLM_MODEL (set it when the deployment name differs).
    llm_expected_model_prefix: str | None = Field(
        default=None, max_length=64,
        validation_alias=AliasChoices(
            "LLM_EXPECTED_MODEL_PREFIX", "INSIGHTIQ_LLM_EXPECTED_MODEL_PREFIX",
            "INSIGHTIQ_AZURE_EXPECTED_MODEL_PREFIX",
        ),
    )

    # ---- Groq (optional fallback) -----------------------------------
    # Used only when Azure OpenAI fails because its key is missing or rejected
    # or the deployment is not found. Unset: no fallback. GROQ_MODEL is
    # required together with the key.
    groq_api_key: SecretStr | None = Field(default=None, validation_alias="GROQ_API_KEY")
    groq_model: str | None = Field(
        default=None, max_length=100,
        validation_alias=AliasChoices("GROQ_MODEL", "INSIGHTIQ_GROQ_MODEL"),
    )
    # Free-tier per-minute token limits are small; larger evidence profiles are skipped.
    groq_input_token_budget: int = Field(default=6000, ge=1000, le=200000)
    groq_max_output_tokens: int = Field(default=8000, ge=500, le=65536)
    groq_http_timeout_seconds: float = Field(default=60.0, gt=0, le=300)

    newsdata_api_key: SecretStr = Field(
        validation_alias="NEWSDATA_API_KEY"
    )

    sec_user_agent: str = Field(
        validation_alias="SEC_USER_AGENT"
    )

    api_access_key: SecretStr = Field(
        validation_alias="API_ACCESS_KEY"
    )

    sec_base_url: str = "https://data.sec.gov"
    newsdata_base_url: str = "https://newsdata.io/api/1"

    request_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        le=120,
    )

    sec_cache_hours: int = Field(
        default=24,
        ge=0,
    )

    news_cache_hours: int = Field(
        default=6,
        ge=0,
    )

    forecast_horizon: int = Field(
        default=4,
        ge=1,
        le=12,
    )

    enable_observability: bool = True

    # ---- Source freshness / refresh policy -------------------------
    # sec_cache_hours and news_cache_hours (above) define when stored
    # SEC facts and NewsData.io articles are considered stale.
    # Start Research refreshes stale sources first unless disabled.
    auto_refresh_on_research: bool = True

    # A 'running' source_refreshes row younger than this is treated
    # as an in-progress refresh by another process.
    refresh_running_stale_minutes: int = Field(
        default=15,
        ge=1,
        le=1440,
    )

    # Forecasts are invalidated by their input fingerprint; the TTL is
    # only a safety net, so it is long by default.
    forecast_cache_ttl_hours: int = Field(
        default=720,
        ge=1,
        le=24 * 366,
    )

    # ---- LLM request limits ----------------------------------------
    # Reasoning tokens count toward the output caps below; they are upper
    # bounds, not targets.

    # Upper bound on the estimated *input* tokens of one request. Evidence
    # packages are trimmed by request profiles to stay below it.
    azure_input_token_budget: int = Field(default=24000, ge=2000, le=400000)

    azure_max_output_tokens_financial: int = Field(default=20000, ge=1000, le=128000)
    azure_max_output_tokens_news_selection: int = Field(default=8000, ge=1000, le=128000)
    azure_max_output_tokens_news_analysis: int = Field(default=16000, ge=1000, le=128000)
    azure_max_output_tokens_strategic: int = Field(default=20000, ge=1000, le=128000)
    azure_max_output_tokens_summary: int = Field(default=16000, ge=1000, le=128000)
    # AI summary plus thorough research (one call; reasoning tokens count too).
    azure_max_output_tokens_research_brief: int = Field(default=32000, ge=1000, le=128000)
    azure_max_output_tokens_sector: int = Field(default=20000, ge=1000, le=128000)

    # One AI call (all of its attempts) stops after this many seconds.
    # Responses API background requests are polled and, on deadline or job
    # cancellation, cancelled at the provider.
    azure_call_deadline_seconds: int = Field(default=900, ge=30, le=3600)
    azure_poll_interval_seconds: float = Field(default=3.0, ge=0.2, le=60)
    azure_max_concurrent_calls: int = Field(default=2, ge=1, le=16)

    # A research job that runs longer than this is stopped and marked failed.
    research_job_deadline_seconds: int = Field(default=3600, ge=300, le=6 * 3600)

    # Company news older than this is not used as company-specific evidence.
    news_max_age_days: int = Field(default=45, ge=1, le=3650)

    # ---- Market data providers (sector comparison, any-company data) ----
    # Optional: features that need a missing key report it clearly.
    finnhub_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="FINNHUB_API_KEY",
    )

    fmp_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="FMP_API_KEY",
    )

    market_quote_cache_hours: int = Field(default=24, ge=0, le=24 * 30)
    market_statement_cache_hours: int = Field(default=24 * 7, ge=0, le=24 * 90)
    finnhub_requests_per_minute: int = Field(default=50, ge=1, le=600)
    fmp_requests_per_minute: int = Field(default=30, ge=1, le=600)
    fmp_daily_request_budget: int = Field(default=240, ge=1, le=100000)
    yfinance_requests_per_minute: int = Field(default=30, ge=1, le=600)
    sector_refresh_max_companies: int = Field(default=8, ge=1, le=50)
    # Sector comparison "Compare selected companies" mode: allowed selection size.
    # The frontend reads these from GET /markets/{m}/sectors; they are not
    # duplicated anywhere else.
    custom_comparison_min_companies: int = Field(default=2, ge=2, le=20)
    custom_comparison_max_companies: int = Field(default=6, ge=2, le=20)

    # Company pages: data older than this is refreshed automatically when a
    # company is opened (strictly greater than the TTL). Hours; fractions allowed.
    # This is also the company-data cache lifetime: a cached company payload
    # expires exactly this long after its last successful fetch.
    data_refresh_ttl_hours: float = Field(
        default=24.0,
        gt=0,
        le=24 * 30,
        validation_alias=AliasChoices(
            "CACHE_TTL_HOURS", "DATA_REFRESH_TTL_HOURS", "INSIGHTIQ_DATA_REFRESH_TTL_HOURS",
        ),
    )
    # Expired company-cache rows are deleted this often (any instance may run it).
    cache_cleanup_interval_minutes: float = Field(
        default=5.0, gt=0, le=24 * 60,
        validation_alias=AliasChoices("CACHE_CLEANUP_INTERVAL_MINUTES", "INSIGHTIQ_CACHE_CLEANUP_INTERVAL_MINUTES"),
    )

    # ---- Database -------------------------------------------------
    # postgres (default): any PostgreSQL-compatible server, configured by
    # DATABASE_URL or the DB_* values. sqlite: the original single-file
    # database, kept for rollback and as the migration source.
    db_backend: str = Field(default="postgres", validation_alias=AliasChoices("DB_BACKEND", "INSIGHTIQ_DB_BACKEND"))
    database_url: SecretStr | None = Field(default=None, validation_alias="DATABASE_URL")
    db_host: str | None = Field(default=None, validation_alias="DB_HOST")
    db_port: int = Field(default=5432, ge=1, le=65535, validation_alias="DB_PORT")
    db_name: str | None = Field(default=None, validation_alias="DB_NAME")
    db_user: str | None = Field(default=None, validation_alias="DB_USER")
    db_password: SecretStr | None = Field(default=None, validation_alias="DB_PASSWORD")
    # disable | allow | prefer | require | verify-ca | verify-full
    db_sslmode: str = Field(default="prefer", validation_alias="DB_SSLMODE")
    db_sslrootcert: str | None = Field(default=None, validation_alias="DB_SSLROOTCERT")
    # Schema holding InsightIQ's tables (tests use one schema per test).
    db_schema: str = Field(default="public", validation_alias="DB_SCHEMA")
    db_pool_min_size: int = Field(default=1, ge=0, le=100, validation_alias="DB_POOL_MIN_SIZE")
    db_pool_max_size: int = Field(default=10, ge=1, le=200, validation_alias="DB_POOL_MAX_SIZE")
    db_pool_timeout_seconds: float = Field(default=30.0, gt=0, le=600, validation_alias="DB_POOL_TIMEOUT_SECONDS")
    db_connect_timeout_seconds: int = Field(default=10, ge=1, le=300, validation_alias="DB_CONNECT_TIMEOUT_SECONDS")
    db_statement_timeout_ms: int = Field(default=60000, ge=0, le=3_600_000, validation_alias="DB_STATEMENT_TIMEOUT_MS")
    # Pooled connections are replaced after this long / closed when idle this long.
    db_pool_max_lifetime_seconds: float = Field(default=1800.0, ge=60, le=86400, validation_alias="DB_POOL_MAX_LIFETIME_SECONDS")
    db_pool_max_idle_seconds: float = Field(default=300.0, ge=10, le=86400, validation_alias="DB_POOL_MAX_IDLE_SECONDS")
    # A remote database must use TLS (sslmode require or stricter) unless this
    # is explicitly set (for example a private network with TLS offload).
    db_allow_insecure_remote: bool = Field(default=False, validation_alias="DB_ALLOW_INSECURE_REMOTE")

    # Minimum gap between two manual (forced) refreshes of the same company.
    force_refresh_cooldown_seconds: int = Field(default=60, ge=0, le=3600)

    @field_validator(
        "llm_temperature", "llm_max_output_tokens", "llm_reasoning_effort",
        "llm_expected_model_prefix", "groq_model", mode="before",
    )
    @classmethod
    def blank_is_unset(cls, value):
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("llm_reasoning_effort")
    @classmethod
    def validate_reasoning_effort(cls, value):
        if value is None:
            return None
        cleaned = value.strip().lower()
        if cleaned not in {"none", "minimal", "low", "medium", "high", "xhigh"}:
            raise ValueError(
                "LLM_REASONING_EFFORT must be blank, none, minimal, low, medium, high or xhigh."
            )
        return cleaned

    @field_validator("db_backend")
    @classmethod
    def validate_db_backend(cls, value):
        cleaned = (value or "postgres").strip().lower()
        if cleaned in {"postgresql", "pg"}:
            cleaned = "postgres"
        if cleaned not in {"postgres", "sqlite"}:
            raise ValueError("DB_BACKEND must be postgres or sqlite.")
        return cleaned

    @field_validator("db_sslmode")
    @classmethod
    def validate_db_sslmode(cls, value):
        cleaned = (value or "prefer").strip().lower()
        if cleaned not in {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}:
            raise ValueError("DB_SSLMODE must be disable, allow, prefer, require, verify-ca or verify-full.")
        return cleaned

    @field_validator("db_schema")
    @classmethod
    def validate_db_schema(cls, value):
        import re

        cleaned = (value or "public").strip()
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", cleaned):
            raise ValueError("DB_SCHEMA must be a lower-case identifier (letters, digits, underscore).")
        return cleaned

    @field_validator("db_host", "db_name", "db_user", "db_sslrootcert", mode="before")
    @classmethod
    def blank_db_value_is_unset(cls, value):
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("llm_api")
    @classmethod
    def validate_llm_api(cls, value):
        cleaned = (value or "auto").strip().lower()
        if cleaned not in {"auto", "chat", "responses"}:
            raise ValueError("LLM_API must be auto, chat or responses.")
        return cleaned

    @field_validator("azure_openai_endpoint")
    @classmethod
    def validate_azure_endpoint(cls, value: str) -> str:
        from app.services.ai_endpoint import normalise_endpoint

        return normalise_endpoint(value)

    @field_validator("llm_model")
    @classmethod
    def validate_llm_model(cls, value: str) -> str:
        import re

        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("LLM_MODEL is not set. Set LLM_MODEL in .env (on Azure: the deployment name).")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}", cleaned):
            raise ValueError("LLM_MODEL must be a model or Azure deployment name (letters, digits, . _ - : /).")
        return cleaned

    @field_validator("sec_user_agent")
    @classmethod
    def validate_sec_identity(cls, value: str) -> str:
        cleaned = value.strip()

        if len(cleaned) < 10 or "@" not in cleaned:
            raise ValueError(
                "SEC_USER_AGENT must contain a genuine "
                "application identity and contact email."
            )

        return cleaned

    @model_validator(mode="after")
    def validate_workspace(self):
        root = self.project_root.resolve()

        if not root.is_dir():
            raise ValueError(
                "Configured project directory does not exist."
            )

        if self.db_backend == "postgres" and not (
            self.database_url and self.database_url.get_secret_value().strip()
        ):
            missing = [name for name, value in (
                ("DB_HOST", self.db_host), ("DB_NAME", self.db_name), ("DB_USER", self.db_user),
            ) if not value]
            if missing:
                raise ValueError(
                    "PostgreSQL is not configured. Set DATABASE_URL, or set in .env: "
                    + ", ".join(missing) + " (and DB_PASSWORD)."
                )
        if self.db_backend == "postgres":
            self._check_database_tls()
        if self.db_pool_min_size > self.db_pool_max_size:
            raise ValueError("DB_POOL_MIN_SIZE must not exceed DB_POOL_MAX_SIZE.")

        if self.groq_api_key and self.groq_api_key.get_secret_value().strip() and not self.groq_model:
            raise ValueError(
                "GROQ_API_KEY is set but GROQ_MODEL is not. Set GROQ_MODEL in .env "
                "(see .env.example) or remove GROQ_API_KEY."
            )

        if self.custom_comparison_min_companies > self.custom_comparison_max_companies:
            raise ValueError(
                "INSIGHTIQ_CUSTOM_COMPARISON_MIN_COMPANIES must not exceed "
                "INSIGHTIQ_CUSTOM_COMPARISON_MAX_COMPANIES."
            )

        return self


def _database_host_and_sslmode(settings):
    if settings.database_url and settings.database_url.get_secret_value().strip():
        from urllib.parse import parse_qs, urlsplit

        parts = urlsplit(settings.database_url.get_secret_value().strip())
        query = parse_qs(parts.query)
        return (parts.hostname or "localhost", (query.get("sslmode") or [settings.db_sslmode])[0],
                (query.get("sslrootcert") or [settings.db_sslrootcert])[0])
    return settings.db_host or "localhost", settings.db_sslmode, settings.db_sslrootcert


def _check_database_tls(self):
    """Trusted TLS for remote servers; certificate files must exist."""
    host, sslmode, rootcert = _database_host_and_sslmode(self)
    local = host in {"localhost", "127.0.0.1", "::1"} or host.startswith("/")
    if not local and sslmode in {"disable", "allow", "prefer"} and not self.db_allow_insecure_remote:
        raise ValueError(
            "A remote database needs TLS: set DB_SSLMODE=verify-full (recommended) or require "
            "(or DB_ALLOW_INSECURE_REMOTE=true for a private network that terminates TLS elsewhere)."
        )
    if sslmode in {"verify-ca", "verify-full"} and rootcert and rootcert != "system":
        if not Path(rootcert).is_file():
            raise ValueError("DB_SSLROOTCERT does not point to a readable CA certificate file.")


Settings._check_database_tls = _check_database_tls


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
