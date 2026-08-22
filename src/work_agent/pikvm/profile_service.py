"""Operations on named PiKVM profiles shared by the CLI and the dashboard.

Every mutation here is exactly what `pikvm-agent profiles ...` and `pikvm-agent auth keychain ...`
can do from a terminal; the dashboard calls the same functions so it never gains authority the CLI
lacks. Nothing returned from this module carries a password, a TOTP seed, or a generated code.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from work_agent.agent.pikvm_session import PiKVMSession
from work_agent.pikvm.config import (
    DEFAULT_TOTP_KEYCHAIN_SERVICE,
    PiKVMSettings,
    TotpProviderKind,
    _configured_profiles,
)
from work_agent.pikvm.errors import PiKVMConfigurationError, PiKVMError
from work_agent.pikvm.profiles import (
    ManagedProfileStore,
    ProfileRecord,
    decode_totp_qr_bytes,
    enroll_totp_seed,
    find_profile,
    profile_records,
    totp_enrolled,
)
from work_agent.pikvm.totp import KeychainTotpProvider, SecretStore, build_totp_provider


@dataclass(frozen=True, slots=True)
class ProfileView:
    """What the CLI prints and the dashboard shows for one profile. No secrets."""

    name: str
    host: str
    url: str
    username: str
    source: str
    enabled: bool
    totp_required: bool
    totp_enrolled: bool | None
    verify_ssl: bool
    removable: bool


@dataclass(frozen=True, slots=True)
class ConnectionTest:
    ok: bool
    message: str
    screen_width: int | None = None
    screen_height: int | None = None
    checked_at: datetime | None = None


class ProfileService:
    def __init__(
        self,
        *,
        store: ManagedProfileStore | None = None,
        secret_store: SecretStore | None = None,
        session_factory: Callable[[PiKVMSettings], PiKVMSession] | None = None,
        qr_decoder: Callable[[bytes], tuple[str, ...]] | None = None,
        totp_verifier: Callable[[PiKVMSettings, KeychainTotpProvider], None] | None = None,
    ) -> None:
        self._store = store or ManagedProfileStore(secret_store=secret_store)
        self._secret_store = secret_store
        self._session_factory = session_factory or (
            lambda settings: PiKVMSession(settings, totp_provider=build_totp_provider(settings))
        )
        self._qr_decoder = qr_decoder
        self._totp_verifier = totp_verifier or _verify_with_screenshot

    # ----- reading -------------------------------------------------------------------------

    def list_profiles(self) -> list[ProfileView]:
        return [self._view(record) for record in profile_records(self._store)]

    def get(self, name: str) -> ProfileView:
        record = find_profile(name, self._store)
        if record is None:
            raise PiKVMConfigurationError(f"Unknown PiKVM profile {name!r}.")
        return self._view(record)

    def _view(self, record: ProfileRecord) -> ProfileView:
        enrolled: bool | None = None
        if record.totp_required and record.url:
            try:
                enrolled = totp_enrolled(
                    record.host,
                    service=DEFAULT_TOTP_KEYCHAIN_SERVICE,
                    secret_store=self._secret_store,
                )
            except PiKVMError:
                enrolled = None
        return ProfileView(
            name=record.name,
            host=record.host if record.url else "",
            url=record.url,
            username=record.username,
            source=record.source,
            enabled=record.enabled,
            totp_required=record.totp_required,
            totp_enrolled=enrolled,
            verify_ssl=record.verify_ssl,
            removable=record.source == "managed",
        )

    # ----- mutations -----------------------------------------------------------------------

    def add(
        self,
        *,
        name: str,
        url: str,
        username: str,
        password: str,
        totp_required: bool,
        verify_ssl: bool,
    ) -> ProfileView:
        record = self._store.add(
            name=name,
            url=url,
            username=username,
            password=password,
            totp_required=totp_required,
            verify_ssl=verify_ssl,
            env_names=frozenset(_configured_profiles()),
        )
        return self._view(record)

    def remove(self, name: str) -> list[str]:
        record = find_profile(name, self._store)
        if record is None:
            raise PiKVMConfigurationError(f"Unknown PiKVM profile {name!r}.")
        if record.source != "managed":
            raise PiKVMConfigurationError(
                f"Profile {record.name!r} is defined in .env and cannot be removed here; disable "
                "it, or edit PIKVM_PROFILES in .env."
            )
        remaining = frozenset(
            item.host
            for item in profile_records(self._store)
            if item.name != record.name and item.url
        )
        return self._store.remove(record.name, remaining_hosts=remaining)

    def set_enabled(self, name: str, enabled: bool) -> ProfileView:
        record = find_profile(name, self._store)
        if record is None:
            raise PiKVMConfigurationError(f"Unknown PiKVM profile {name!r}.")
        self._store.set_enabled(record.name, enabled)
        return self._view(replace(record, enabled=enabled))

    # ----- connectivity and 2FA ------------------------------------------------------------

    def test_connection(self, name: str) -> ConnectionTest:
        """Log in and take one screenshot; the harmless read that proves the profile works."""

        try:
            settings = PiKVMSettings.from_env(name)
            with self._session_factory(settings) as session:
                shot = session.get_screenshot()
        except (PiKVMError, OSError, ValueError) as exc:
            return ConnectionTest(ok=False, message=str(exc), checked_at=datetime.now(UTC))
        return ConnectionTest(
            ok=True,
            message=f"Authenticated and captured a {shot.size.width}x{shot.size.height} screen.",
            screen_width=shot.size.width,
            screen_height=shot.size.height,
            checked_at=datetime.now(UTC),
        )

    def enroll_totp_from_image(
        self,
        name: str,
        image: bytes,
        *,
        replace_existing: bool = False,
    ) -> list[str]:
        """Decode a provisioning QR in memory, store its seed in Keychain, verify with a read.

        The image bytes are never written to disk and the seed is never returned.
        """

        record = find_profile(name, self._store)
        if record is None:
            raise PiKVMConfigurationError(f"Unknown PiKVM profile {name!r}.")
        if not record.totp_required:
            raise PiKVMConfigurationError(
                f"Profile {record.name!r} does not require 2FA; nothing to enroll."
            )
        seed = decode_totp_qr_bytes(image, decoder=self._qr_decoder)
        settings = PiKVMSettings.from_env(record.name)
        verification_settings = replace(
            settings,
            totp_required=True,
            totp_provider=TotpProviderKind.KEYCHAIN,
            totp_interactive_fallback=False,
        )

        try:
            enroll_totp_seed(
                seed=seed,
                host=record.host,
                service=settings.totp_keychain_service,
                secret_store=self._secret_store,
                verify=lambda provider: self._totp_verifier(verification_settings, provider),
                replace_existing=replace_existing,
            )
        finally:
            del seed
        return [
            f"TOTP QR decoded locally for {record.host}.",
            "TOTP secret stored in macOS Keychain.",
            "PiKVM authentication verified with a harmless screenshot read.",
        ]


def _verify_with_screenshot(settings: PiKVMSettings, provider: KeychainTotpProvider) -> None:
    with PiKVMSession(settings, totp_provider=provider) as session:
        session.get_screenshot()
