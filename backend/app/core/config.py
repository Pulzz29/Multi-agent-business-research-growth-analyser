from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator, model_validator
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
