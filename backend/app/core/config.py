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

    # ---- Azure OpenAI (all AI research writing) -------------------
    # The deployment name is the name chosen in Microsoft Foundry; it is not
    # assumed to equal the model name. The model actually serving each
    # request is read from the response and recorded.
    azure_openai_api_key: SecretStr = Field(
        validation_alias="AZURE_OPENAI_API_KEY"
    )
    azure_openai_endpoint: str = Field(
        validation_alias="AZURE_OPENAI_ENDPOINT"
    )
    azure_openai_deployment: str = Field(
        validation_alias="AZURE_OPENAI_DEPLOYMENT"
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

    # ---- Azure OpenAI request limits -------------------------------
    # Supported efforts depend on the deployed model; Azure rejects an
    # unsupported one with HTTP 400. Reasoning tokens count toward
    # max_output_tokens; the caps below are upper bounds, not targets.
    azure_reasoning_effort: str = Field(default="medium")

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

    # Background responses are polled until they finish or the deadline
    # passes; on deadline (or job cancellation) the response is cancelled.
    azure_call_deadline_seconds: int = Field(default=900, ge=30, le=3600)
    azure_poll_interval_seconds: float = Field(default=3.0, ge=0.2, le=60)
    # Timeout of each individual HTTP request (create / retrieve / cancel).
    azure_http_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    azure_max_concurrent_calls: int = Field(default=2, ge=1, le=16)
    # The model reported by Azure for every response must start with this; a
    # response from any other model is rejected, never used silently. Unset,
    # it defaults to the deployment name (set it when the two differ).
    azure_expected_model_prefix: str | None = Field(default=None, min_length=1, max_length=64)

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
    data_refresh_ttl_hours: float = Field(
        default=6.0,
        gt=0,
        le=24 * 30,
        validation_alias=AliasChoices("DATA_REFRESH_TTL_HOURS", "INSIGHTIQ_DATA_REFRESH_TTL_HOURS"),
    )

    # Minimum gap between two manual (forced) refreshes of the same company.
    force_refresh_cooldown_seconds: int = Field(default=60, ge=0, le=3600)

    @field_validator("azure_reasoning_effort")
    @classmethod
    def validate_reasoning_effort(cls, value: str) -> str:
        cleaned = (value or "").strip().lower()

        if cleaned not in {"minimal", "low", "medium", "high", "xhigh"}:
            raise ValueError(
                "INSIGHTIQ_AZURE_REASONING_EFFORT must be minimal, low, medium, high or xhigh."
            )

        return cleaned

    @field_validator("azure_openai_endpoint")
    @classmethod
    def validate_azure_endpoint(cls, value: str) -> str:
        from app.services.ai_endpoint import normalise_endpoint

        return normalise_endpoint(value)

    @field_validator("azure_openai_deployment")
    @classmethod
    def validate_azure_deployment(cls, value: str) -> str:
        import re

        cleaned = (value or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", cleaned):
            raise ValueError("AZURE_OPENAI_DEPLOYMENT must be the deployment name from Microsoft Foundry.")
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

        if self.custom_comparison_min_companies > self.custom_comparison_max_companies:
            raise ValueError(
                "INSIGHTIQ_CUSTOM_COMPARISON_MIN_COMPANIES must not exceed "
                "INSIGHTIQ_CUSTOM_COMPARISON_MAX_COMPANIES."
            )

        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
