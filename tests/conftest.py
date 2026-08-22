from __future__ import annotations

from pathlib import Path

import dotenv.main
import keyring
import keyring.backend
import pytest


class _MemoryKeyring(keyring.backend.KeyringBackend):
    """In-process keyring so no test can read or write the real macOS Keychain."""

    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.items.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.items[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.items:
            raise keyring.errors.PasswordDeleteError(username)
        del self.items[(service, username)]


@pytest.fixture(autouse=True)
def _isolated_managed_profiles(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep every test away from the real ~/Library managed-profile file and Keychain."""

    monkeypatch.setenv("PIKVM_AGENT_PROFILES_FILE", str(tmp_path / "profiles.json"))
    # The developer's real .env must never shape a test: load_dotenv() finds nothing.
    monkeypatch.setattr(dotenv.main, "find_dotenv", lambda *args, **kwargs: "")
    previous = keyring.get_keyring()
    keyring.set_keyring(_MemoryKeyring())
    yield
    keyring.set_keyring(previous)
