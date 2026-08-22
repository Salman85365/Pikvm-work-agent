from __future__ import annotations

import json
from pathlib import Path

import pytest

from work_agent.pikvm import PiKVMConfigurationError, PiKVMSettings, configured_pikvm_profiles
from work_agent.pikvm.errors import PiKVMQrError
from work_agent.pikvm.profile_service import ProfileService
from work_agent.pikvm.profiles import (
    PASSWORD_KEYCHAIN_SERVICE,
    ManagedProfileStore,
    enabled_profile_names,
    profile_records,
)
from work_agent.profiles_cli import execute_profiles_command


class _Secrets:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}

    def get_secret(self, service: str, account: str) -> str | None:
        return self.items.get((service, account))

    def set_secret(self, service: str, account: str, secret: str) -> None:
        self.items[(service, account)] = secret

    def delete_secret(self, service: str, account: str) -> bool:
        return self.items.pop((service, account), None) is not None


def _env_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PIKVM_PROFILES", "heidrick,nbc_kvm")
    monkeypatch.setenv("PIKVM_PROFILE", "nbc_kvm")
    for name, host in (("HEIDRICK", "100.94.8.25"), ("NBC_KVM", "100.73.125.112")):
        monkeypatch.setenv(f"PIKVM_{name}_URL", f"https://{host}")
        monkeypatch.setenv(f"PIKVM_{name}_USERNAME", "admin")
        monkeypatch.setenv(f"PIKVM_{name}_PASSWORD", "env-secret")
        monkeypatch.setenv(f"PIKVM_{name}_TOTP_REQUIRED", "true")
        monkeypatch.setenv(f"PIKVM_{name}_VERIFY_SSL", "false")


def _store(tmp_path: Path, secrets: _Secrets) -> ManagedProfileStore:
    return ManagedProfileStore(tmp_path / "profiles.json", secret_store=secrets)


def test_env_and_managed_profiles_merge_and_disable_applies_to_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_profiles(monkeypatch)
    import keyring

    store = ManagedProfileStore(tmp_path / "profiles.json")
    monkeypatch.setenv("PIKVM_AGENT_PROFILES_FILE", str(store.path))

    store.add(
        name="Lab-KVM",
        url="https://lab.example.local/kvm/",
        username="operator",
        password="lab-secret",
        totp_required=False,
        verify_ssl=False,
        env_names=frozenset({"heidrick", "nbc_kvm"}),
    )
    store.set_enabled("heidrick", False)

    names = [(item.name, item.source, item.enabled) for item in profile_records(store)]
    assert names == [
        ("heidrick", "env", False),
        ("nbc_kvm", "env", True),
        ("lab-kvm", "managed", True),
    ]
    assert enabled_profile_names(store) == ("nbc_kvm", "lab-kvm")
    assert configured_pikvm_profiles() == ("nbc_kvm", "lab-kvm")

    # The file never holds the password; Keychain does, and it is 0600.
    document = json.loads(store.path.read_text())
    assert "lab-secret" not in json.dumps(document)
    assert keyring.get_password(PASSWORD_KEYCHAIN_SERVICE, "lab-kvm") == "lab-secret"
    assert store.path.stat().st_mode & 0o777 == 0o600

    managed = PiKVMSettings.from_env("lab-kvm")
    assert managed.base_url == "https://lab.example.local/kvm"
    assert managed.password == "lab-secret"
    assert managed.totp_required is False

    with pytest.raises(PiKVMConfigurationError, match="disabled"):
        PiKVMSettings.from_env("heidrick")
    # The default selection still works and the env profile still loads normally.
    assert PiKVMSettings.from_env().profile == "nbc_kvm"


def test_removing_a_managed_profile_cleans_keychain_but_keeps_shared_totp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_profiles(monkeypatch)
    secrets = _Secrets()
    store = _store(tmp_path, secrets)
    secrets.set_secret("pikvm-work-agent.totp", "100.94.8.25", "SEED")
    store.add(
        name="mirror",
        url="https://100.94.8.25",
        username="admin",
        password="pw",
        totp_required=True,
        verify_ssl=False,
    )
    service = ProfileService(store=store, secret_store=secrets)

    notes = service.remove("mirror")

    assert any("Kept the TOTP credential" in note for note in notes)
    assert (PASSWORD_KEYCHAIN_SERVICE, "mirror") not in secrets.items
    assert secrets.items[("pikvm-work-agent.totp", "100.94.8.25")] == "SEED"
    with pytest.raises(PiKVMConfigurationError, match=r"defined in \.env"):
        service.remove("heidrick")
    with pytest.raises(PiKVMConfigurationError, match=r"defined in \.env"):
        store.add(
            name="heidrick",
            url="https://x",
            username="a",
            password="b",
            totp_required=True,
            verify_ssl=False,
            env_names=frozenset({"heidrick"}),
        )


def test_service_views_expose_no_secrets_and_report_enrollment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_profiles(monkeypatch)
    secrets = _Secrets()
    secrets.set_secret("pikvm-work-agent.totp", "100.73.125.112", "SEED")
    service = ProfileService(store=_store(tmp_path, secrets), secret_store=secrets)

    views = {view.name: view for view in service.list_profiles()}

    assert views["nbc_kvm"].totp_enrolled is True
    assert views["heidrick"].totp_enrolled is False
    assert views["heidrick"].removable is False
    for view in views.values():
        assert "env-secret" not in repr(view) and "SEED" not in repr(view)


def test_totp_enrollment_from_image_bytes_verifies_and_never_returns_the_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_profiles(monkeypatch)
    secrets = _Secrets()
    verified: list[str] = []
    uri = "otpauth://totp/PiKVM:admin?secret=JBSWY3DPEHPK3PXP&issuer=PiKVM"
    service = ProfileService(
        store=_store(tmp_path, secrets),
        secret_store=secrets,
        qr_decoder=lambda data: (uri,) if data == b"qr-bytes" else (),
        totp_verifier=lambda settings, provider: verified.append(provider.current_code()),
    )

    notes = service.enroll_totp_from_image("heidrick", b"qr-bytes")

    assert secrets.items[("pikvm-work-agent.totp", "100.94.8.25")] == "JBSWY3DPEHPK3PXP"
    assert len(verified) == 1 and len(verified[0]) == 6
    assert all("JBSWY3DPEHPK3PXP" not in note for note in notes)
    with pytest.raises(PiKVMQrError, match="No QR code"):
        service.enroll_totp_from_image("heidrick", b"not-a-qr", replace_existing=True)
    with pytest.raises(PiKVMConfigurationError, match="already exists"):
        service.enroll_totp_from_image("heidrick", b"qr-bytes")


def test_failed_verification_rolls_the_seed_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_profiles(monkeypatch)
    secrets = _Secrets()
    secrets.set_secret("pikvm-work-agent.totp", "100.94.8.25", "OLDSEEDOLDSEEDOL")
    from work_agent.pikvm.errors import PiKVMAuthenticationError

    def reject(settings: object, provider: object) -> None:
        raise PiKVMAuthenticationError("rejected")

    service = ProfileService(
        store=_store(tmp_path, secrets),
        secret_store=secrets,
        qr_decoder=lambda data: ("otpauth://totp/PiKVM:admin?secret=JBSWY3DPEHPK3PXP",),
        totp_verifier=reject,
    )

    with pytest.raises(PiKVMAuthenticationError):
        service.enroll_totp_from_image("heidrick", b"qr", replace_existing=True)

    assert secrets.items[("pikvm-work-agent.totp", "100.94.8.25")] == "OLDSEEDOLDSEEDOL"


def test_profiles_cli_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _env_profiles(monkeypatch)
    secrets = _Secrets()
    service = ProfileService(store=_store(tmp_path, secrets), secret_store=secrets)

    def run(*argv: str) -> str:
        from work_agent.cli import build_parser

        args = build_parser().parse_args(["profiles", *argv])
        return execute_profiles_command(
            args, service=service, password_prompt=lambda _: "typed-secret"
        )

    added = run("add", "lab", "--url", "https://lab.example", "--username", "op", "--no-2fa")
    assert "Added managed profile 'lab'" in added
    assert "typed-secret" not in added
    assert "now disabled" in run("disable", "heidrick")
    listing = run("list")
    assert "heidrick" in listing and "DISABLED" in listing and "lab" in listing
    assert "enabled" in run("enable", "heidrick")
    assert "Removed managed profile 'lab'" in run("remove", "lab")
    assert "lab" not in run("list")
