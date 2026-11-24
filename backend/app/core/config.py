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

    groq_api_key: SecretStr = Field(
        validation_alias="GROQ_API_KEY"
    )

    newsdata_api_key: SecretStr = Field(
        validation_alias="NEWSDATA_API_KEY"
    )

    sec_user_agent: str = Field(
        validation_alias="SEC_USER_AGENT"
    )

    api_access_key: SecretStr = Field(
        validation_alias="API_ACCESS_KEY"
    )

    groq_model: str = Field(
        default="openai/gpt-oss-120b",
        validation_alias="GROQ_MODEL",
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

    # ---- Groq request budgets (research-writing calls) -------------
    # Conservative *estimated* tokens (input + max completion) per
    # request. The estimator over-counts (~3 bytes/token), so 7000
    # stays below an 8,000 tokens-per-minute account limit.
    groq_request_token_budget: int = Field(
        default=7000,
        ge=2000,
        le=131072,
    )

    groq_financial_max_completion: int = Field(
        default=2800,
        ge=500,
        le=32768,
    )

    groq_news_analysis_max_completion: int = Field(
        default=2400,
        ge=500,
        le=32768,
    )

    groq_strategic_max_completion: int = Field(
        default=2800,
        ge=500,
        le=32768,
    )

    # gpt-oss reasoning tokens count against max_completion_tokens.
    # "" leaves the provider default.
    groq_reasoning_effort: str = Field(
        default="low",
    )

    groq_request_timeout_seconds: float = Field(
        default=45.0,
        gt=0,
        le=180,
    )

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

    # Company pages: data older than this is refreshed automatically when a
    # company is opened (strictly greater than the TTL). Hours; fractions allowed.
    data_refresh_ttl_hours: float = Field(
        default=6.0,
        gt=0,
        le=24 * 30,
        validation_alias=AliasChoices("DATA_REFRESH_TTL_HOURS", "INSIGHTIQ_DATA_REFRESH_TTL_HOURS"),
    )

    # Minimum gap between two manual (forced) refreshes of the same company.
    force_refresh_cooldown_seconds: int = Field(default=60, ge=0, le=3600)

    @field_validator("groq_reasoning_effort")
    @classmethod
    def validate_reasoning_effort(cls, value: str) -> str:
        cleaned = (value or "").strip().lower()

        if cleaned not in {"", "low", "medium", "high"}:
            raise ValueError(
                "INSIGHTIQ_GROQ_REASONING_EFFORT must be low, "
                "medium, high or empty."
            )

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

        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
