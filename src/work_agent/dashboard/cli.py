from __future__ import annotations

import argparse
import ipaddress
import secrets
import socket
import threading
import webbrowser
from collections.abc import Callable

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
    """Return a literal loopback address (any 127/8 address, or ::1) for the given host."""

    candidate = host.strip()
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
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
    return str(address)


def dashboard_url(host: str, port: int) -> str:
    """Format the served URL, bracketing an IPv6 literal so browsers accept it."""

    return f"http://[{host}]:{port}/" if ":" in host else f"http://{host}:{port}/"


def bind_dashboard_socket(host: str, port: int) -> socket.socket:
    """Bind the listening socket up front so a taken port fails before anything else starts."""

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    listener = socket.socket(family, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(128)
        listener.set_inheritable(True)
    except OSError as exc:
        listener.close()
        raise DashboardError(
            f"The dashboard could not bind {dashboard_url(host, port)}. Another process may be "
            "using that port; pass --port to choose another."
        ) from exc
    return listener


def execute_dashboard_command(
    args: argparse.Namespace,
    *,
    open_browser: Callable[[str], object] | None = None,
) -> int:
    import uvicorn

    from work_agent.dashboard.app import create_app

    host = _validate_host(args.host)
    token = secrets.token_urlsafe(32)
    url = dashboard_url(host, args.port)

    # Bind before printing or opening anything: an EADDRINUSE must not pop a browser tab at a
    # port some other process (possibly an older dashboard) is answering.
    listener = bind_dashboard_socket(host, args.port)

    print(f"PiKVM Work Agent dashboard: {url}")
    print("Loopback only. This session's token is embedded in the served page.")
    print("Press Ctrl+C to stop.")

    launch = open_browser or webbrowser.open
    if not args.no_browser:
        threading.Timer(0.8, lambda: launch(url)).start()

    config = uvicorn.Config(
        create_app(token=token),
        host=host,
        port=args.port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    try:
        server.run(sockets=[listener])
    except SystemExit as exc:  # uvicorn exits the interpreter on a startup failure.
        raise DashboardError(
            f"The dashboard server could not start on {url} (exit status {exc.code})."
        ) from None
    except OSError as exc:
        raise DashboardError(f"The dashboard could not serve {url}: {exc.strerror}") from exc
    finally:
        listener.close()
    return 0
