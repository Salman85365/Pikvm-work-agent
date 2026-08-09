from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from work_agent import cli
from work_agent.pikvm import Screenshot, ScreenSize


class _FakeClient:
    received_totp_code: str | None = None

    def __init__(self, _: object, *, totp_code: str | None = None) -> None:
        type(self).received_totp_code = totp_code

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def get_screenshot(self) -> Screenshot:
        return Screenshot(
            content=b"jpeg bytes",
            size=ScreenSize(1280, 720),
            captured_at=datetime.now(UTC),
        )


def test_screenshot_command_saves_explicit_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "screen.jpg"
    settings = type("Settings", (), {"totp_required": True})()
    monkeypatch.setattr(cli.PiKVMSettings, "from_env", lambda: settings)
    monkeypatch.setattr(cli, "PiKVMClient", _FakeClient)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: "123456")

    exit_code = cli.run(["screenshot", "--output", str(output)])

    assert exit_code == 0
    assert output.read_bytes() == b"jpeg bytes"
    assert "1280x720" in capsys.readouterr().out
    assert _FakeClient.received_totp_code == "123456"


def test_screenshot_can_skip_totp_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "screen.jpg"
    settings = type("Settings", (), {"totp_required": False})()
    monkeypatch.setattr(cli.PiKVMSettings, "from_env", lambda: settings)
    monkeypatch.setattr(cli, "PiKVMClient", _FakeClient)

    def unexpected_prompt(_: str) -> str:
        raise AssertionError("2FA prompt should be disabled")

    monkeypatch.setattr(cli.getpass, "getpass", unexpected_prompt)

    assert cli.run(["screenshot", "--output", str(output)]) == 0
    assert _FakeClient.received_totp_code is None
