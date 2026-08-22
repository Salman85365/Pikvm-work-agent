from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv

from work_agent.pikvm.errors import PiKVMConfigurationError

DEFAULT_TOTP_KEYCHAIN_SERVICE = "pikvm-work-agent.totp"
_PROFILE_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")


class TotpProviderKind(StrEnum):
    KEYCHAIN = "keychain"
    INTERACTIVE = "interactive"


_WORK_IDENTITY_MAX_NAMES = 20
_WORK_IDENTITY_MAX_NAME_LENGTH = 120


@dataclass(frozen=True, slots=True)
class WorkIdentity:
    """Names which identify the user at one work environment."""

    name: str
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        name = _identity_value("work identity name", self.name)
        normalized: list[str] = []
        seen: set[str] = set()
        for value in (name, *self.aliases):
            alias = _identity_value("work identity alias", value)
            key = alias.casefold()
            if key not in seen:
                normalized.append(alias)
                seen.add(key)
        if len(normalized) > _WORK_IDENTITY_MAX_NAMES:
            raise PiKVMConfigurationError(
                f"A work identity can contain at most {_WORK_IDENTITY_MAX_NAMES} names."
            )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "aliases", tuple(normalized))

    def matches(self, value: str) -> bool:
        candidate = value.strip().casefold()
        return bool(candidate) and any(candidate == alias.casefold() for alias in self.aliases)


def _identity_value(label: str, raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        raise PiKVMConfigurationError(f"The {label} must not be empty.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise PiKVMConfigurationError(f"The {label} must not contain control characters.")
    if len(value) > _WORK_IDENTITY_MAX_NAME_LENGTH:
        raise PiKVMConfigurationError(
            f"The {label} must not exceed {_WORK_IDENTITY_MAX_NAME_LENGTH} characters."
        )
    return value


def _parse_bool(name: str, raw_value: str) -> bool:
    value = raw_value.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise PiKVMConfigurationError(f"{name} must be one of true/false, yes/no, on/off, or 1/0.")


def _env_float(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return float(raw_value)
    except ValueError as exc:
        raise PiKVMConfigurationError(f"{name} must be a number.") from exc


def _env_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except ValueError as exc:
        raise PiKVMConfigurationError(f"{name} must be an integer.") from exc


def _normalize_profile_name(raw_name: str) -> str:
    name = raw_name.strip().lower()
    if not name or _PROFILE_NAME_PATTERN.fullmatch(name) is None:
        raise PiKVMConfigurationError(
            "PiKVM profile names must use only letters, numbers, hyphens, or underscores and "
            "must start with a letter or number."
        )
    return name


def _profile_prefix(profile: str) -> str:
    return f"PIKVM_{profile.upper().replace('-', '_')}_"


def _configured_profiles() -> tuple[str, ...]:
    raw_profiles = os.getenv("PIKVM_PROFILES")
    if raw_profiles is None:
        return ()
    parts = raw_profiles.split(",")
    if any(not part.strip() for part in parts):
        raise PiKVMConfigurationError(
            "PIKVM_PROFILES must be a comma-separated list without empty profile names."
        )
    profiles = tuple(_normalize_profile_name(part) for part in parts)
    if len(set(profiles)) != len(profiles):
        raise PiKVMConfigurationError("PIKVM_PROFILES contains a duplicate profile name.")
    prefixes = tuple(_profile_prefix(profile) for profile in profiles)
    if len(set(prefixes)) != len(prefixes):
        raise PiKVMConfigurationError(
            "PIKVM_PROFILES contains names that map to the same environment-variable prefix."
        )
    return profiles


def _selected_profile(
    explicit_profile: str | None,
    profiles: tuple[str, ...],
    *,
    disabled: frozenset[str] = frozenset(),
) -> str | None:
    raw_profile = explicit_profile
    if raw_profile is None:
        raw_profile = os.getenv("PIKVM_PROFILE")
    if raw_profile is None:
        enabled = tuple(name for name in profiles if name not in disabled)
        if len(enabled) == 1:
            return enabled[0]
        if len(enabled) > 1:
            raise PiKVMConfigurationError(
                "Multiple PiKVM profiles are configured. Set PIKVM_PROFILE or pass --profile."
            )
        if profiles:
            raise PiKVMConfigurationError(
                "Every configured PiKVM profile is disabled. Enable one in the dashboard or with "
                "`pikvm-agent profiles enable NAME`."
            )
        return None

    profile = _normalize_profile_name(raw_profile)
    if profiles and profile not in profiles:
        raise PiKVMConfigurationError(
            f"Unknown PiKVM profile {profile!r}; add it to PIKVM_PROFILES, add it in the "
            "dashboard, or select a listed name."
        )
    if profile in disabled:
        raise PiKVMConfigurationError(
            f"PiKVM profile {profile!r} is disabled. Enable it in the dashboard or with "
            f"`pikvm-agent profiles enable {profile}` before using it."
        )
    return profile


def _profile_env_name(profile: str | None, setting: str) -> str:
    return f"{_profile_prefix(profile)}{setting}" if profile is not None else f"PIKVM_{setting}"


def _work_identity(
    profile: str | None,
    env: Callable[[str, str | None], str | None],
) -> WorkIdentity | None:
    name_setting = _profile_env_name(profile, "WORK_IDENTITY_NAME")
    aliases_setting = _profile_env_name(profile, "WORK_IDENTITY_ALIASES")
    raw_name = env("WORK_IDENTITY_NAME", None)
    raw_aliases = env("WORK_IDENTITY_ALIASES", None)
    if raw_name is None and raw_aliases is None:
        return None
    if raw_name is None or not raw_name.strip():
        raise PiKVMConfigurationError(
            f"{name_setting} is required when {aliases_setting} is configured."
        )

    aliases: tuple[str, ...] = ()
    if raw_aliases is not None:
        parts = raw_aliases.split(",")
        if any(not part.strip() for part in parts):
            raise PiKVMConfigurationError(
                f"{aliases_setting} must be a comma-separated list without empty aliases."
            )
        aliases = tuple(parts)
    return WorkIdentity(name=raw_name, aliases=aliases)


def configured_pikvm_profiles() -> tuple[str, ...]:
    """Return every enabled named profile (.env and managed) without loading credentials."""

    from work_agent.pikvm.profiles import enabled_profile_names

    load_dotenv()
    return enabled_profile_names()


def _known_profiles() -> tuple[tuple[str, ...], frozenset[str]]:
    """All profile names from both sources, plus the shared disabled set."""

    from work_agent.pikvm.profiles import ManagedProfileStore

    store = ManagedProfileStore()
    env_names = _configured_profiles()
    managed = tuple(name for name in sorted(store.managed_profiles()) if name not in env_names)
    return env_names + managed, store.disabled_names()


@dataclass(frozen=True, slots=True)
class PiKVMSettings:
    """Connection settings loaded locally on the controlling Mac."""

    base_url: str
    username: str
    password: str = field(repr=False)
    profile: str | None = None
    work_identity: WorkIdentity | None = field(default=None, repr=False)
    totp_required: bool = True
    totp_provider: TotpProviderKind = TotpProviderKind.KEYCHAIN
    totp_keychain_service: str = DEFAULT_TOTP_KEYCHAIN_SERVICE
    totp_interactive_fallback: bool = False
    verify_ssl: bool = True
    connect_timeout: float = 5.0
    request_timeout: float = 15.0
    max_retries: int = 2
    retry_backoff: float = 0.25
    mouse_move_settle_seconds: float = 0.1
    keymap: str | None = "en-us"

    def __post_init__(self) -> None:
        if self.profile is not None:
            object.__setattr__(self, "profile", _normalize_profile_name(self.profile))
        raw_url = self.base_url.strip()
        parsed = urlsplit(raw_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise PiKVMConfigurationError("PIKVM_URL must be an absolute http:// or https:// URL.")
        if parsed.username is not None or parsed.password is not None:
            raise PiKVMConfigurationError(
                "Do not place credentials in PIKVM_URL; use PIKVM_USERNAME and PIKVM_PASSWORD."
            )
        if parsed.query or parsed.fragment:
            raise PiKVMConfigurationError("PIKVM_URL cannot contain a query string or fragment.")

        normalized_path = parsed.path.rstrip("/")
        normalized = urlunsplit((parsed.scheme, parsed.netloc, normalized_path, "", ""))
        object.__setattr__(self, "base_url", normalized)

        if not self.username.strip():
            raise PiKVMConfigurationError("PIKVM_USERNAME is required.")
        if not self.password:
            raise PiKVMConfigurationError("PIKVM_PASSWORD is required.")
        service = self.totp_keychain_service.strip()
        if not service or any(character.isspace() for character in service):
            raise PiKVMConfigurationError(
                "PIKVM_TOTP_KEYCHAIN_SERVICE must not be empty or contain whitespace."
            )
        object.__setattr__(self, "totp_keychain_service", service)
        if self.connect_timeout <= 0 or self.request_timeout <= 0:
            raise PiKVMConfigurationError("PiKVM timeouts must be greater than zero.")
        if self.max_retries < 0:
            raise PiKVMConfigurationError("PIKVM_MAX_RETRIES cannot be negative.")
        if self.retry_backoff < 0:
            raise PiKVMConfigurationError("PIKVM_RETRY_BACKOFF cannot be negative.")
        if not 0 <= self.mouse_move_settle_seconds <= 2:
            raise PiKVMConfigurationError(
                "PIKVM_MOUSE_MOVE_SETTLE_SECONDS must be between 0 and 2 seconds."
            )
        if self.keymap is not None and not self.keymap.strip():
            object.__setattr__(self, "keymap", None)

    @classmethod
    def from_env(cls, profile: str | None = None) -> PiKVMSettings:
        """Load settings from environment variables and an optional local .env file."""

        load_dotenv()

        profiles, disabled = _known_profiles()
        selected_profile = _selected_profile(profile, profiles, disabled=disabled)
        if selected_profile is not None and selected_profile not in _configured_profiles():
            return cls._from_managed(selected_profile)

        def env(setting: str, default: str | None = None) -> str | None:
            return os.getenv(_profile_env_name(selected_profile, setting), default)

        required = {
            _profile_env_name(selected_profile, "URL"): env("URL"),
            _profile_env_name(selected_profile, "USERNAME"): env("USERNAME"),
            _profile_env_name(selected_profile, "PASSWORD"): env("PASSWORD"),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise PiKVMConfigurationError(
                "Missing required setting(s): " + ", ".join(sorted(missing))
            )

        verify_ssl_name = _profile_env_name(selected_profile, "VERIFY_SSL")
        verify_ssl = _parse_bool(verify_ssl_name, env("VERIFY_SSL", "true") or "")
        provider_name = _profile_env_name(selected_profile, "TOTP_PROVIDER")
        raw_provider = env("TOTP_PROVIDER", TotpProviderKind.KEYCHAIN.value) or ""
        try:
            totp_provider = TotpProviderKind(raw_provider.strip().lower())
        except ValueError as exc:
            raise PiKVMConfigurationError(
                f"{provider_name} must be keychain or interactive."
            ) from exc
        return cls(
            base_url=required[_profile_env_name(selected_profile, "URL")] or "",
            username=required[_profile_env_name(selected_profile, "USERNAME")] or "",
            password=required[_profile_env_name(selected_profile, "PASSWORD")] or "",
            profile=selected_profile,
            work_identity=_work_identity(selected_profile, env),
            totp_required=_parse_bool(
                _profile_env_name(selected_profile, "TOTP_REQUIRED"),
                env("TOTP_REQUIRED", "true") or "",
            ),
            totp_provider=totp_provider,
            totp_keychain_service=env("TOTP_KEYCHAIN_SERVICE", DEFAULT_TOTP_KEYCHAIN_SERVICE) or "",
            totp_interactive_fallback=_parse_bool(
                _profile_env_name(selected_profile, "TOTP_INTERACTIVE_FALLBACK"),
                env("TOTP_INTERACTIVE_FALLBACK", "false") or "",
            ),
            verify_ssl=verify_ssl,
            connect_timeout=_env_float(_profile_env_name(selected_profile, "CONNECT_TIMEOUT"), 5.0),
            request_timeout=_env_float(
                _profile_env_name(selected_profile, "REQUEST_TIMEOUT"), 15.0
            ),
            max_retries=_env_int(_profile_env_name(selected_profile, "MAX_RETRIES"), 2),
            retry_backoff=_env_float(_profile_env_name(selected_profile, "RETRY_BACKOFF"), 0.25),
            mouse_move_settle_seconds=_env_float(
                _profile_env_name(selected_profile, "MOUSE_MOVE_SETTLE_SECONDS"), 0.1
            ),
            keymap=env("KEYMAP", "en-us"),
        )

    @classmethod
    def _from_managed(cls, profile: str) -> PiKVMSettings:
        from work_agent.pikvm.profiles import ManagedProfileStore

        store = ManagedProfileStore()
        record = store.managed_profiles().get(profile)
        if record is None:
            raise PiKVMConfigurationError(f"Unknown managed PiKVM profile {profile!r}.")
        return cls(
            base_url=record.url,
            username=record.username,
            password=store.password(profile),
            profile=profile,
            totp_required=record.totp_required,
            totp_provider=TotpProviderKind.KEYCHAIN,
            verify_ssl=record.verify_ssl,
            connect_timeout=_env_float("PIKVM_CONNECT_TIMEOUT", 5.0),
            request_timeout=_env_float("PIKVM_REQUEST_TIMEOUT", 15.0),
            max_retries=_env_int("PIKVM_MAX_RETRIES", 2),
            retry_backoff=_env_float("PIKVM_RETRY_BACKOFF", 0.25),
            mouse_move_settle_seconds=_env_float("PIKVM_MOUSE_MOVE_SETTLE_SECONDS", 0.1),
            keymap=record.keymap,
        )
