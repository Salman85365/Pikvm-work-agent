from __future__ import annotations

import argparse
import getpass
from collections.abc import Callable
from pathlib import Path

from work_agent.pikvm.errors import PiKVMConfigurationError
from work_agent.pikvm.profile_service import ProfileService, ProfileView


def add_profiles_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    profiles = subparsers.add_parser(
        "profiles",
        help="List, add, remove, enable, or disable named PiKVM profiles.",
    )
    actions = profiles.add_subparsers(dest="profiles_action", required=True)
    actions.add_parser("list", help="Show every .env and managed profile (no secrets).")

    add = actions.add_parser(
        "add",
        help="Add a managed profile; the password goes to macOS Keychain, never to a file.",
    )
    add.add_argument("name")
    add.add_argument("--url", required=True, help="PiKVM base URL, e.g. https://100.64.0.9")
    add.add_argument("--username", required=True)
    add.add_argument(
        "--no-2fa",
        dest="totp_required",
        action="store_false",
        help="This PiKVM does not require a TOTP code.",
    )
    add.add_argument(
        "--verify-ssl",
        action="store_true",
        help="Verify the PiKVM TLS certificate (most PiKVMs use a self-signed one).",
    )

    for action_name, help_text in (
        ("remove", "Remove a managed profile and its Keychain password."),
        ("enable", "Enable a profile for workflows, the scheduler, and the dashboard."),
        ("disable", "Skip a profile everywhere without deleting it."),
        ("test", "Log in and take one screenshot to prove the profile works."),
    ):
        parser = actions.add_parser(action_name, help=help_text)
        parser.add_argument("name")

    enroll = actions.add_parser(
        "enroll-2fa",
        help="Decode a PiKVM TOTP provisioning QR image locally and store its seed in Keychain.",
    )
    enroll.add_argument("name")
    enroll.add_argument("--qr", type=Path, required=True, help="PNG/JPEG of the provisioning QR.")
    enroll.add_argument("--replace", action="store_true", help="Replace an existing seed.")


def execute_profiles_command(
    args: argparse.Namespace,
    *,
    service: ProfileService | None = None,
    password_prompt: Callable[[str], str] | None = None,
) -> str:
    selected = service or ProfileService()
    action = args.profiles_action
    if action == "list":
        return format_profiles(selected.list_profiles())
    if action == "add":
        prompt = password_prompt or getpass.getpass
        password = prompt(f"PiKVM password for {args.name}: ")
        view = selected.add(
            name=args.name,
            url=args.url,
            username=args.username,
            password=password,
            totp_required=args.totp_required,
            verify_ssl=args.verify_ssl,
        )
        del password
        lines = [f"Added managed profile {view.name!r} for {view.host}."]
        if view.totp_required and not view.totp_enrolled:
            lines.append(
                "This PiKVM requires 2FA: enroll its provisioning QR with "
                f"`pikvm-agent profiles enroll-2fa {view.name} --qr IMAGE`."
            )
        return "\n".join(lines)
    if action == "remove":
        return "\n".join(selected.remove(args.name))
    if action in {"enable", "disable"}:
        view = selected.set_enabled(args.name, action == "enable")
        return f"Profile {view.name!r} is now {'enabled' if view.enabled else 'disabled'}."
    if action == "test":
        result = selected.test_connection(args.name)
        prefix = "OK" if result.ok else "FAILED"
        return f"{prefix}: {result.message}"
    if action == "enroll-2fa":
        try:
            image = args.qr.read_bytes()
        except OSError as exc:
            raise PiKVMConfigurationError(
                f"The QR image could not be read: {exc.strerror}."
            ) from None
        notes = selected.enroll_totp_from_image(args.name, image, replace_existing=args.replace)
        notes.append("Delete the QR image now; it contains the TOTP credential.")
        return "\n".join(notes)
    raise AssertionError(f"Unhandled profiles action: {action}")


def format_profiles(views: list[ProfileView]) -> str:
    if not views:
        return "No PiKVM profiles are configured. Add one with `pikvm-agent profiles add`."
    lines = []
    for view in views:
        state = "enabled" if view.enabled else "DISABLED"
        if view.totp_required:
            twofa = "2FA enrolled" if view.totp_enrolled else "2FA NOT ENROLLED"
        else:
            twofa = "no 2FA"
        lines.append(
            f"{view.name:<16} {state:<9} {view.source:<8} {view.host:<28} "
            f"{view.username:<12} {twofa}"
        )
    return "\n".join(lines)
