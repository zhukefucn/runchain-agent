from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["development", "test"] = "development"
    database_url: str = "sqlite+aiosqlite:///./data/demo.db"
    workspace_root: Path = Path("workspace")
    jwt_secret_key: SecretStr
    model_name: str = "step-3.7-flash"
    model_base_url: AnyHttpUrl = "https://api.stepfun.com/step_plan/v1"
    model_api_key: SecretStr


@lru_cache
def get_settings() -> Settings:
    return Settings()
