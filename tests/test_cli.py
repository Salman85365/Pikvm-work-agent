from __future__ import annotations

import json
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import ClassVar

import pytest
from PIL import Image

from work_agent import cli
from work_agent.pikvm import MouseButton, PiKVMTimeoutError, Screenshot, ScreenSize
from work_agent.slack.models import Availability, AvailabilityBatchResult, AvailabilityResult
from work_agent.vision import (
    AnalysisUsage,
    ImageDetail,
    ReasoningEffort,
    ScreenAnalysis,
    ScreenState,
    ServiceTier,
)


class _FakeClient:
    received_totp_code: str | None = None
    calls: ClassVar[list[tuple[str, tuple[object, ...], dict[str, object]]]] = []

    def __init__(self, settings: object, *, totp_provider: object) -> None:
        type(self).received_totp_code = (
            totp_provider.current_code() if settings.totp_required else None
        )

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def get_screenshot(self) -> Screenshot:
        type(self).calls.append(("get_screenshot", (), {}))
        return Screenshot(
            content=b"jpeg bytes",
            size=ScreenSize(1280, 720),
            captured_at=datetime.now(UTC),
        )

    def press_key(self, key: str) -> None:
        type(self).calls.append(("press_key", (key,), {}))

    def hotkey(self, *keys: str) -> None:
        type(self).calls.append(("hotkey", keys, {}))

    def type_text(
        self,
        text: str,
        *,
        keymap: str | None = None,
        delay: float = 0.0,
    ) -> None:
        type(self).calls.append(("type_text", (text,), {"keymap": keymap, "delay": delay}))

    def move_mouse(self, x: int, y: int, *, screen_size: ScreenSize) -> None:
        type(self).calls.append(("move_mouse", (x, y), {"screen_size": screen_size}))

    def click(self, *, button: MouseButton = MouseButton.LEFT) -> None:
        type(self).calls.append(("click", (), {"button": button}))

    def scroll(self, delta_y: int, *, delta_x: int = 0) -> None:
        type(self).calls.append(("scroll", (delta_y,), {"delta_x": delta_x}))


class _FailingClient(_FakeClient):
    received_totp_code: str | None = None
    calls: ClassVar[list[tuple[str, tuple[object, ...], dict[str, object]]]] = []

    def press_key(self, key: str) -> None:
        super().press_key(key)
        raise PiKVMTimeoutError("PiKVM timed out; the HID outcome is uncertain.")


class _TotpProvider:
    def __init__(self, code: str = "123456") -> None:
        self.code = code

    def current_code(self) -> str:
        return self.code


class _FakeAnalyzer:
    settings: ClassVar[object | None] = None
    calls: ClassVar[list[dict[str, object]]] = []

    def __init__(self, settings: object) -> None:
        type(self).settings = settings

    def analyze(
        self,
        screenshot: bytes,
        *,
        objective: str,
        width: int,
        height: int,
        options: object,
    ) -> ScreenAnalysis:
        type(self).calls.append(
            {
                "screenshot": screenshot,
                "objective": objective,
                "width": width,
                "height": height,
                "options": options,
            }
        )
        return ScreenAnalysis(
            objective=objective,
            application="Slack",
            screen_state=ScreenState.APPLICATION,
            summary="Slack is visible.",
            target_found=False,
            target=None,
            relevant_elements=[],
            warnings=[],
            safe_to_continue=True,
            stop_reason=None,
            confidence=0.94,
            screenshot_width=width,
            screenshot_height=height,
            requested_model="model-override",
            model="model-actual",
            requested_service_tier=ServiceTier.FLEX,
            service_tier="flex",
            image_detail=ImageDetail.HIGH,
            reasoning_effort=ReasoningEffort.NONE,
            usage=AnalysisUsage(
                input_tokens=10,
                cached_input_tokens=1,
                cache_write_tokens=0,
                output_tokens=5,
                reasoning_tokens=2,
                total_tokens=15,
            ),
            latency_seconds=0.5,
            retries=0,
            escalated=False,
            attempted_models=["model-override"],
        )


def _configure_cli(
    monkeypatch: pytest.MonkeyPatch,
    *,
    totp_required: bool = True,
    client_type: type[_FakeClient] = _FakeClient,
) -> None:
    settings = type("Settings", (), {"totp_required": totp_required})()
    client_type.received_totp_code = None
    client_type.calls = []
    monkeypatch.setattr(cli.PiKVMSettings, "from_env", lambda _profile=None: settings)
    monkeypatch.setattr(cli, "_totp_provider", lambda _: _TotpProvider())
    monkeypatch.setattr(cli, "PiKVMSession", client_type)


def _configure_vision_cli(monkeypatch: pytest.MonkeyPatch) -> object:
    settings = object()
    _FakeAnalyzer.settings = None
    _FakeAnalyzer.calls = []
    monkeypatch.setattr(cli.VisionSettings, "from_env", lambda: settings)
    monkeypatch.setattr(cli, "OpenAIScreenAnalyzer", _FakeAnalyzer)
    return settings


def _write_test_image(path: Path) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (80, 40), "white").save(buffer, format="PNG")
    content = buffer.getvalue()
    path.write_bytes(content)
    return content


def test_screenshot_command_saves_explicit_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "screen.jpg"
    _configure_cli(monkeypatch)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: "123456")

    exit_code = cli.run(["screenshot", "--output", str(output)])

    assert exit_code == 0
    assert output.read_bytes() == b"jpeg bytes"
    assert "1280x720" in capsys.readouterr().out
    assert _FakeClient.received_totp_code == "123456"
    assert _FakeClient.calls == [("get_screenshot", (), {})]


def test_screenshot_can_skip_totp_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "screen.jpg"
    _configure_cli(monkeypatch, totp_required=False)

    def unexpected_prompt(_: str) -> str:
        raise AssertionError("2FA prompt should be disabled")

    monkeypatch.setattr(cli.getpass, "getpass", unexpected_prompt)

    assert cli.run(["screenshot", "--output", str(output)]) == 0
    assert _FakeClient.received_totp_code is None


def test_global_profile_is_propagated_to_pikvm_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "profile-screen.jpg"
    settings = type("Settings", (), {"totp_required": False})()
    selected_profiles: list[str | None] = []
    _FakeClient.received_totp_code = None
    _FakeClient.calls = []
    monkeypatch.setattr(
        cli.PiKVMSettings,
        "from_env",
        lambda profile=None: selected_profiles.append(profile) or settings,
    )
    monkeypatch.setattr(cli, "_totp_provider", lambda _: _TotpProvider())
    monkeypatch.setattr(cli, "PiKVMSession", _FakeClient)

    exit_code = cli.run(["--profile", "lab-kvm", "screenshot", "--output", str(output)])

    assert exit_code == 0
    assert selected_profiles == ["lab-kvm"]
    assert _FakeClient.received_totp_code is None
    assert _FakeClient.calls == [("get_screenshot", (), {})]


@pytest.mark.parametrize(
    ("argv", "expected_call"),
    [
        (["key", "Enter"], ("press_key", ("Enter",), {})),
        (
            ["hotkey", "ControlLeft", "KeyL"],
            ("hotkey", ("ControlLeft", "KeyL"), {}),
        ),
        (
            ["type", "hello world", "--keymap", "de", "--delay", "0.25"],
            ("type_text", ("hello world",), {"keymap": "de", "delay": 0.25}),
        ),
        (
            [
                "mouse-move",
                "960",
                "540",
                "--screen-width",
                "1920",
                "--screen-height",
                "1080",
            ],
            (
                "move_mouse",
                (960, 540),
                {"screen_size": ScreenSize(1920, 1080)},
            ),
        ),
        (
            ["click", "--button", "right"],
            ("click", (), {"button": MouseButton.RIGHT}),
        ),
        (
            ["scroll", "-120", "--delta-x", "10"],
            ("scroll", (-120,), {"delta_x": 10}),
        ),
    ],
)
def test_hid_commands_dispatch_exactly_once(
    argv: list[str],
    expected_call: tuple[str, tuple[object, ...], dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_cli(monkeypatch)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: "123456")

    assert cli.run(argv) == 0

    assert _FakeClient.received_totp_code == "123456"
    assert _FakeClient.calls == [expected_call]


def test_type_command_does_not_echo_text(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    text = "sensitive text"
    _configure_cli(monkeypatch, totp_required=False)

    assert cli.run(["type", text]) == 0

    captured = capsys.readouterr()
    assert text not in captured.out
    assert text not in captured.err
    assert "14 characters" in captured.out


@pytest.mark.parametrize(
    "argv",
    [
        ["key", "Bad,Key"],
        ["key", "Bad Key"],
        ["hotkey", "ControlLeft,KeyL"],
        ["type", ""],
        ["type", "text", "--delay", "5.1"],
        [
            "mouse-move",
            "1920",
            "0",
            "--screen-width",
            "1920",
            "--screen-height",
            "1080",
        ],
        [
            "mouse-move",
            "0",
            "-1",
            "--screen-width",
            "1920",
            "--screen-height",
            "1080",
        ],
        [
            "mouse-move",
            "0",
            "0",
            "--screen-width",
            "1",
            "--screen-height",
            "1080",
        ],
        ["scroll", "0"],
    ],
)
def test_invalid_hid_arguments_fail_before_authentication(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_settings() -> object:
        raise AssertionError("Invalid input must not load credentials")

    monkeypatch.setattr(cli.PiKVMSettings, "from_env", unexpected_settings)

    with pytest.raises(SystemExit) as error:
        cli.run(argv)

    assert error.value.code == 2


def test_hid_failure_is_reported_without_another_attempt(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure_cli(monkeypatch, totp_required=False, client_type=_FailingClient)

    assert cli.run(["key", "Enter"]) == 1

    assert _FailingClient.calls == [("press_key", ("Enter",), {})]
    assert "outcome is uncertain" in capsys.readouterr().err


def test_analyze_file_does_not_load_pikvm_or_prompt_for_2fa(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    image_path = tmp_path / "neutral.png"
    image_content = _write_test_image(image_path)
    vision_settings = _configure_vision_cli(monkeypatch)

    def unexpected_pikvm_settings() -> object:
        raise AssertionError("offline analysis must not load PiKVM credentials")

    def unexpected_prompt(_: str) -> str:
        raise AssertionError("offline analysis must not prompt for PiKVM 2FA")

    monkeypatch.setattr(cli.PiKVMSettings, "from_env", unexpected_pikvm_settings)
    monkeypatch.setattr(cli.getpass, "getpass", unexpected_prompt)

    exit_code = cli.run(
        [
            "analyze-file",
            str(image_path),
            "--objective",
            "Identify the visible application",
            "--model",
            "model-override",
            "--service-tier",
            "flex",
            "--reasoning-effort",
            "none",
            "--detail",
            "high",
            "--json",
        ]
    )

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["application"] == "Slack"
    assert _FakeAnalyzer.settings is vision_settings
    assert len(_FakeAnalyzer.calls) == 1
    call = _FakeAnalyzer.calls[0]
    assert call["screenshot"] == image_content
    assert (call["width"], call["height"]) == (80, 40)
    options = call["options"]
    assert options.model == "model-override"
    assert options.service_tier is ServiceTier.FLEX
    assert options.reasoning_effort is ReasoningEffort.NONE
    assert options.image_detail is ImageDetail.HIGH


def test_analyze_screen_captures_once_and_never_calls_hid(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure_vision_cli(monkeypatch)
    _configure_cli(monkeypatch, totp_required=True)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: "123456")

    exit_code = cli.run(
        [
            "analyze-screen",
            "--objective",
            "Identify the visible application",
            "--json",
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["screen_state"] == "application"
    assert _FakeClient.received_totp_code == "123456"
    assert _FakeClient.calls == [("get_screenshot", (), {})]
    assert len(_FakeAnalyzer.calls) == 1
    assert _FakeAnalyzer.calls[0]["screenshot"] == b"jpeg bytes"


def test_slack_availability_command_routes_without_generic_pikvm_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    received: list[object] = []

    def execute(args: object) -> AvailabilityBatchResult:
        received.append(args)
        return AvailabilityBatchResult(
            results=(
                AvailabilityResult(
                    kvm="work-kvm",
                    desired=Availability.ACTIVE,
                    observed=Availability.ACTIVE,
                    changed=False,
                    success=True,
                ),
            )
        )

    monkeypatch.setattr(cli, "execute_slack_command", execute)
    monkeypatch.setattr(
        cli.PiKVMSettings,
        "from_env",
        lambda *_: (_ for _ in ()).throw(AssertionError("must use Slack workflow")),
    )

    exit_code = cli.run(["slack", "availability", "set", "active", "--kvm", "work-kvm"])

    assert exit_code == 0
    assert received[0].availability is Availability.ACTIVE
    assert received[0].kvm == "work-kvm"
    assert "already set; no-op" in capsys.readouterr().out


def test_schedule_command_routes_and_preserves_nonzero_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli,
        "execute_schedule_command",
        lambda args: (f"schedule action: {args.schedule_action}", 1),
    )

    exit_code = cli.run(["schedule", "slack-availability", "status"])

    assert exit_code == 1
    assert "schedule action: status" in capsys.readouterr().out


def test_slack_and_schedule_reject_ambiguous_global_profile() -> None:
    with pytest.raises(SystemExit) as error:
        cli.run(
            [
                "--profile",
                "work-kvm",
                "slack",
                "availability",
                "get",
                "--kvm",
                "work-kvm",
            ]
        )

    assert error.value.code == 2
