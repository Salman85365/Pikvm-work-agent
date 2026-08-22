from __future__ import annotations

import pytest

from work_agent.pikvm import PiKVMConfigurationError, PiKVMSettings, TotpProviderKind, WorkIdentity


@pytest.fixture(autouse=True)
def _isolate_profile_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PIKVM_PROFILE", raising=False)
    monkeypatch.delenv("PIKVM_PROFILES", raising=False)


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
    monkeypatch.setenv("PIKVM_MOUSE_MOVE_SETTLE_SECONDS", "0.2")

    settings = PiKVMSettings.from_env()

    assert settings.verify_ssl is False
    assert settings.max_retries == 4
    assert settings.mouse_move_settle_seconds == 0.2
    assert settings.totp_required is True
    assert settings.totp_provider is TotpProviderKind.KEYCHAIN
    assert settings.totp_keychain_service == "pikvm-work-agent.totp"
    assert settings.totp_interactive_fallback is False


@pytest.mark.parametrize("value", [-0.1, 2.1])
def test_mouse_move_settle_delay_is_bounded(value: float) -> None:
    with pytest.raises(PiKVMConfigurationError, match="between 0 and 2"):
        PiKVMSettings(
            base_url="https://pikvm.example",
            username="admin",
            password="secret",
            mouse_move_settle_seconds=value,
        )


def test_from_env_can_disable_totp_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("work_agent.pikvm.config.load_dotenv", lambda: False)
    monkeypatch.setenv("PIKVM_URL", "http://pikvm.local")
    monkeypatch.setenv("PIKVM_USERNAME", "operator")
    monkeypatch.setenv("PIKVM_PASSWORD", "secret")
    monkeypatch.setenv("PIKVM_TOTP_REQUIRED", "false")

    assert PiKVMSettings.from_env().totp_required is False


def test_from_env_parses_explicit_interactive_totp_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("work_agent.pikvm.config.load_dotenv", lambda: False)
    monkeypatch.setenv("PIKVM_URL", "http://pikvm.local")
    monkeypatch.setenv("PIKVM_USERNAME", "operator")
    monkeypatch.setenv("PIKVM_PASSWORD", "secret")
    monkeypatch.setenv("PIKVM_TOTP_PROVIDER", "interactive")
    monkeypatch.setenv("PIKVM_TOTP_KEYCHAIN_SERVICE", "private.totp.service")
    monkeypatch.setenv("PIKVM_TOTP_INTERACTIVE_FALLBACK", "true")
    monkeypatch.setenv("PIKVM_TOTP_SECRET", "must-not-be-loaded")

    settings = PiKVMSettings.from_env()

    assert settings.totp_provider is TotpProviderKind.INTERACTIVE
    assert settings.totp_keychain_service == "private.totp.service"
    assert settings.totp_interactive_fallback is True
    assert not hasattr(settings, "totp_secret")
    assert "must-not-be-loaded" not in repr(settings)


def test_invalid_totp_provider_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("work_agent.pikvm.config.load_dotenv", lambda: False)
    monkeypatch.setenv("PIKVM_URL", "http://pikvm.local")
    monkeypatch.setenv("PIKVM_USERNAME", "operator")
    monkeypatch.setenv("PIKVM_PASSWORD", "secret")
    monkeypatch.setenv("PIKVM_TOTP_PROVIDER", "automatic-magic")

    with pytest.raises(PiKVMConfigurationError, match="keychain or interactive"):
        PiKVMSettings.from_env()


def test_named_profiles_select_independent_hosts_and_mixed_totp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("work_agent.pikvm.config.load_dotenv", lambda: False)
    monkeypatch.setenv("PIKVM_PROFILES", "heidrick,lab-kvm")
    monkeypatch.setenv("PIKVM_PROFILE", "heidrick")
    monkeypatch.setenv("PIKVM_HEIDRICK_URL", "https://heidrick.example/kvm")
    monkeypatch.setenv("PIKVM_HEIDRICK_USERNAME", "work-user")
    monkeypatch.setenv("PIKVM_HEIDRICK_PASSWORD", "work-password")
    monkeypatch.setenv("PIKVM_HEIDRICK_TOTP_REQUIRED", "true")
    monkeypatch.setenv("PIKVM_HEIDRICK_VERIFY_SSL", "false")
    monkeypatch.setenv("PIKVM_LAB_KVM_URL", "http://lab.example")
    monkeypatch.setenv("PIKVM_LAB_KVM_USERNAME", "lab-user")
    monkeypatch.setenv("PIKVM_LAB_KVM_PASSWORD", "lab-password")
    monkeypatch.setenv("PIKVM_LAB_KVM_TOTP_REQUIRED", "false")

    default_settings = PiKVMSettings.from_env()
    lab_settings = PiKVMSettings.from_env("lab-kvm")

    assert default_settings.profile == "heidrick"
    assert default_settings.base_url == "https://heidrick.example/kvm"
    assert default_settings.username == "work-user"
    assert default_settings.totp_required is True
    assert default_settings.verify_ssl is False
    assert "work-password" not in repr(default_settings)
    assert lab_settings.profile == "lab-kvm"
    assert lab_settings.base_url == "http://lab.example"
    assert lab_settings.username == "lab-user"
    assert lab_settings.totp_required is False
    assert lab_settings.verify_ssl is True
    assert "lab-password" not in repr(lab_settings)


def test_single_named_profile_is_selected_without_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("work_agent.pikvm.config.load_dotenv", lambda: False)
    monkeypatch.setenv("PIKVM_PROFILES", "only-kvm")
    monkeypatch.delenv("PIKVM_PROFILE", raising=False)
    monkeypatch.setenv("PIKVM_ONLY_KVM_URL", "https://only.example")
    monkeypatch.setenv("PIKVM_ONLY_KVM_USERNAME", "operator")
    monkeypatch.setenv("PIKVM_ONLY_KVM_PASSWORD", "password")

    settings = PiKVMSettings.from_env()

    assert settings.profile == "only-kvm"


def test_multiple_named_profiles_require_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("work_agent.pikvm.config.load_dotenv", lambda: False)
    monkeypatch.setenv("PIKVM_PROFILES", "heidrick,lab")
    monkeypatch.delenv("PIKVM_PROFILE", raising=False)

    with pytest.raises(PiKVMConfigurationError, match="Set PIKVM_PROFILE or pass --profile"):
        PiKVMSettings.from_env()


def test_unknown_or_colliding_profile_names_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("work_agent.pikvm.config.load_dotenv", lambda: False)
    monkeypatch.setenv("PIKVM_PROFILES", "heidrick,lab")

    with pytest.raises(PiKVMConfigurationError, match="Unknown PiKVM profile"):
        PiKVMSettings.from_env("other")

    monkeypatch.setenv("PIKVM_PROFILES", "work-kvm,work_kvm")
    with pytest.raises(PiKVMConfigurationError, match="same environment-variable prefix"):
        PiKVMSettings.from_env("work-kvm")


def test_named_profile_reports_its_prefixed_missing_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("work_agent.pikvm.config.load_dotenv", lambda: False)
    monkeypatch.setenv("PIKVM_PROFILES", "heidrick")
    monkeypatch.setenv("PIKVM_HEIDRICK_URL", "https://heidrick.example")
    monkeypatch.delenv("PIKVM_HEIDRICK_USERNAME", raising=False)
    monkeypatch.delenv("PIKVM_HEIDRICK_PASSWORD", raising=False)

    with pytest.raises(PiKVMConfigurationError) as error:
        PiKVMSettings.from_env()

    assert "PIKVM_HEIDRICK_USERNAME" in str(error.value)
    assert "PIKVM_HEIDRICK_PASSWORD" in str(error.value)


def test_work_identity_is_profile_scoped_and_aliases_are_normalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("work_agent.pikvm.config.load_dotenv", lambda: False)
    monkeypatch.setenv("PIKVM_PROFILES", "heidrick,lab")
    for profile in ("HEIDRICK", "LAB"):
        monkeypatch.setenv(f"PIKVM_{profile}_URL", f"https://{profile.lower()}.example")
        monkeypatch.setenv(f"PIKVM_{profile}_USERNAME", "operator")
        monkeypatch.setenv(f"PIKVM_{profile}_PASSWORD", "secret")
    monkeypatch.setenv("PIKVM_HEIDRICK_WORK_IDENTITY_NAME", " Shafiq ")
    monkeypatch.setenv(
        "PIKVM_HEIDRICK_WORK_IDENTITY_ALIASES",
        "Shafiq, Shafique,SHAFIQUE",
    )

    heidrick = PiKVMSettings.from_env("heidrick")
    lab = PiKVMSettings.from_env("lab")

    assert heidrick.work_identity == WorkIdentity(
        name="Shafiq",
        aliases=("Shafiq", "Shafique"),
    )
    assert heidrick.work_identity.matches(" shafique ")
    assert lab.work_identity is None


def test_work_identity_never_uses_an_unprefixed_global_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("work_agent.pikvm.config.load_dotenv", lambda: False)
    monkeypatch.setenv("PIKVM_PROFILES", "heidrick")
    monkeypatch.setenv("PIKVM_HEIDRICK_URL", "https://heidrick.example")
    monkeypatch.setenv("PIKVM_HEIDRICK_USERNAME", "operator")
    monkeypatch.setenv("PIKVM_HEIDRICK_PASSWORD", "secret")
    monkeypatch.setenv("WORK_IDENTITY_NAME", "Must not be used")
    monkeypatch.setenv("WORK_IDENTITY_ALIASES", "Also ignored")

    assert PiKVMSettings.from_env("heidrick").work_identity is None


@pytest.mark.parametrize(
    ("name", "aliases", "message"),
    [
        (None, "Shafique", "WORK_IDENTITY_NAME is required"),
        ("Shafiq", "Shafique,,Shafiq", "without empty aliases"),
    ],
)
def test_invalid_profile_work_identity_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    name: str | None,
    aliases: str,
    message: str,
) -> None:
    monkeypatch.setattr("work_agent.pikvm.config.load_dotenv", lambda: False)
    monkeypatch.setenv("PIKVM_PROFILES", "heidrick")
    monkeypatch.setenv("PIKVM_HEIDRICK_URL", "https://heidrick.example")
    monkeypatch.setenv("PIKVM_HEIDRICK_USERNAME", "operator")
    monkeypatch.setenv("PIKVM_HEIDRICK_PASSWORD", "secret")
    if name is not None:
        monkeypatch.setenv("PIKVM_HEIDRICK_WORK_IDENTITY_NAME", name)
    monkeypatch.setenv("PIKVM_HEIDRICK_WORK_IDENTITY_ALIASES", aliases)

    with pytest.raises(PiKVMConfigurationError, match=message):
        PiKVMSettings.from_env("heidrick")


def test_work_identity_rejects_control_characters() -> None:
    with pytest.raises(PiKVMConfigurationError, match="control characters"):
        WorkIdentity(name="Shafiq\nOther")


def test_work_identity_limits_match_the_protected_capture_manifest() -> None:
    with pytest.raises(PiKVMConfigurationError, match="120 characters"):
        WorkIdentity(name="x" * 121)
    with pytest.raises(PiKVMConfigurationError, match="at most 20 names"):
        WorkIdentity(name="Primary", aliases=tuple(f"Alias {index}" for index in range(20)))


def test_pikvm_settings_repr_does_not_expose_the_work_identity() -> None:
    settings = PiKVMSettings(
        base_url="https://pikvm.example",
        username="admin",
        password="secret",
        work_identity=WorkIdentity("Private Work Name"),
    )

    assert "Private Work Name" not in repr(settings)
