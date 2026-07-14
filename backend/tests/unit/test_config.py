import pytest
from pydantic import ValidationError


def test_settings_rejects_missing_model_key(monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None)
