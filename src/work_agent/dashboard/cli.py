from __future__ import annotations

import argparse
import ipaddress
import secrets
import threading
import webbrowser

from work_agent.dashboard.errors import DashboardError

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8787


def _port(raw_value: str) -> int:
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1024 <= value <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1024 and 65535")
    return value


def add_dashboard_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    dashboard = subparsers.add_parser(
        "dashboard",
        help="Serve the Mac-local operations dashboard on loopback only.",
    )
    dashboard.add_argument(
        "--host",
        default=_DEFAULT_HOST,
        help="Loopback address to bind (default: 127.0.0.1).",
    )
    dashboard.add_argument(
        "--port",
        type=_port,
        default=_DEFAULT_PORT,
        help=f"TCP port to bind (default: {_DEFAULT_PORT}).",
    )
    dashboard.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open the dashboard URL automatically.",
    )


def _validate_host(host: str) -> str:
    candidate = host.strip()
    if candidate.lower() == "localhost":
        return _DEFAULT_HOST
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        raise DashboardError(
            "The dashboard host must be a loopback address such as 127.0.0.1. It can start and "
            "verify real HID workflows, so it is never exposed off this Mac."
        ) from None
    if not address.is_loopback:
        raise DashboardError(
            f"{candidate} is not a loopback address. The dashboard can start real HID workflows, "
            "so it only binds 127.0.0.1 or ::1."
        )
    return candidate


def execute_dashboard_command(args: argparse.Namespace) -> int:
    import uvicorn

    from work_agent.dashboard.app import create_app

    host = _validate_host(args.host)
    token = secrets.token_urlsafe(32)
    url = f"http://{host}:{args.port}/"

    print(f"PiKVM Work Agent dashboard: {url}")
    print("Loopback only. This session's token is embedded in the served page.")
    print("Press Ctrl+C to stop.")

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        uvicorn.run(
            create_app(token=token),
            host=host,
            port=args.port,
            log_level="warning",
            access_log=False,
        )
    except OSError as exc:
        raise DashboardError(
            f"The dashboard could not bind {host}:{args.port}. Another process may be using it."
        ) from exc
    return 0
