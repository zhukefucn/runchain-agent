import pytest
from pydantic import ValidationError


def test_settings_rejects_missing_model_key(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret")
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    from app.config import Settings

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)

    assert [error["loc"] for error in exc_info.value.errors()] == [("model_api_key",)]
