from __future__ import annotations

import keyring.errors
import pyotp
import pytest

from work_agent.pikvm import (
    FallbackTotpProvider,
    InteractiveTotpProvider,
    KeychainTotpProvider,
    KeyringSecretStore,
    PiKVMConfigurationError,
    PiKVMKeychainError,
    PiKVMKeychainMissingError,
    PiKVMSettings,
    PiKVMTotpSecretError,
    TotpProviderKind,
    build_totp_provider,
    endpoint_url,
    generate_totp_code,
    normalize_pikvm_host,
    normalize_totp_seed,
)

_SECRET = "JBSWY3DPEHPK3PXP"


class _Store:
    def __init__(self, entries: dict[tuple[str, str], str] | None = None) -> None:
        self.entries = entries or {}
        self.lookups: list[tuple[str, str]] = []

    def get_secret(self, service: str, account: str) -> str | None:
        self.lookups.append((service, account))
        return self.entries.get((service, account))

    def set_secret(self, service: str, account: str, secret: str) -> None:
        self.entries[(service, account)] = secret

    def delete_secret(self, service: str, account: str) -> bool:
        return self.entries.pop((service, account), None) is not None


def _settings(**overrides: object) -> PiKVMSettings:
    values: dict[str, object] = {
        "base_url": "https://PiKVM.Example/path",
        "username": "operator",
        "password": "password",
    }
    values.update(overrides)
    return PiKVMSettings(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("endpoint", "account"),
    [
        ("https://LUTRON-3.Example.Local/path?ignored=yes#fragment", "lutron-3.example.local"),
        ("http://PiKVM.Example:80/", "pikvm.example"),
        ("https://PiKVM.Example:443/", "pikvm.example"),
        ("https://PiKVM.Example:8443/api", "pikvm.example:8443"),
        ("PIKVM.EXAMPLE:9443/path", "pikvm.example:9443"),
    ],
)
def test_host_normalization_is_exact_and_safe(endpoint: str, account: str) -> None:
    assert normalize_pikvm_host(endpoint) == account


def test_endpoint_url_preserves_nondefault_port_and_uses_default_scheme() -> None:
    assert endpoint_url("PiKVM.Example:8443/path") == "https://pikvm.example:8443/path"
    assert endpoint_url("http://PiKVM.Example:8080/path") == "http://pikvm.example:8080/path"


@pytest.mark.parametrize(
    "endpoint",
    ["", "ftp://pikvm.example", "https://user:password@pikvm.example", "https://:443"],
)
def test_invalid_or_credentialed_hosts_are_rejected(endpoint: str) -> None:
    with pytest.raises(PiKVMConfigurationError):
        normalize_pikvm_host(endpoint)


def test_raw_base32_and_standard_totp_uri_are_normalized() -> None:
    assert normalize_totp_seed("jbsw y3dp ehpk 3pxp===") == _SECRET
    assert (
        normalize_totp_seed(
            "otpauth://totp/Example?secret=JBSWY3DPEHPK3PXP&digits=6&period=30&algorithm=SHA1"
        )
        == _SECRET
    )


@pytest.mark.parametrize(
    "secret",
    [
        "not-valid-base32!",
        "otpauth://hotp/Example?secret=JBSWY3DPEHPK3PXP&counter=0",
        "otpauth://totp/Example?secret=JBSWY3DPEHPK3PXP&digits=8",
        "otpauth://totp/Example?secret=JBSWY3DPEHPK3PXP&period=60",
    ],
)
def test_malformed_or_unsupported_secrets_are_rejected_without_leaking(secret: str) -> None:
    with pytest.raises(PiKVMTotpSecretError) as caught:
        normalize_totp_seed(secret)

    assert secret not in str(caught.value)


def test_totp_generation_matches_rfc_vector_truncated_to_six_digits() -> None:
    rfc_secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"

    code = generate_totp_code(rfc_secret, clock=lambda: 59.0, sleeper=lambda _: None)

    assert code == "287082"
    assert len(code) == 6
    assert code.isdigit()


def test_totp_waits_when_current_period_is_about_to_expire() -> None:
    times = iter([28.0, 30.0])
    sleeps: list[float] = []

    code = generate_totp_code(_SECRET, clock=lambda: next(times), sleeper=sleeps.append)

    assert sleeps == [2.05]
    assert code == pyotp.TOTP(_SECRET).at(30)


def test_totp_does_not_wait_when_period_has_enough_time() -> None:
    sleeps: list[float] = []

    generate_totp_code(_SECRET, clock=lambda: 10.0, sleeper=sleeps.append)

    assert sleeps == []


def test_keychain_provider_uses_only_the_exact_service_and_host() -> None:
    service = "pikvm-work-agent.totp"
    store = _Store(
        {
            (service, "lutron-3.example.local"): _SECRET,
            (service, "lutron-5.example.local"): "GEZDGNBVGY3TQOJQ",
        }
    )
    provider = KeychainTotpProvider(
        service=service,
        account="lutron-5.example.local",
        store=store,
        clock=lambda: 10.0,
    )

    provider.current_code()

    assert store.lookups == [(service, "lutron-5.example.local")]
    assert _SECRET not in repr(provider)


def test_missing_or_malformed_keychain_secret_is_sanitized() -> None:
    service = "pikvm-work-agent.totp"
    missing = KeychainTotpProvider(
        service=service,
        account="missing.example",
        store=_Store(),
    )
    malformed_value = "PRIVATE-INVALID-SEED"
    malformed = KeychainTotpProvider(
        service=service,
        account="invalid.example",
        store=_Store({(service, "invalid.example"): malformed_value}),
    )

    with pytest.raises(PiKVMKeychainMissingError, match=r"missing\.example"):
        missing.current_code()
    with pytest.raises(PiKVMTotpSecretError) as caught:
        malformed.current_code()

    assert malformed_value not in str(caught.value)


def test_seed_and_generated_code_are_never_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = "pikvm-work-agent.totp"
    provider = KeychainTotpProvider(
        service=service,
        account="private.example",
        store=_Store({(service, "private.example"): _SECRET}),
        clock=lambda: 10.0,
    )

    code = provider.current_code()

    assert _SECRET not in caplog.text
    assert code not in caplog.text


def test_keyring_backend_error_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    private_value = "PRIVATE-KEYRING-DIAGNOSTIC"

    def denied(_: str, __: str) -> str | None:
        raise keyring.errors.KeyringError(private_value)

    monkeypatch.setattr("work_agent.pikvm.totp.keyring.get_password", denied)

    with pytest.raises(PiKVMKeychainError) as caught:
        KeyringSecretStore().get_secret("service", "pikvm.example")

    assert private_value not in str(caught.value)


def test_keyring_adapter_uses_exact_service_and_account(monkeypatch: pytest.MonkeyPatch) -> None:
    entries: dict[tuple[str, str], str] = {}

    def get_password(service: str, account: str) -> str | None:
        return entries.get((service, account))

    def set_password(service: str, account: str, secret: str) -> None:
        entries[(service, account)] = secret

    def delete_password(service: str, account: str) -> None:
        del entries[(service, account)]

    monkeypatch.setattr("work_agent.pikvm.totp.keyring.get_password", get_password)
    monkeypatch.setattr("work_agent.pikvm.totp.keyring.set_password", set_password)
    monkeypatch.setattr("work_agent.pikvm.totp.keyring.delete_password", delete_password)
    store = KeyringSecretStore()

    store.set_secret("pikvm-work-agent.totp", "lutron-3.example", _SECRET)

    assert store.get_secret("pikvm-work-agent.totp", "lutron-3.example") == _SECRET
    assert store.get_secret("pikvm-work-agent.totp", "lutron-5.example") is None
    assert store.delete_secret("pikvm-work-agent.totp", "lutron-3.example") is True
    assert entries == {}


def test_interactive_fallback_is_only_used_when_enabled() -> None:
    settings = _settings(totp_interactive_fallback=True)
    prompts: list[str] = []
    provider = build_totp_provider(
        settings,
        store=_Store(),
        interactive_prompt=lambda prompt: prompts.append(prompt) or "123456",
    )

    assert isinstance(provider, FallbackTotpProvider)
    assert provider.current_code() == "123456"
    assert prompts == ["PiKVM 2FA code: "]


def test_keychain_failure_does_not_prompt_when_fallback_is_disabled() -> None:
    provider = build_totp_provider(
        _settings(totp_interactive_fallback=False),
        store=_Store(),
        interactive_prompt=lambda _: pytest.fail("interactive prompt must not run"),
    )

    with pytest.raises(PiKVMKeychainMissingError):
        provider.current_code()


def test_interactive_provider_must_be_explicitly_configured() -> None:
    provider = build_totp_provider(
        _settings(totp_provider=TotpProviderKind.INTERACTIVE),
        interactive_prompt=lambda _: "654321",
    )

    assert isinstance(provider, InteractiveTotpProvider)
    assert provider.current_code() == "654321"
