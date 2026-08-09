# PiKVM Work Agent

A local Python application that will observe and control a remote computer only through an
existing PiKVM connection. Nothing is installed on the remote computer.

## Current status: Milestone 1 complete

Milestone 1 contains:

- a typed, synchronous `PiKVMClient` built on PiKVM's documented HTTP API;
- screenshot capture and explicit save support;
- keyboard, text, absolute-mouse, click, double-click, and scroll transport abstractions;
- environment-only credential loading with `.env` convenience support;
- secure, per-command terminal prompting for PiKVM's current TOTP code;
- timeouts and bounded retries for screenshot reads;
- explicit, sanitized configuration, authentication, timeout, connection, HTTP, and protocol
  errors;
- a single non-LLM CLI command: `pikvm-agent screenshot`;
- hardware-independent tests using an in-memory HTTP transport.

HID methods exist at the transport layer for the later milestones, but this milestone does not
expose HID CLI commands and does not invoke them automatically. OpenAI, screen understanding,
planning, policy, controller, verification, logging, and Slack workflow code are deliberately not
included yet.

The screenshot command has been successfully validated against the real PiKVM from the controlling
Mac, including the hidden interactive 2FA flow. PiKVM HID operations have not yet been tested on
real hardware. Milestone 2 will expose and verify those operations incrementally through explicit,
non-autonomous CLI commands.

## Requirements

- macOS with Python 3.11 or newer
- network access from the Mac to PiKVM
- a PiKVM user with access to the snapshot and HID APIs

## Install on the Mac

```bash
cd pikvm-work-agent
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
cp .env.example .env
```

Edit `.env` and set `PIKVM_URL`, `PIKVM_USERNAME`, and `PIKVM_PASSWORD`. Leave
`PIKVM_TOTP_REQUIRED=true` when PiKVM 2FA is enabled. The `.env` file is ignored by Git.
`PIKVM_VERIFY_SSL=false` is compatible with PiKVM's usual self-signed certificate, but a trusted
certificate with verification enabled is preferable.

## Capture the first screenshot

```bash
pikvm-agent screenshot --output pikvm-screen.jpg
```

The terminal will then prompt without echoing the code:

```text
PiKVM 2FA code:
```

Enter the current six-digit code from the authenticator and press Return. The code is validated,
appended to the password only in memory as required by PiKVM, and is neither saved nor logged. A
fresh code is requested for every command. If PiKVM does not use 2FA, set
`PIKVM_TOTP_REQUIRED=false`.

The command makes one authenticated request to PiKVM's `/api/streamer/snapshot` endpoint, validates
that the response is a decodable JPEG, and writes it only to the explicitly requested path.

## Run automated checks

```bash
pytest
ruff check .
mypy
```

These checks do not contact PiKVM and do not send keyboard or mouse input.

## Transport example

The public HID names are PiKVM/DOM `KeyboardEvent.code` values such as `Enter`, `ControlLeft`, and
`KeyL`.

```python
from work_agent.pikvm import PiKVMClient, PiKVMSettings, ScreenSize

settings = PiKVMSettings.from_env()

with PiKVMClient(settings) as pikvm:
    screenshot = pikvm.get_screenshot()
    screenshot.save("pikvm-screen.jpg")

    # Reserved for explicitly authorized future workflows:
    pikvm.press_key("Enter")
    pikvm.hotkey("ControlLeft", "KeyL")
    pikvm.type_text("hello")
    pikvm.click(1200, 600, screen_size=ScreenSize(1920, 1080))
```

Mouse coordinates in the public API are screenshot pixels. They are bounds-checked and converted to
PiKVM's signed absolute-HID coordinate range. HID requests are intentionally not retried: retrying a
timed-out click or key press could duplicate an action.

## API reference

The transport follows the official [PiKVM HTTP API](https://docs.pikvm.org/api/), including the
snapshot route and HTTP HID event routes.
