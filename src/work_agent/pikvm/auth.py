from __future__ import annotations

from work_agent.pikvm.config import PiKVMSettings
from work_agent.pikvm.errors import PiKVMConfigurationError
from work_agent.pikvm.totp import TotpProvider, validate_totp_code

USER_AGENT = "pikvm-work-agent/0.1.1"


def build_pikvm_credentials(
    settings: PiKVMSettings,
    *,
    totp_provider: TotpProvider | None,
) -> tuple[str, str]:
    """Return one short-lived ``(user, password)`` pair with a current TOTP appended.

    Callers must keep the result out of logs and process arguments, and must use it promptly:
    PiKVM accepts the appended code only for roughly one TOTP period either side of now.
    """

    password = settings.password
    if settings.totp_required:
        if totp_provider is None:
            raise PiKVMConfigurationError(
                "PiKVM 2FA is enabled but no TOTP provider was configured."
            )
        password += validate_totp_code(totp_provider.current_code())
    return settings.username, password


def build_pikvm_auth_headers(
    settings: PiKVMSettings,
    *,
    totp_provider: TotpProvider | None,
) -> dict[str, str]:
    """Build one short-lived PiKVM per-request authentication header set.

    Suitable only for a single connection established immediately (the WebRTC handshake). Long
    sessions must log in once with :func:`build_pikvm_credentials` and hold the session cookie
    instead, because these headers stop authenticating once the embedded TOTP expires.
    """

    user, password = build_pikvm_credentials(settings, totp_provider=totp_provider)
    return {
        "X-KVMD-User": user,
        "X-KVMD-Passwd": password,
        "User-Agent": USER_AGENT,
    }
