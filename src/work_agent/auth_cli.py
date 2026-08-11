from __future__ import annotations

import argparse
import getpass
import secrets
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from work_agent.agent.pikvm_session import PiKVMSession
from work_agent.pikvm import (
    KeychainTotpProvider,
    KeyringSecretStore,
    PiKVMConfigurationError,
    PiKVMError,
    PiKVMKeychainError,
    PiKVMSettings,
    QrDecoder,
    SecretStore,
    TotpProviderKind,
    decode_totp_qr,
    endpoint_url,
    generate_totp_code,
    normalize_pikvm_host,
    normalize_totp_seed,
)


def add_auth_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    auth = subparsers.add_parser(
        "auth",
        help="Manage local PiKVM authentication helpers.",
    )
    auth_backends = auth.add_subparsers(dest="auth_backend", required=True)
    keychain = auth_backends.add_parser(
        "keychain",
        help="Manage the dedicated macOS Keychain TOTP credential.",
    )
    actions = keychain.add_subparsers(dest="auth_action", required=True)
    for name, help_text in (
        ("setup-secret", "Manually enroll a hidden PiKVM TOTP seed as a fallback."),
        ("status", "Show non-sensitive Keychain configuration metadata."),
        ("verify", "Verify automatic TOTP with a harmless PiKVM read."),
        ("remove", "Remove only this host's local Keychain TOTP entry."),
    ):
        action = actions.add_parser(name, help=help_text)
        action.add_argument(
            "--host",
            default=None,
            help="PiKVM URL or host (default: configured PIKVM_URL).",
        )
    import_qr = actions.add_parser(
        "import-qr",
        help="Decode a PiKVM TOTP QR locally, store it, and verify authentication.",
    )
    import_qr.add_argument(
        "--host",
        default=None,
        help="PiKVM URL or host (default: configured PIKVM_URL).",
    )
    import_qr.add_argument(
        "--qr",
        type=Path,
        required=True,
        help="Local PNG or JPEG containing exactly one PiKVM TOTP QR code.",
    )
    import_qr.add_argument(
        "--delete-qr-after-success",
        action="store_true",
        help="Delete the exact supplied image only after storage and PiKVM verification succeed.",
    )


def execute_auth_command(
    args: argparse.Namespace,
    *,
    store: SecretStore | None = None,
    secret_prompt: Callable[[str], str] | None = None,
    confirmation_prompt: Callable[[str], str] | None = None,
    qr_decoder: QrDecoder | None = None,
) -> str:
    if args.auth_backend != "keychain":
        raise AssertionError(f"Unhandled authentication backend: {args.auth_backend}")

    settings = _settings_for_target(
        PiKVMSettings.from_env(getattr(args, "profile", None)),
        args.host,
    )
    account = normalize_pikvm_host(settings.base_url)
    service = settings.totp_keychain_service
    selected_store = store or KeyringSecretStore()

    if args.auth_action == "status":
        configured = selected_store.get_secret(service, account) is not None
        return "\n".join(
            [
                *([f"Profile: {settings.profile}"] if settings.profile is not None else []),
                f"PiKVM: {account}",
                "TOTP provider: keychain",
                f"Configured: {'yes' if configured else 'no'}",
                f"Keychain service: {service}",
            ]
        )

    if args.auth_action == "setup-secret":
        if selected_store.get_secret(service, account) is not None and not _confirm(
            f"A TOTP credential already exists for {account}. Replace it? [y/N] ",
            confirmation_prompt,
        ):
            return f"TOTP credential for {account} was not changed."
        raw_secret = _read_secret(account, secret_prompt)
        normalized_secret = normalize_totp_seed(raw_secret)
        generate_totp_code(normalized_secret)
        selected_store.set_secret(service, account, normalized_secret)
        del raw_secret, normalized_secret
        return "\n".join(
            [
                f"PiKVM: {account}",
                "TOTP secret stored successfully in macOS Keychain.",
            ]
        )

    if args.auth_action == "import-qr":
        qr_path: Path = args.qr
        normalized_secret = decode_totp_qr(qr_path, decoder=qr_decoder)
        generate_totp_code(normalized_secret)
        previous_secret = selected_store.get_secret(service, account)
        if previous_secret is not None and not _confirm(
            f"A TOTP credential already exists for {account}. Replace it? [y/N] ",
            confirmation_prompt,
        ):
            del normalized_secret, previous_secret
            return f"TOTP credential for {account} was not changed."

        stored = False
        try:
            selected_store.set_secret(service, account, normalized_secret)
            stored = True
            read_back = selected_store.get_secret(service, account)
            if read_back is None or not secrets.compare_digest(read_back, normalized_secret):
                raise PiKVMKeychainError(
                    f"macOS Keychain could not read back the TOTP credential for {account}."
                )
            generate_totp_code(read_back)
            _verify_keychain(settings, selected_store, service, account)
        except PiKVMError:
            if stored:
                _rollback_import(selected_store, service, account, previous_secret)
            raise
        finally:
            del normalized_secret
            if previous_secret is not None:
                del previous_secret

        lines = [
            f"PiKVM: {account}",
            "TOTP QR decoded locally.",
            "TOTP secret stored successfully in macOS Keychain.",
            "PiKVM authentication verified with a harmless screenshot read.",
        ]
        if args.delete_qr_after_success:
            try:
                qr_path.unlink()
            except OSError:
                lines.append(
                    "Warning: authentication succeeded, but the QR image could not be deleted; "
                    "delete it manually."
                )
            else:
                lines.append("QR enrollment image deleted after successful verification.")
        else:
            lines.append(
                "Delete the QR image when you no longer need it; it contains the TOTP credential."
            )
        return "\n".join(lines)

    if args.auth_action == "remove":
        if selected_store.get_secret(service, account) is None:
            return f"No local TOTP credential is configured for {account}."
        if not _confirm(
            f"Remove local TOTP credential for {account}? [y/N] ",
            confirmation_prompt,
        ):
            return f"TOTP credential for {account} was not removed."
        selected_store.delete_secret(service, account)
        return f"Removed the local TOTP credential for {account}."

    if args.auth_action == "verify":
        _verify_keychain(settings, selected_store, service, account)
        return "\n".join(
            [
                "PiKVM authentication successful.",
                "TOTP source: macOS Keychain",
                f"Host: {account}",
            ]
        )

    raise AssertionError(f"Unhandled keychain action: {args.auth_action}")


def _verify_keychain(
    settings: PiKVMSettings,
    store: SecretStore,
    service: str,
    account: str,
) -> None:
    provider = KeychainTotpProvider(service=service, account=account, store=store)
    verification_settings = replace(
        settings,
        totp_required=True,
        totp_provider=TotpProviderKind.KEYCHAIN,
        totp_interactive_fallback=False,
    )
    with PiKVMSession(verification_settings, totp_provider=provider) as session:
        session.get_screenshot()


def _rollback_import(
    store: SecretStore,
    service: str,
    account: str,
    previous_secret: str | None,
) -> None:
    try:
        if previous_secret is None:
            store.delete_secret(service, account)
        else:
            store.set_secret(service, account, previous_secret)
    except PiKVMError:
        raise PiKVMKeychainError(
            "TOTP enrollment failed and the prior Keychain state could not be restored. "
            "Inspect `pikvm-agent auth keychain status` before retrying."
        ) from None


def _settings_for_target(settings: PiKVMSettings, host: str | None) -> PiKVMSettings:
    if host is None:
        return settings
    default_scheme = urlsplit(settings.base_url).scheme
    target_url = endpoint_url(host, default_scheme=default_scheme)
    if settings.profile is not None and normalize_pikvm_host(target_url) != normalize_pikvm_host(
        settings.base_url
    ):
        raise PiKVMConfigurationError(
            "A named PiKVM profile cannot be combined with a different --host. "
            "Select the profile for that host instead."
        )
    return replace(settings, base_url=target_url)


def _read_secret(account: str, prompt: Callable[[str], str] | None) -> str:
    prompt_text = f"PiKVM: {account}\nTOTP setup secret: "
    try:
        return prompt(prompt_text) if prompt is not None else getpass.getpass(prompt_text)
    except EOFError:
        raise PiKVMError(
            "Could not read the TOTP setup secret. Run setup-secret in an interactive terminal."
        ) from None


def _confirm(prompt_text: str, prompt: Callable[[str], str] | None) -> bool:
    try:
        answer = prompt(prompt_text) if prompt is not None else input(prompt_text)
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}
