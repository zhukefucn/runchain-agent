import pytest
from pydantic import ValidationError


def test_test_settings_allow_missing_model_key(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret")
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    from app.config import Settings

    settings = Settings(_env_file=None, app_env="test")

    assert settings.model_api_key.get_secret_value() == ""


def test_real_settings_reject_missing_model_key(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret")
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    from app.config import Settings

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None, app_env="real")

    assert [error["loc"] for error in exc_info.value.errors()] == [("model_api_key",)]


def test_development_alias_also_requires_real_model_key(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret")
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="development")


@pytest.mark.parametrize(
    "url",
    ["http://api.example.test/v1", "https://user:pass@example.test/v1"],
)
def test_settings_reject_unsafe_real_model_urls(url):
    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            app_env="real",
            jwt_secret_key="jwt-secret",
            model_api_key="model-secret",
            model_base_url=url,
        )
