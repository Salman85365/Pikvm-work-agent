from __future__ import annotations

import pytest

from work_agent.pikvm import PiKVMConfigurationError, PiKVMSettings


def test_settings_normalize_url_and_hide_password() -> None:
    settings = PiKVMSettings(
        base_url="https://pikvm.example/",
        username="admin",
        password="super-secret",
    )

    assert settings.base_url == "https://pikvm.example"
    assert "super-secret" not in repr(settings)


def test_settings_reject_credentials_in_url() -> None:
    with pytest.raises(PiKVMConfigurationError, match="Do not place credentials"):
        PiKVMSettings(
            base_url="https://admin:secret@pikvm.example",
            username="admin",
            password="secret",
        )


def test_from_env_reports_all_missing_required_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("work_agent.pikvm.config.load_dotenv", lambda: False)
    for name in ("PIKVM_URL", "PIKVM_USERNAME", "PIKVM_PASSWORD"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(PiKVMConfigurationError) as error:
        PiKVMSettings.from_env()

    assert "PIKVM_URL" in str(error.value)
    assert "PIKVM_USERNAME" in str(error.value)
    assert "PIKVM_PASSWORD" in str(error.value)


def test_from_env_parses_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("work_agent.pikvm.config.load_dotenv", lambda: False)
    monkeypatch.setenv("PIKVM_URL", "http://pikvm.local")
    monkeypatch.setenv("PIKVM_USERNAME", "operator")
    monkeypatch.setenv("PIKVM_PASSWORD", "secret")
    monkeypatch.setenv("PIKVM_VERIFY_SSL", "no")
    monkeypatch.setenv("PIKVM_MAX_RETRIES", "4")

    settings = PiKVMSettings.from_env()

    assert settings.verify_ssl is False
    assert settings.max_retries == 4
    assert settings.totp_required is True


def test_from_env_can_disable_totp_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("work_agent.pikvm.config.load_dotenv", lambda: False)
    monkeypatch.setenv("PIKVM_URL", "http://pikvm.local")
    monkeypatch.setenv("PIKVM_USERNAME", "operator")
    monkeypatch.setenv("PIKVM_PASSWORD", "secret")
    monkeypatch.setenv("PIKVM_TOTP_REQUIRED", "false")

    assert PiKVMSettings.from_env().totp_required is False
