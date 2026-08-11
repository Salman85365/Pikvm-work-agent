from __future__ import annotations

import argparse
from pathlib import Path
from typing import ClassVar

import pytest

from work_agent import auth_cli, cli
from work_agent.pikvm import PiKVMAuthenticationError, PiKVMConfigurationError, PiKVMSettings

_SECRET = "JBSWY3DPEHPK3PXP"


class _Store:
    def __init__(self, entries: dict[tuple[str, str], str] | None = None) -> None:
        self.entries = entries or {}
        self.set_calls: list[tuple[str, str, str]] = []
        self.delete_calls: list[tuple[str, str]] = []

    def get_secret(self, service: str, account: str) -> str | None:
        return self.entries.get((service, account))

    def set_secret(self, service: str, account: str, secret: str) -> None:
        self.set_calls.append((service, account, secret))
        self.entries[(service, account)] = secret

    def delete_secret(self, service: str, account: str) -> bool:
        self.delete_calls.append((service, account))
        return self.entries.pop((service, account), None) is not None


class _VerifySession:
    calls: ClassVar[list[str]] = []
    fail: ClassVar[bool] = False

    def __init__(self, settings: PiKVMSettings, *, totp_provider: object) -> None:
        self.settings = settings
        self.provider = totp_provider

    def __enter__(self) -> _VerifySession:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def get_screenshot(self) -> object:
        code = self.provider.current_code()
        type(self).calls.append(code)
        if type(self).fail:
            raise PiKVMAuthenticationError("PiKVM rejected the configured credentials.")
        return object()


class _Decoder:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.paths: list[Path] = []

    def decode(self, image_path: Path) -> tuple[str, ...]:
        self.paths.append(image_path)
        return (self.payload,)


def _settings(_profile: str | None = None) -> PiKVMSettings:
    return PiKVMSettings(
        base_url="https://default-pikvm.example",
        username="operator",
        password="password",
        profile=_profile,
        verify_ssl=False,
    )


def _args(action: str, host: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(auth_backend="keychain", auth_action=action, host=host)


def _import_args(
    qr: Path,
    *,
    host: str | None = None,
    delete_after_success: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        auth_backend="keychain",
        auth_action="import-qr",
        host=host,
        qr=qr,
        delete_qr_after_success=delete_after_success,
    )


def _configure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_cli.PiKVMSettings, "from_env", _settings)


def test_setup_secret_stores_normalized_seed_for_exact_host_without_echoing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    store = _Store()
    generated: list[str] = []
    prompts: list[str] = []
    monkeypatch.setattr(
        auth_cli,
        "generate_totp_code",
        lambda secret: generated.append(secret) or "999999",
    )

    output = auth_cli.execute_auth_command(
        _args("setup-secret", "https://LUTRON-3.Example.Local/path?ignored=yes"),
        store=store,
        secret_prompt=lambda prompt: prompts.append(prompt) or "jbsw y3dp ehpk 3pxp",
    )

    expected = ("pikvm-work-agent.totp", "lutron-3.example.local", _SECRET)
    assert store.set_calls == [expected]
    assert generated == [_SECRET]
    assert "lutron-3.example.local" in prompts[0]
    assert "stored successfully" in output
    assert _SECRET not in output
    assert "999999" not in output


def test_setup_secret_refuses_existing_entry_without_explicit_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    key = ("pikvm-work-agent.totp", "default-pikvm.example")
    store = _Store({key: _SECRET})

    output = auth_cli.execute_auth_command(
        _args("setup-secret"),
        store=store,
        secret_prompt=lambda _: pytest.fail("secret prompt must not run"),
        confirmation_prompt=lambda _: "",
    )

    assert "not changed" in output
    assert store.entries[key] == _SECRET
    assert store.set_calls == []


def test_setup_secret_replaces_only_after_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    key = ("pikvm-work-agent.totp", "default-pikvm.example")
    store = _Store({key: "GEZDGNBVGY3TQOJQ"})
    monkeypatch.setattr(auth_cli, "generate_totp_code", lambda _: "999999")

    auth_cli.execute_auth_command(
        _args("setup-secret"),
        store=store,
        secret_prompt=lambda _: _SECRET,
        confirmation_prompt=lambda _: "yes",
    )

    assert store.entries[key] == _SECRET


def test_status_reports_only_non_sensitive_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    store = _Store({("pikvm-work-agent.totp", "default-pikvm.example"): _SECRET})

    output = auth_cli.execute_auth_command(_args("status"), store=store)

    assert "Configured: yes" in output
    assert "TOTP provider: keychain" in output
    assert "pikvm-work-agent.totp" in output
    assert _SECRET not in output


def test_status_reports_selected_profile_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    args = _args("status")
    args.profile = "heidrick"

    output = auth_cli.execute_auth_command(args, store=_Store())

    assert "Profile: heidrick" in output
    assert "password" not in output


def test_named_profile_rejects_different_auth_host(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    args = _args("status", host="https://other-pikvm.example")
    args.profile = "heidrick"

    with pytest.raises(PiKVMConfigurationError, match="cannot be combined"):
        auth_cli.execute_auth_command(args, store=_Store())


def test_remove_defaults_to_no_and_removes_only_selected_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    service = "pikvm-work-agent.totp"
    selected = (service, "lutron-3.example.local")
    other = (service, "lutron-5.example.local")
    store = _Store({selected: _SECRET, other: "GEZDGNBVGY3TQOJQ"})

    declined = auth_cli.execute_auth_command(
        _args("remove", "lutron-3.example.local"),
        store=store,
        confirmation_prompt=lambda _: "",
    )
    removed = auth_cli.execute_auth_command(
        _args("remove", "lutron-3.example.local"),
        store=store,
        confirmation_prompt=lambda _: "y",
    )

    assert "not removed" in declined
    assert "Removed" in removed
    assert selected not in store.entries
    assert other in store.entries
    assert store.delete_calls == [selected]


def test_verify_uses_keychain_provider_and_read_only_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    _VerifySession.calls = []
    _VerifySession.fail = False
    monkeypatch.setattr(auth_cli, "PiKVMSession", _VerifySession)
    store = _Store({("pikvm-work-agent.totp", "verify.example"): _SECRET})

    output = auth_cli.execute_auth_command(
        _args("verify", "https://verify.example"),
        store=store,
    )

    assert len(_VerifySession.calls) == 1
    assert len(_VerifySession.calls[0]) == 6
    assert _VerifySession.calls[0] not in output
    assert "authentication successful" in output
    assert "Host: verify.example" in output


def test_import_qr_stores_reads_back_and_verifies_without_deleting_by_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch)
    _VerifySession.calls = []
    _VerifySession.fail = False
    monkeypatch.setattr(auth_cli, "PiKVMSession", _VerifySession)
    monkeypatch.setattr(auth_cli, "generate_totp_code", lambda _: "999999")
    store = _Store()
    image_path = tmp_path / "pikvm.totp-qr.png"
    image_path.write_bytes(b"synthetic fixture placeholder")
    decoder = _Decoder(f"otpauth://totp/PiKVM:operator?secret={_SECRET}&issuer=PiKVM")

    output = auth_cli.execute_auth_command(
        _import_args(image_path, host="https://QR.Example/path"),
        store=store,
        qr_decoder=decoder,
    )

    key = ("pikvm-work-agent.totp", "qr.example")
    assert store.entries[key] == _SECRET
    assert store.set_calls == [(*key, _SECRET)]
    assert decoder.paths == [image_path]
    assert len(_VerifySession.calls) == 1
    assert image_path.exists()
    assert "decoded locally" in output
    assert "authentication verified" in output
    assert _SECRET not in output
    assert _VerifySession.calls[0] not in output


def test_import_qr_deletes_exact_source_only_after_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch)
    _VerifySession.calls = []
    _VerifySession.fail = False
    monkeypatch.setattr(auth_cli, "PiKVMSession", _VerifySession)
    monkeypatch.setattr(auth_cli, "generate_totp_code", lambda _: "999999")
    image_path = tmp_path / "pikvm.totp-qr.jpg"
    image_path.write_bytes(b"synthetic fixture placeholder")
    other_path = tmp_path / "other.png"
    other_path.write_bytes(b"keep")

    output = auth_cli.execute_auth_command(
        _import_args(image_path, delete_after_success=True),
        store=_Store(),
        qr_decoder=_Decoder(f"otpauth://totp/test?secret={_SECRET}"),
    )

    assert not image_path.exists()
    assert other_path.exists()
    assert "deleted after successful verification" in output


def test_import_qr_verification_failure_rolls_back_and_preserves_image(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch)
    _VerifySession.calls = []
    _VerifySession.fail = True
    monkeypatch.setattr(auth_cli, "PiKVMSession", _VerifySession)
    monkeypatch.setattr(auth_cli, "generate_totp_code", lambda _: "999999")
    image_path = tmp_path / "pikvm.totp-qr.png"
    image_path.write_bytes(b"synthetic fixture placeholder")
    store = _Store()

    with pytest.raises(PiKVMAuthenticationError, match="rejected"):
        auth_cli.execute_auth_command(
            _import_args(image_path, delete_after_success=True),
            store=store,
            qr_decoder=_Decoder(f"otpauth://totp/test?secret={_SECRET}"),
        )

    assert store.entries == {}
    assert store.delete_calls == [("pikvm-work-agent.totp", "default-pikvm.example")]
    assert image_path.exists()


def test_import_qr_failed_replacement_restores_previous_credential(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch)
    _VerifySession.calls = []
    _VerifySession.fail = True
    monkeypatch.setattr(auth_cli, "PiKVMSession", _VerifySession)
    monkeypatch.setattr(auth_cli, "generate_totp_code", lambda _: "999999")
    key = ("pikvm-work-agent.totp", "default-pikvm.example")
    old_secret = "GEZDGNBVGY3TQOJQ"
    store = _Store({key: old_secret})
    image_path = tmp_path / "pikvm.totp-qr.png"
    image_path.write_bytes(b"synthetic fixture placeholder")

    with pytest.raises(PiKVMAuthenticationError):
        auth_cli.execute_auth_command(
            _import_args(image_path),
            store=store,
            qr_decoder=_Decoder(f"otpauth://totp/test?secret={_SECRET}"),
            confirmation_prompt=lambda _: "yes",
        )

    assert store.entries[key] == old_secret
    assert store.set_calls[-1] == (*key, old_secret)


def test_import_qr_does_not_overwrite_without_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch)
    monkeypatch.setattr(auth_cli, "generate_totp_code", lambda _: "999999")
    key = ("pikvm-work-agent.totp", "default-pikvm.example")
    old_secret = "GEZDGNBVGY3TQOJQ"
    store = _Store({key: old_secret})
    image_path = tmp_path / "pikvm.totp-qr.png"

    output = auth_cli.execute_auth_command(
        _import_args(image_path),
        store=store,
        qr_decoder=_Decoder(f"otpauth://totp/test?secret={_SECRET}"),
        confirmation_prompt=lambda _: "no",
    )

    assert store.entries[key] == old_secret
    assert store.set_calls == []
    assert "not changed" in output


def test_auth_parser_never_accepts_a_secret_argument() -> None:
    parser = cli.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "auth",
                "keychain",
                "setup-secret",
                "--host",
                "pikvm.example",
                "--secret",
                _SECRET,
            ]
        )


def test_auth_parser_accepts_explicit_qr_import_options(tmp_path: Path) -> None:
    parser = cli.build_parser()
    image_path = tmp_path / "pikvm.totp-qr.png"

    args = parser.parse_args(
        [
            "auth",
            "keychain",
            "import-qr",
            "--host",
            "pikvm.example",
            "--qr",
            str(image_path),
            "--delete-qr-after-success",
        ]
    )

    assert args.host == "pikvm.example"
    assert args.qr == image_path
    assert args.delete_qr_after_success is True


def test_root_cli_routes_keychain_command_without_openai_or_pikvm(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[argparse.Namespace] = []
    monkeypatch.setattr(
        cli,
        "execute_auth_command",
        lambda args: calls.append(args) or "Configured: yes",
    )

    exit_code = cli.run(
        [
            "--profile",
            "heidrick",
            "auth",
            "keychain",
            "status",
            "--host",
            "pikvm.example",
        ]
    )

    assert exit_code == 0
    assert len(calls) == 1
    assert calls[0].profile == "heidrick"
    assert "Configured: yes" in capsys.readouterr().out
