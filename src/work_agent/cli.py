from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from work_agent.pikvm import PiKVMClient, PiKVMError, PiKVMSettings


def _default_screenshot_path() -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path(f"pikvm-screenshot-{timestamp}.jpg")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pikvm-agent",
        description="Control PiKVM from this Mac. Milestone 1 is complete.",
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
    return parser


def _prompt_totp_code() -> str:
    try:
        return getpass.getpass("PiKVM 2FA code: ")
    except EOFError as exc:
        raise PiKVMError(
            "Could not read a 2FA code. Run this command in an interactive terminal."
        ) from exc


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "screenshot":
        output = args.output or _default_screenshot_path()
        try:
            settings = PiKVMSettings.from_env()
            totp_code = _prompt_totp_code() if settings.totp_required else None
            with PiKVMClient(settings, totp_code=totp_code) as client:
                screenshot = client.get_screenshot()
            saved_path = screenshot.save(output)
        except (PiKVMError, OSError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

        print(
            f"Saved {screenshot.size.width}x{screenshot.size.height} screenshot to "
            f"{saved_path.resolve()}"
        )
        return 0

    return 2


def main() -> None:
    raise SystemExit(run())
