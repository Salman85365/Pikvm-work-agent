"""Named PiKVM profiles from two sources, with a shared enable/disable switch.

`.env` profiles (`PIKVM_PROFILES` + `PIKVM_<NAME>_*`) remain the hand-edited source. Profiles added
from the dashboard or `pikvm-agent profiles add` are *managed*: their non-secret fields live in an
owner-only JSON file under Application Support and their password lives in macOS Keychain (service
``pikvm-work-agent.password``, account = profile name). The password never touches a file. A
profile from either source can be disabled; disabled profiles are skipped by every `--all-kvms`
workflow, the scheduler, and the dashboard, and an explicit selection of one is refused with a
message that says so.

TOTP seeds keep their existing home: the ``pikvm-work-agent.totp`` Keychain service keyed by the
PiKVM host, so enrolling a seed for a managed profile and for a `.env` profile of the same host is
the same operation.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from work_agent.pikvm.errors import (
    PiKVMConfigurationError,
    PiKVMError,
    PiKVMKeychainError,
    PiKVMQrError,
)
from work_agent.pikvm.totp import (
    KeychainTotpProvider,
    KeyringSecretStore,
    SecretStore,
    generate_totp_code,
    normalize_pikvm_host,
    normalize_totp_uri,
)

PASSWORD_KEYCHAIN_SERVICE = "pikvm-work-agent.password"
MANAGED_PROFILES_PATH = (
    Path.home() / "Library" / "Application Support" / "pikvm-work-agent" / "profiles.json"
)
_SCHEMA_VERSION = 1
_PROFILE_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
_MAX_NAME_LENGTH = 40


@dataclass(frozen=True, slots=True)
class ProfileRecord:
    """One named PiKVM as the dashboard and CLI see it. Never carries a password or seed."""

    name: str
    url: str
    username: str
    totp_required: bool
    verify_ssl: bool
    source: str  # "env" | "managed"
    enabled: bool
    keymap: str | None = "en-us"

    @property
    def host(self) -> str:
        return normalize_pikvm_host(self.url)


def normalize_profile_name(raw_name: str) -> str:
    name = raw_name.strip().lower()
    if not name or len(name) > _MAX_NAME_LENGTH or _PROFILE_NAME_PATTERN.fullmatch(name) is None:
        raise PiKVMConfigurationError(
            "PiKVM profile names must use only letters, numbers, hyphens, or underscores, start "
            f"with a letter or number, and be at most {_MAX_NAME_LENGTH} characters."
        )
    return name


@dataclass(slots=True)
class _Payload:
    profiles: dict[str, dict[str, object]]
    disabled: list[str]


class ManagedProfileStore:
    """Owner-only JSON for non-secret profile fields plus the shared disabled list."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        secret_store: SecretStore | None = None,
    ) -> None:
        override = os.getenv("PIKVM_AGENT_PROFILES_FILE")
        self._path = path or (Path(override).expanduser() if override else MANAGED_PROFILES_PATH)
        self._secrets = secret_store or KeyringSecretStore()

    @property
    def path(self) -> Path:
        return self._path

    # ----- reading -------------------------------------------------------------------------

    def managed_profiles(self) -> dict[str, ProfileRecord]:
        payload = self._load()
        disabled = set(payload.disabled)
        records: dict[str, ProfileRecord] = {}
        for name, fields in payload.profiles.items():
            keymap = fields.get("keymap", "en-us")
            records[name] = ProfileRecord(
                name=name,
                url=str(fields["url"]),
                username=str(fields["username"]),
                totp_required=bool(fields.get("totp_required", True)),
                verify_ssl=bool(fields.get("verify_ssl", False)),
                keymap=str(keymap) if keymap is not None else None,
                source="managed",
                enabled=name not in disabled,
            )
        return records

    def disabled_names(self) -> frozenset[str]:
        return frozenset(self._load().disabled)

    def password(self, name: str) -> str:
        normalized = normalize_profile_name(name)
        if normalized not in self._load().profiles:
            raise PiKVMConfigurationError(f"Unknown managed PiKVM profile {normalized!r}.")
        password = self._secrets.get_secret(PASSWORD_KEYCHAIN_SERVICE, normalized)
        if password is None:
            raise PiKVMKeychainError(
                f"macOS Keychain has no password for managed profile {normalized!r}; remove and "
                "re-add the profile."
            )
        return password

    # ----- writing -------------------------------------------------------------------------

    def add(
        self,
        *,
        name: str,
        url: str,
        username: str,
        password: str,
        totp_required: bool,
        verify_ssl: bool,
        keymap: str | None = "en-us",
        env_names: frozenset[str] = frozenset(),
    ) -> ProfileRecord:
        normalized = normalize_profile_name(name)
        if normalized in env_names:
            raise PiKVMConfigurationError(
                f"Profile {normalized!r} is defined in .env; edit it there or pick another name."
            )
        payload = self._load()
        if normalized in payload.profiles:
            raise PiKVMConfigurationError(f"Managed profile {normalized!r} already exists.")
        if not username.strip():
            raise PiKVMConfigurationError("A PiKVM username is required.")
        if not password:
            raise PiKVMConfigurationError("A PiKVM password is required.")
        # Validate the URL the same way PiKVMSettings does, without storing anything yet.
        from work_agent.pikvm.config import PiKVMSettings

        settings = PiKVMSettings(
            base_url=url,
            username=username,
            password=password,
            profile=normalized,
            totp_required=totp_required,
            verify_ssl=verify_ssl,
            keymap=keymap,
        )
        self._secrets.set_secret(PASSWORD_KEYCHAIN_SERVICE, normalized, password)
        try:
            payload.profiles[normalized] = {
                "url": settings.base_url,
                "username": settings.username,
                "totp_required": totp_required,
                "verify_ssl": verify_ssl,
                "keymap": settings.keymap,
            }
            self._save(payload)
        except BaseException:
            with _suppress_keychain_errors():
                self._secrets.delete_secret(PASSWORD_KEYCHAIN_SERVICE, normalized)
            raise
        return self.managed_profiles()[normalized]

    def remove(
        self,
        name: str,
        *,
        remaining_hosts: frozenset[str] = frozenset(),
        totp_keychain_service: str = "pikvm-work-agent.totp",
    ) -> list[str]:
        """Remove a managed profile and its password; drop the TOTP seed only if no other
        profile still uses that host. Returns human-readable notes."""

        normalized = normalize_profile_name(name)
        payload = self._load()
        fields = payload.profiles.get(normalized)
        if fields is None:
            raise PiKVMConfigurationError(f"Unknown managed PiKVM profile {normalized!r}.")
        host = normalize_pikvm_host(str(fields["url"]))
        del payload.profiles[normalized]
        payload.disabled = [item for item in payload.disabled if item != normalized]
        self._save(payload)
        notes = [f"Removed managed profile {normalized!r}."]
        with _suppress_keychain_errors():
            if self._secrets.delete_secret(PASSWORD_KEYCHAIN_SERVICE, normalized):
                notes.append("Removed its password from macOS Keychain.")
        if host not in remaining_hosts:
            try:
                if self._secrets.delete_secret(totp_keychain_service, host):
                    notes.append(f"Removed the TOTP credential for {host} from macOS Keychain.")
            except PiKVMKeychainError:
                notes.append(
                    f"The TOTP credential for {host} could not be removed; use "
                    "`pikvm-agent auth keychain remove`."
                )
        else:
            notes.append(f"Kept the TOTP credential for {host}; another profile uses it.")
        return notes

    def set_enabled(self, name: str, enabled: bool) -> None:
        normalized = normalize_profile_name(name)
        payload = self._load()
        disabled = [item for item in payload.disabled if item != normalized]
        if not enabled:
            disabled.append(normalized)
        payload.disabled = sorted(disabled)
        self._save(payload)

    # ----- file handling -------------------------------------------------------------------

    def _load(self) -> _Payload:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _Payload(profiles={}, disabled=[])
        except OSError as exc:
            raise PiKVMConfigurationError(
                f"The managed PiKVM profile file could not be read: {exc.strerror}."
            ) from None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise PiKVMConfigurationError(
                f"The managed PiKVM profile file {self._path} is not valid JSON; fix or move it "
                "aside."
            ) from None
        if not isinstance(data, dict):
            raise PiKVMConfigurationError("The managed PiKVM profile file has an unknown shape.")
        profiles = data.get("profiles", {})
        disabled = data.get("disabled", [])
        if not isinstance(profiles, dict) or not isinstance(disabled, list):
            raise PiKVMConfigurationError("The managed PiKVM profile file has an unknown shape.")
        return _Payload(
            profiles={
                str(name): dict(fields)
                for name, fields in profiles.items()
                if isinstance(fields, dict) and "url" in fields and "username" in fields
            },
            disabled=sorted({str(item) for item in disabled}),
        )

    def _save(self, payload: _Payload) -> None:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        document = {
            "schema": _SCHEMA_VERSION,
            "profiles": payload.profiles,
            "disabled": payload.disabled,
        }
        text = json.dumps(document, indent=2, sort_keys=True) + "\n"
        descriptor, temp_name = tempfile.mkstemp(
            dir=self._path.parent, prefix=".profiles-", suffix=".json"
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(text)
            os.replace(temp_name, self._path)
        except BaseException:
            with _suppress_os_errors():
                os.unlink(temp_name)
            raise


class _suppress_keychain_errors:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return isinstance(exc, PiKVMKeychainError)


class _suppress_os_errors:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return isinstance(exc, OSError)


# ----- registry across both sources ---------------------------------------------------------


def env_profile_records() -> list[ProfileRecord]:
    """Profiles declared in .env, described without their password."""

    from dotenv import load_dotenv

    from work_agent.pikvm import config as _config

    load_dotenv()
    records: list[ProfileRecord] = []
    for name in _config._configured_profiles():

        def env(setting: str, default: str | None = None, *, _name: str = name) -> str | None:
            return os.getenv(_config._profile_env_name(_name, setting), default)

        url = env("URL") or ""
        username = env("USERNAME") or ""
        try:
            totp_required = _config._parse_bool("TOTP_REQUIRED", env("TOTP_REQUIRED", "true") or "")
            verify_ssl = _config._parse_bool("VERIFY_SSL", env("VERIFY_SSL", "true") or "")
        except PiKVMConfigurationError:
            totp_required, verify_ssl = True, True
        records.append(
            ProfileRecord(
                name=name,
                url=url,
                username=username,
                totp_required=totp_required,
                verify_ssl=verify_ssl,
                keymap=env("KEYMAP", "en-us"),
                source="env",
                enabled=True,
            )
        )
    return records


def profile_records(store: ManagedProfileStore | None = None) -> list[ProfileRecord]:
    """Every known profile, .env first then managed, with the shared disabled list applied."""

    selected = store or ManagedProfileStore()
    disabled = selected.disabled_names()
    records = [replace(item, enabled=item.name not in disabled) for item in env_profile_records()]
    env_names = {item.name for item in records}
    for name, item in sorted(selected.managed_profiles().items()):
        if name in env_names:
            continue
        records.append(item)
    return records


def enabled_profile_names(store: ManagedProfileStore | None = None) -> tuple[str, ...]:
    return tuple(item.name for item in profile_records(store) if item.enabled)


def find_profile(name: str, store: ManagedProfileStore | None = None) -> ProfileRecord | None:
    normalized = normalize_profile_name(name)
    for item in profile_records(store):
        if item.name == normalized:
            return item
    return None


# ----- TOTP enrollment from an in-memory image ----------------------------------------------


def decode_totp_qr_bytes(
    data: bytes, *, decoder: Callable[[bytes], tuple[str, ...]] | None = None
) -> str:
    """Decode one PiKVM provisioning QR from image bytes, entirely in memory."""

    payloads = (decoder or _zxing_decode_bytes)(data)
    if not payloads:
        raise PiKVMQrError("No QR code was found in the supplied image.")
    if len(payloads) != 1:
        raise PiKVMQrError(
            "Multiple QR codes were found. Supply an image containing only the PiKVM TOTP QR."
        )
    try:
        return normalize_totp_uri(payloads[0])
    except PiKVMError as exc:
        raise PiKVMQrError(str(exc)) from None


def _zxing_decode_bytes(data: bytes) -> tuple[str, ...]:
    from io import BytesIO

    import zxingcpp
    from PIL import Image, UnidentifiedImageError

    if len(data) > 8 * 1024 * 1024:
        raise PiKVMQrError("The TOTP QR image is larger than 8 MB.")
    try:
        with Image.open(BytesIO(data)) as source:
            if source.format not in {"JPEG", "PNG"}:
                raise PiKVMQrError("The TOTP QR image must be a PNG or JPEG file.")
            image = source.convert("RGB")
            image.load()
    except PiKVMQrError:
        raise
    except (Image.DecompressionBombError, OSError, SyntaxError, UnidentifiedImageError):
        raise PiKVMQrError("The TOTP QR image could not be decoded as PNG or JPEG.") from None
    try:
        barcodes = zxingcpp.read_barcodes(
            image, formats=zxingcpp.BarcodeFormats(zxingcpp.BarcodeFormat.QRCode)
        )
        return tuple(barcode.text for barcode in barcodes if barcode.valid)
    except (RuntimeError, TypeError, UnicodeError, ValueError):
        raise PiKVMQrError("The local QR decoder could not process the image.") from None


def totp_enrolled(host: str, *, service: str, secret_store: SecretStore | None = None) -> bool:
    store = secret_store or KeyringSecretStore()
    try:
        return store.get_secret(service, host) is not None
    except PiKVMKeychainError:
        return False


def enroll_totp_seed(
    *,
    seed: str,
    host: str,
    service: str,
    secret_store: SecretStore | None = None,
    verify: Callable[[KeychainTotpProvider], None] | None = None,
    replace_existing: bool = False,
) -> None:
    """Store a seed for ``host`` in Keychain, read it back, verify, and roll back on failure.

    ``verify`` receives a provider bound to the stored seed and should perform a harmless
    authenticated PiKVM read; it may raise any PiKVMError to trigger the rollback.
    """

    store = secret_store or KeyringSecretStore()
    generate_totp_code(seed)
    previous = store.get_secret(service, host)
    if previous is not None and not replace_existing:
        raise PiKVMConfigurationError(
            f"A TOTP credential already exists for {host}; confirm replacement to overwrite it."
        )
    stored = False
    try:
        store.set_secret(service, host, seed)
        stored = True
        read_back = store.get_secret(service, host)
        if read_back is None or not secrets.compare_digest(read_back, seed):
            raise PiKVMKeychainError(
                f"macOS Keychain could not read back the TOTP credential for {host}."
            )
        if verify is not None:
            verify(KeychainTotpProvider(service=service, account=host, store=store))
    except PiKVMError:
        if stored:
            try:
                if previous is None:
                    store.delete_secret(service, host)
                else:
                    store.set_secret(service, host, previous)
            except PiKVMError:
                raise PiKVMKeychainError(
                    "TOTP enrollment failed and the prior Keychain state could not be restored. "
                    "Inspect `pikvm-agent auth keychain status` before retrying."
                ) from None
        raise


def describe_unknown_profile(name: str, store: ManagedProfileStore | None = None) -> str:
    """Say whether an unusable profile name is unknown or merely disabled."""

    try:
        record = find_profile(name, store)
    except PiKVMConfigurationError:
        record = None
    if record is not None and not record.enabled:
        return (
            f"PiKVM profile {record.name!r} is disabled. Enable it in the dashboard or with "
            f"`pikvm-agent profiles enable {record.name}`."
        )
    return f"Unknown PiKVM profile {name!r}."
