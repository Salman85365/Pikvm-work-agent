# PiKVM Work Agent

A local Python application that observes and controls a remote computer only through an existing
PiKVM connection. Nothing is installed on the remote computer.

## Current status: Milestone 4 manual validation pending

Milestones 1 through 3 are complete and have been validated against the real PiKVM from the controlling
Mac. Authentication, hidden interactive 2FA, screenshots, keyboard input, hotkeys, text entry,
absolute mouse movement, clicks, and scrolling have all been proven on hardware.

Real Milestone 3 tests correctly identified Slack from a saved 1920x1080 screenshot, localized its
profile control in normalized coordinates with a useful overlay, and identified Chrome from a live
PiKVM screenshot after the interactive 2FA flow.

Milestone 4 implements the bounded controller:

```text
observe -> analyze and verify -> plan one action -> local policy -> fresh-screen guard
-> one HID action -> local screen settle -> fresh observation
```

The planner returns exactly one strict `ActionProposal`; it cannot invoke PiKVM. A local policy,
human approvals, stale-screen comparison, hard step/runtime limits, repetition detection, and
post-action visual verification gate progress. HID requests remain non-retryable. Automated tests
pass, but Milestone 4 is not complete until the dry-run and controlled real-hardware phases below
have been inspected by the user.

## Requirements

- macOS with Python 3.11 or newer;
- network access from the Mac to PiKVM and the OpenAI API;
- a PiKVM user with access to snapshot and HID APIs;
- an OpenAI API key for screen-analysis commands.

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
`PIKVM_TOTP_REQUIRED=true` when PiKVM 2FA is enabled. `PIKVM_VERIFY_SSL=false` is compatible with
PiKVM's usual self-signed certificate, but a trusted certificate with verification enabled is
preferable.

For OpenAI perception and planning, also set:

```dotenv
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-5.6-luna
OPENAI_FALLBACK_MODEL=gpt-5.6-terra
OPENAI_SERVICE_TIER=default
OPENAI_REASONING_EFFORT=low
OPENAI_IMAGE_DETAIL=auto
OPENAI_STORE=false
OPENAI_REQUEST_TIMEOUT_SECONDS=30
OPENAI_MAX_RETRIES=2
OPENAI_ESCALATION_ENABLED=false
OPENAI_CONFIDENCE_THRESHOLD=0.80
OPENAI_VISION_MODEL=gpt-5.6-luna
OPENAI_VISION_SERVICE_TIER=default
OPENAI_VISION_REASONING_EFFORT=low
OPENAI_PLANNER_MODEL=gpt-5.6-terra
OPENAI_PLANNER_SERVICE_TIER=default
OPENAI_PLANNER_REASONING_EFFORT=low
```

Only `default` and `flex` service tiers are accepted. Supported reasoning efforts are `none`, `low`,
and `medium`; supported image details are `low`, `auto`, and `high`. The application rejects
`OPENAI_STORE=true` and always sends `store=False` to the Responses API.

`.env` is ignored by Git. Never commit an API key, PiKVM credential, TOTP code, real work
screenshot, or analysis overlay.

## Analyze an existing screenshot

Offline file analysis does not load PiKVM credentials, request PiKVM 2FA, or execute HID input:

```bash
pikvm-agent analyze-file pikvm-screen.jpg \
  --objective "Identify the visible application" \
  --json
```

Locate a relevant control and explicitly create a local overlay:

```bash
pikvm-agent analyze-file pikvm-screen.jpg \
  --objective "Locate the Slack profile control" \
  --overlay analysis-overlay.jpg \
  --json
```

The overlay draws relevant bounding boxes, labels, confidence, and the target click point. It is a
debugging artifact only; no click occurs. Use `--detail low|auto|high`,
`--reasoning-effort none|low|medium`, `--service-tier default|flex`, or `--model MODEL` to override
one run without changing `.env`.

## Analyze one live PiKVM screen

```bash
pikvm-agent analyze-screen \
  --objective "Identify the visible application" \
  --json
```

This loads the existing PiKVM configuration, securely prompts for a fresh hidden TOTP code when
required, captures exactly one screenshot, closes the PiKVM client, analyzes the JPEG in memory, and
exits. It does not save the screenshot unless `--overlay` is explicitly supplied, and it performs
no HID action.

Screen coordinates in structured output are integers from 0 through 1000. Pixel coordinates shown
by the human-readable CLI and overlay are local display conversions only. Confidence is an advisory
model signal; results below the configured threshold are marked unsafe and receive a
`low_confidence` warning.

## Capture a screenshot explicitly

```bash
pikvm-agent screenshot --output pikvm-screen.jpg
```

The terminal prompts without echoing the code:

```text
PiKVM 2FA code:
```

The six-digit code is kept only in memory for that authentication attempt and is never saved or
logged. Unlike `analyze-screen`, the screenshot command writes the image because saving is its
explicit purpose.

## Explicit HID commands

Milestone 2 provides one-operation commands:

```bash
pikvm-agent key Enter
pikvm-agent hotkey ControlLeft KeyL
pikvm-agent type "hello" --delay 0.05
pikvm-agent mouse-move 960 540 --screen-width 1920 --screen-height 1080
pikvm-agent click --button left
pikvm-agent scroll -120
```

Key names use PiKVM/DOM `KeyboardEvent.code` values. Mouse movement and clicking remain separate.
HID requests are never retried because a timeout can occur after PiKVM has already applied the
input. These commands are not connected to the OpenAI analyzer.

## Controlled agent commands

Inspect one complete proposal and policy decision with no HID:

```bash
pikvm-agent agent-step \
  --objective "Open Slack"
```

`agent-step` is dry-run by default. Add `--execute` to permit at most one locally confirmed HID
action followed by a fresh visual verification.

Run a bounded dry-run session:

```bash
pikvm-agent agent-run \
  --objective "Open Slack and stop when the main Slack window is visible" \
  --dry-run
```

The dry run captures one screen, analyzes it, plans one action, applies policy, prints what would
happen, and exits without HID. For explicitly requested local debug artifacts, add a Git-ignored
directory such as `--debug-dir .local-debug/session-001`. Those screenshots may contain private
work information.

Available controller safeguards include `--max-steps`, `--timeout`, `--approval-mode safe|every`,
`--step`, and model overrides. `safe` is the default approval mode. There is deliberately no
approve-everything mode, raw-coordinate autonomous action, shell action, power action, or direct
OpenAI-to-PiKVM tool path.

## Milestone 4 manual validation

Run these phases in order and inspect each result before proceeding.

Phase A — no HID:

```bash
pikvm-agent agent-run \
  --objective "Open Slack and stop when the main Slack window is visible" \
  --dry-run
```

Phase B — confirm every HID action and step through the controller:

```bash
pikvm-agent agent-run \
  --objective "Open Slack and stop when the main Slack window is visible" \
  --approval-mode every \
  --step \
  --max-steps 8
```

Phase C — allow only locally classified safe navigation automatically:

```bash
pikvm-agent agent-run \
  --objective "Open Slack and stop when the main Slack window is visible" \
  --approval-mode safe \
  --max-steps 8
```

Then use the second safe objective:

```bash
pikvm-agent agent-run \
  --objective "Open my Slack profile menu and stop when the menu is visible" \
  --approval-mode every \
  --step \
  --max-steps 6
```

Stop with the profile menu open. Do not select Update Status or send/read messages; those workflows
belong to later milestones.

## Private manual evaluation files

Put real screenshots and any expected-results manifest under `evals/private/`. That directory and
the standard screenshot/overlay names are ignored by Git. Unit tests use generated neutral images
and mocked OpenAI responses; they never spend API credits.

## Run automated checks

```bash
pytest
ruff check .
ruff format --check .
mypy
```

These checks do not contact PiKVM or OpenAI and do not send keyboard or mouse input.

## API references

- [PiKVM HTTP API](https://docs.pikvm.org/api/)
- [OpenAI Responses API](https://developers.openai.com/api/docs/guides/migrate-to-responses)
- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [OpenAI image inputs](https://developers.openai.com/api/docs/guides/images-vision)
