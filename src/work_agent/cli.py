from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from work_agent.agent.cli import (
    add_agent_parsers,
    execute_agent_command,
    format_session_result,
    result_exit_code,
)
from work_agent.agent.errors import AgentError
from work_agent.agent.pikvm_session import PiKVMSession
from work_agent.auth_cli import add_auth_parser, execute_auth_command
from work_agent.pikvm import (
    MouseButton,
    PiKVMError,
    PiKVMSettings,
    ScreenSize,
    TotpProvider,
    build_totp_provider,
)
from work_agent.schedule.cli import add_schedule_parser, execute_schedule_command
from work_agent.schedule.errors import ScheduleError
from work_agent.slack.cli import (
    add_slack_parser,
    execute_slack_command,
    format_availability_batch,
)
from work_agent.slack.errors import SlackAvailabilityError
from work_agent.vision import (
    AnalysisOptions,
    ImageDetail,
    OpenAIScreenAnalyzer,
    ReasoningEffort,
    ScreenAnalysis,
    ServiceTier,
    VisionError,
    VisionSettings,
    load_image,
    normalized_to_pixel,
    save_analysis_overlay,
)


def _default_screenshot_path() -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path(f"pikvm-screenshot-{timestamp}.jpg")


def _key_name(raw_value: str) -> str:
    value = raw_value.strip()
    if not value or "," in value or any(character.isspace() for character in value):
        raise argparse.ArgumentTypeError(
            "key names must be non-empty PiKVM/KeyboardEvent.code values without commas or spaces"
        )
    return value


def _typing_delay(raw_value: str) -> float:
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("typing delay must be a number") from exc
    if not 0 <= value <= 5:
        raise argparse.ArgumentTypeError("typing delay must be between 0 and 5 seconds")
    return value


def _screen_dimension(raw_value: str) -> int:
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("screen dimensions must be integers") from exc
    if value <= 1:
        raise argparse.ArgumentTypeError("screen dimensions must be greater than one pixel")
    return value


def _objective(raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        raise argparse.ArgumentTypeError("objective must not be empty")
    return value


def _model_name(raw_value: str) -> str:
    value = raw_value.strip()
    if not value or any(character.isspace() for character in value):
        raise argparse.ArgumentTypeError("model must be a non-empty identifier without whitespace")
    return value


def _add_analysis_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--objective",
        type=_objective,
        required=True,
        help="Visible-screen question or target to locate.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the complete validated analysis as JSON.",
    )
    parser.add_argument(
        "--overlay",
        type=Path,
        default=None,
        help="Explicitly save a local JPEG/PNG coordinate-debug overlay.",
    )
    parser.add_argument("--model", type=_model_name, default=None)
    parser.add_argument(
        "--service-tier",
        type=ServiceTier,
        choices=list(ServiceTier),
        default=None,
    )
    parser.add_argument(
        "--reasoning-effort",
        type=ReasoningEffort,
        choices=list(ReasoningEffort),
        default=None,
    )
    parser.add_argument(
        "--detail",
        type=ImageDetail,
        choices=list(ImageDetail),
        default=None,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pikvm-agent",
        description=(
            "Use PiKVM from this Mac for explicit HID operations, screen perception, "
            "and bounded controller sessions."
        ),
        epilog=(
            "Explicit HID commands act immediately and are never retried; "
            "analysis commands never act; agent actions are locally gated and verified."
        ),
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Named PiKVM profile from PIKVM_PROFILES (must precede the command).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    screenshot = subparsers.add_parser(
        "screenshot",
        help="Retrieve one PiKVM screenshot and save it locally.",
    )
    screenshot.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="JPEG destination (default: a timestamped file in the current directory).",
    )

    key = subparsers.add_parser(
        "key",
        help="Press and release one keyboard key.",
    )
    key.add_argument("key", type=_key_name, help="PiKVM/KeyboardEvent.code value, such as Enter.")

    hotkey = subparsers.add_parser(
        "hotkey",
        help="Press keys in order and release them in reverse order.",
    )
    hotkey.add_argument(
        "keys",
        nargs="+",
        type=_key_name,
        help="PiKVM/KeyboardEvent.code values, such as ControlLeft KeyL.",
    )

    type_text = subparsers.add_parser(
        "type",
        help="Type one exact text value.",
    )
    type_text.add_argument("text", help="Exact text to type; shell quoting may be required.")
    type_text.add_argument(
        "--keymap",
        default=None,
        help="Override PIKVM_KEYMAP for this operation.",
    )
    type_text.add_argument(
        "--delay",
        type=_typing_delay,
        default=0.0,
        help="Delay between characters in seconds, from 0 through 5 (default: 0).",
    )

    mouse_move = subparsers.add_parser(
        "mouse-move",
        help="Move the absolute mouse to screenshot pixel coordinates.",
    )
    mouse_move.add_argument("x", type=int, help="Horizontal screenshot pixel coordinate.")
    mouse_move.add_argument("y", type=int, help="Vertical screenshot pixel coordinate.")
    mouse_move.add_argument(
        "--screen-width",
        type=_screen_dimension,
        required=True,
        help="Width of the screenshot used for the coordinates.",
    )
    mouse_move.add_argument(
        "--screen-height",
        type=_screen_dimension,
        required=True,
        help="Height of the screenshot used for the coordinates.",
    )

    click = subparsers.add_parser(
        "click",
        help="Click once at the current mouse position.",
    )
    click.add_argument(
        "--button",
        type=MouseButton,
        choices=list(MouseButton),
        default=MouseButton.LEFT,
        help="Mouse button to click (default: left).",
    )

    scroll = subparsers.add_parser(
        "scroll",
        help="Send one mouse-wheel operation.",
    )
    scroll.add_argument("delta_y", type=int, help="Vertical wheel delta.")
    scroll.add_argument(
        "--delta-x",
        type=int,
        default=0,
        help="Horizontal wheel delta (default: 0).",
    )

    analyze_file = subparsers.add_parser(
        "analyze-file",
        help="Analyze one local screenshot without connecting to PiKVM.",
    )
    analyze_file.add_argument("image", type=Path, help="Local JPEG, PNG, or WebP screenshot.")
    _add_analysis_arguments(analyze_file)

    analyze_screen = subparsers.add_parser(
        "analyze-screen",
        help="Capture and analyze exactly one PiKVM screenshot without HID actions.",
    )
    _add_analysis_arguments(analyze_screen)
    add_auth_parser(subparsers)
    add_agent_parsers(subparsers)
    add_slack_parser(subparsers)
    add_schedule_parser(subparsers)
    return parser


def _prompt_totp_code(prompt: str = "PiKVM 2FA code: ") -> str:
    try:
        return getpass.getpass(prompt)
    except EOFError as exc:
        raise PiKVMError(
            "Could not read a 2FA code. Run this command in an interactive terminal."
        ) from exc


def _totp_provider(settings: PiKVMSettings) -> TotpProvider:
    return build_totp_provider(settings, interactive_prompt=_prompt_totp_code)


def _validate_command(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.command in {"slack", "schedule"} and args.profile is not None:
        parser.error(
            "--profile is not used by Slack workflows; pass --kvm or use the scheduled "
            "all-KVM flow."
        )
    if args.command == "type" and not args.text:
        parser.error("type text cannot be empty")
    if args.command == "mouse-move":
        if not 0 <= args.x < args.screen_width:
            parser.error(f"mouse x coordinate must be between 0 and {args.screen_width - 1}")
        if not 0 <= args.y < args.screen_height:
            parser.error(f"mouse y coordinate must be between 0 and {args.screen_height - 1}")
    if args.command == "scroll" and args.delta_x == 0 and args.delta_y == 0:
        parser.error("at least one scroll delta must be non-zero")


def _execute_pikvm_command(args: argparse.Namespace, client: PiKVMSession) -> str:
    if args.command == "screenshot":
        output = args.output or _default_screenshot_path()
        screenshot = client.get_screenshot()
        saved_path = screenshot.save(output)
        return (
            f"Saved {screenshot.size.width}x{screenshot.size.height} screenshot to "
            f"{saved_path.resolve()}"
        )
    if args.command == "key":
        client.press_key(args.key)
        return f"PiKVM accepted key press: {args.key}"
    if args.command == "hotkey":
        client.hotkey(*args.keys)
        return "PiKVM accepted hotkey: " + " + ".join(args.keys)
    if args.command == "type":
        client.type_text(args.text, keymap=args.keymap, delay=args.delay)
        return f"PiKVM accepted text input ({len(args.text)} characters)."
    if args.command == "mouse-move":
        screen_size = ScreenSize(width=args.screen_width, height=args.screen_height)
        client.move_mouse(args.x, args.y, screen_size=screen_size)
        return f"PiKVM accepted mouse move to ({args.x}, {args.y})."
    if args.command == "click":
        client.click(button=args.button)
        return f"PiKVM accepted one {args.button.value} click."
    if args.command == "scroll":
        client.scroll(args.delta_y, delta_x=args.delta_x)
        return f"PiKVM accepted scroll delta x={args.delta_x}, y={args.delta_y}."
    raise AssertionError(f"Unhandled command: {args.command}")


def _analysis_options(args: argparse.Namespace) -> AnalysisOptions:
    return AnalysisOptions(
        model=args.model,
        service_tier=args.service_tier,
        reasoning_effort=args.reasoning_effort,
        image_detail=args.detail,
    )


def _format_analysis(analysis: ScreenAnalysis) -> str:
    tier = analysis.service_tier or analysis.requested_service_tier.value
    warnings = ", ".join(warning.value for warning in analysis.warnings) or "none"
    lines = [
        f"Application: {analysis.application}",
        f"Screen state: {analysis.screen_state.value}",
        f"Summary: {analysis.summary}",
        f"Target found: {'yes' if analysis.target_found else 'no'}",
        f"Confidence: {analysis.confidence:.2f}",
        f"Safe to continue: {'yes' if analysis.safe_to_continue else 'no'}",
        f"Warnings: {warnings}",
    ]
    if analysis.stop_reason:
        lines.append(f"Stop reason: {analysis.stop_reason}")
    if analysis.target is not None:
        lines.append(
            f"Target: {analysis.target.label} ({analysis.target.role.value}, "
            f"confidence {analysis.target.confidence:.2f})"
        )
        if analysis.target.bounding_box is not None:
            box = analysis.target.bounding_box
            lines.append(f"Target box (normalized): ({box.x1}, {box.y1})-({box.x2}, {box.y2})")
        if analysis.target.click_point is not None:
            point = analysis.target.click_point
            pixel = normalized_to_pixel(
                point,
                width=analysis.screenshot_width,
                height=analysis.screenshot_height,
            )
            lines.append(f"Target point (normalized): ({point.x}, {point.y})")
            lines.append(f"Target point (pixels, display only): ({pixel.x}, {pixel.y})")
    lines.extend(
        [
            f"Model: {analysis.model}",
            f"Tier: {tier}",
            f"Detail: {analysis.image_detail.value}",
            f"Reasoning effort: {analysis.reasoning_effort.value}",
            f"Latency: {analysis.latency_seconds:.2f}s",
            f"Input tokens: {analysis.usage.input_tokens}",
            f"Output tokens: {analysis.usage.output_tokens}",
            f"Total tokens: {analysis.usage.total_tokens}",
            f"Retries: {analysis.retries}",
            f"Escalated: {'yes' if analysis.escalated else 'no'}",
        ]
    )
    return "\n".join(lines)


def _execute_analysis(args: argparse.Namespace) -> tuple[ScreenAnalysis, Path | None]:
    vision_settings = VisionSettings.from_env()
    analyzer = OpenAIScreenAnalyzer(vision_settings)

    if args.command == "analyze-file":
        image = load_image(args.image)
        content = image.content
        width = image.width
        height = image.height
    elif args.command == "analyze-screen":
        pikvm_settings = PiKVMSettings.from_env(args.profile)
        with PiKVMSession(
            pikvm_settings,
            totp_provider=_totp_provider(pikvm_settings),
        ) as session:
            screenshot = session.get_screenshot()
        content = screenshot.content
        width = screenshot.size.width
        height = screenshot.size.height
    else:
        raise AssertionError(f"Unhandled analysis command: {args.command}")

    analysis = analyzer.analyze(
        content,
        objective=args.objective,
        width=width,
        height=height,
        options=_analysis_options(args),
    )
    overlay_path = None
    if args.overlay is not None:
        overlay_path = save_analysis_overlay(content, analysis, args.overlay)
    return analysis, overlay_path


def run(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_command(parser, args)

    if args.command == "slack":
        try:
            availability_result = execute_slack_command(args)
        except (SlackAvailabilityError, PiKVMError, OSError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(format_availability_batch(availability_result))
        return 0 if availability_result.success else 1

    if args.command == "schedule":
        try:
            schedule_result, exit_code = execute_schedule_command(args)
        except (ScheduleError, SlackAvailabilityError, PiKVMError, OSError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(schedule_result)
        return exit_code

    if args.command in {"agent-run", "agent-step"}:
        try:
            agent_result = execute_agent_command(args)
        except KeyboardInterrupt:
            print("Agent interrupted before the controller started; no HID action was issued.")
            return 2
        except (AgentError, VisionError, PiKVMError, OSError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(format_session_result(agent_result))
        return result_exit_code(agent_result)

    if args.command in {"analyze-file", "analyze-screen"}:
        try:
            analysis, overlay_path = _execute_analysis(args)
        except (VisionError, PiKVMError, OSError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(analysis.model_dump_json(indent=2))
        else:
            print(_format_analysis(analysis))
        if overlay_path is not None:
            print(f"Saved analysis overlay to {overlay_path.resolve()}", file=sys.stderr)
        return 0

    if args.command == "auth":
        try:
            auth_result = execute_auth_command(args)
        except (PiKVMError, OSError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(auth_result)
        return 0

    try:
        settings = PiKVMSettings.from_env(args.profile)
        with PiKVMSession(settings, totp_provider=_totp_provider(settings)) as session:
            result = _execute_pikvm_command(args, session)
    except (PiKVMError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(result)
    return 0


def main() -> None:
    raise SystemExit(run())
