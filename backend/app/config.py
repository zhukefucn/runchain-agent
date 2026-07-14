from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["development", "test", "real"] = "test"
    database_url: str = "sqlite+aiosqlite:///./data/demo.db"
    workspace_root: Path = Path("workspace")
    skill_root: Path = Path("skills")
    runner_root: Path = Path("runner")
    mcp_root: Path = Path("backend/app/mcp")
    frontend_dist: Path = Path("frontend/dist")
    jwt_secret_key: SecretStr
    model_name: str = "step-3.7-flash"
    model_base_url: AnyHttpUrl = "https://api.stepfun.com/step_plan/v1"
    model_api_key: SecretStr = SecretStr("")

    @field_validator("model_api_key")
    @classmethod
    def require_real_model_key(cls, value: SecretStr, info) -> SecretStr:
        if info.data.get("app_env") != "test" and not value.get_secret_value():
            raise ValueError("MODEL_API_KEY is required in real mode")
        return value

    @model_validator(mode="after")
    def validate_real_model_configuration(self) -> "Settings":
        if self.app_env == "test":
            return self
        url = self.model_base_url
        if url.username is not None or url.password is not None:
            raise ValueError("MODEL_BASE_URL must not contain user information")
        if url.scheme != "https" and url.host not in {"localhost", "127.0.0.1"}:
            raise ValueError("MODEL_BASE_URL must use HTTPS")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
