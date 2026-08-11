# PiKVM Work Agent

A local Python application that observes and controls a remote computer only through an existing
PiKVM connection. Nothing is installed on the remote computer.

## Current status: Milestone 5 implementation complete; real multi-KVM validation pending

Milestones 1 through 4.5 are complete and have been validated against the real PiKVM/OpenAI path from
the controlling Mac. Authentication, screenshots, explicit HID operations, screen perception, and
the bounded observe/reason/act/verify controller have all been proven on hardware.

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
pass.

Milestone 4.5 adds automatic local RFC 6238 TOTP generation. Each PiKVM seed is stored in a separate
generic-password item in macOS Keychain and selected by normalized PiKVM hostname. Enrollment now
decodes the PiKVM provisioning QR entirely on the Mac and verifies it with a harmless PiKVM read.
Real Keychain enrollment and hardware verification succeeded. Never paste a real TOTP seed or
provisioning URI into Codex, ChatGPT, a Git file, or any OpenAI prompt.

Milestone 5 adds one bounded Slack GUI workflow for reading and setting manual Active/Away
availability. It uses the existing controller, visually verifies the menu's current-state toggle,
processes named KVMs sequentially, and records only sanitized operation metadata. A Mac-local
launchd schedule applies Active Monday-Friday at 18:00 and Away Tuesday-Saturday at 02:00 in
Asia/Karachi, with deterministic reconciliation for missed events. Automated validation passes;
real Slack transitions across the configured KVMs must still be performed by the user before the
milestone is marked complete.

## Requirements

- macOS with Python 3.11 or newer;
- access to the user's macOS login Keychain;
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

Configure one or more named PiKVM profiles in `.env`. `PIKVM_PROFILE` selects the default:

```dotenv
PIKVM_PROFILES=heidrick,lab-kvm
PIKVM_PROFILE=heidrick

PIKVM_HEIDRICK_URL=https://heidrick-pikvm.example.local/kvm
PIKVM_HEIDRICK_USERNAME=admin
PIKVM_HEIDRICK_PASSWORD=change-me
PIKVM_HEIDRICK_TOTP_REQUIRED=true
PIKVM_HEIDRICK_TOTP_PROVIDER=keychain
PIKVM_HEIDRICK_TOTP_INTERACTIVE_FALLBACK=false
PIKVM_HEIDRICK_VERIFY_SSL=false

PIKVM_LAB_KVM_URL=https://lab-pikvm.example.local
PIKVM_LAB_KVM_USERNAME=admin
PIKVM_LAB_KVM_PASSWORD=change-me
PIKVM_LAB_KVM_TOTP_REQUIRED=false
PIKVM_LAB_KVM_VERIFY_SSL=false
```

Profile names are case-insensitive slugs containing letters, numbers, hyphens, or underscores. A
hyphen becomes an underscore in environment-variable names, so `lab-kvm` uses the
`PIKVM_LAB_KVM_` prefix. Each profile has independent credentials, 2FA policy, TLS verification,
timeouts, retry settings, and keymap. A trusted certificate with verification enabled is
preferable; local self-signed devices commonly require their profile's `VERIFY_SSL=false`.

There is intentionally no TOTP-secret environment setting. Profiles that require 2FA use an
exact-host Keychain entry; profiles with `TOTP_REQUIRED=false` never read that entry or prompt for a
code. Legacy `PIKVM_URL`, `PIKVM_USERNAME`, and related single-KVM variables remain supported only
when neither `PIKVM_PROFILES` nor `PIKVM_PROFILE` is configured.

Use the default profile by omitting a selector, or override it for one invocation. The global option
must precede the command:

```bash
pikvm-agent screenshot --output heidrick-screen.jpg
pikvm-agent --profile lab-kvm screenshot --output lab-screen.jpg
```

When multiple profiles are listed and no default is configured, the CLI stops and requires
`--profile`; it never guesses which KVM should receive credentials or HID input.

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

## Enroll automatic PiKVM TOTP

Place or crop the existing PiKVM TOTP provisioning QR into the Git-ignored `.local-secrets/`
directory, then import it. QR detection, provisioning-URI parsing, code generation, and Keychain
storage all happen locally; the image, URI, seed, and generated code are never sent to OpenAI. Run
this once for each profile that has `TOTP_REQUIRED=true`:

```bash
pikvm-agent --profile heidrick auth keychain import-qr \
  --qr .local-secrets/heidrick.totp-qr.png \
  --delete-qr-after-success
pikvm-agent --profile heidrick auth keychain status
pikvm-agent --profile heidrick auth keychain verify
```

The import accepts PNG and JPEG files containing exactly one QR code, requires a standard
six-digit, 30-second, SHA-1 `otpauth://totp` credential, reads the stored entry back, generates a
code locally, and authenticates to PiKVM through the existing read-only screenshot path. It never
silently overwrites an entry. `--delete-qr-after-success` deletes only the supplied image and only
after all those checks succeed. Without that option, the command leaves the image in place and
reminds you to remove it.

`.local-secrets/` and conventional `*.totp-qr.png`/`.jpg` names are ignored by Git, but Git ignore
is not encryption or secure storage. A provisioning QR is equivalent to possession of the TOTP
setup credential. Keychain is the permanent runtime store; the application never reads the QR
again after enrollment.

The default Keychain convention is:

```text
service: pikvm-work-agent.totp
account: heidrick-pikvm.example.local
```

Schemes, paths, queries, fragments, and default ports are excluded from the account. A non-default
port is retained, so separate endpoints and profiles do not accidentally share a seed. The selected
profile supplies the host. To prevent credentials crossing endpoints, a named profile cannot be
combined with a different auth `--host`; the option remains available for legacy single-KVM use.

Removal affects only the exact local generic-password entry and defaults to no:

```bash
pikvm-agent --profile heidrick auth keychain remove
```

It does not disable PiKVM 2FA, modify the PiKVM password, or touch Apple Passwords/another
authenticator. Arbitrary URLs, HOTP, empty/malformed secrets, ambiguous multiple QRs, and unsupported
TOTP parameters are rejected with sanitized errors.

If QR import is unavailable, the optional manual fallback uses a hidden terminal prompt and accepts
either Base32 or a provisioning URI. The value is never accepted as a command-line argument:

```bash
pikvm-agent --profile heidrick auth keychain setup-secret
```

For explicit manual debugging, set the selected profile's `TOTP_PROVIDER=interactive` to use the
existing hidden `PiKVM 2FA code:` prompt. Alternatively, keychain mode may use that prompt only
when that profile's `TOTP_INTERACTIVE_FALLBACK=true`. Fallback defaults to false so an unattended
session stops cleanly rather than hanging for input.

Automatic TOTP intentionally gives the Mac programmatic access to both the PiKVM password source
and the TOTP seed. Protecting the Mac and macOS account is therefore especially important. This
does not weaken or disable PiKVM server-side 2FA.

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

This loads the existing PiKVM configuration, obtains a fresh TOTP locally from the configured
provider, captures exactly one screenshot, closes the PiKVM client, analyzes the JPEG in memory, and
exits. It does not save the screenshot unless `--overlay` is explicitly supplied, and it performs
no HID action. With a working Keychain entry there is no terminal TOTP prompt.

Screen coordinates in structured output are integers from 0 through 1000. Pixel coordinates shown
by the human-readable CLI and overlay are local display conversions only. Confidence is an advisory
model signal; results below the configured threshold are marked unsafe and receive a
`low_confidence` warning.

## Capture a screenshot explicitly

```bash
pikvm-agent screenshot --output pikvm-screen.jpg
```

The configured Keychain seed is used to generate the current code locally; neither value is printed
or logged. Unlike `analyze-screen`, the screenshot command writes the image because saving is its
explicit purpose. If a safe read discovers expired authentication, it creates one fresh client/code
and retries once.

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
input. Authentication failure or transport ambiguity during HID also never replays input. These
commands are not connected to the OpenAI analyzer.

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

## Slack manual availability

Read or set one named KVM through Slack's visible GUI:

```bash
pikvm-agent slack availability get --kvm heidrick
pikvm-agent slack availability set active --kvm heidrick
pikvm-agent slack availability set away --kvm heidrick
```

Apply a requested state to every `PIKVM_PROFILES` entry in declaration order:

```bash
pikvm-agent slack availability set active --all-kvms
```

Each KVM runs to completion before the next starts. One failure is reported but does not stop later
profiles. The workflow opens Slack/profile navigation as needed, checks the manual availability
toggle first, avoids a click when the state is already correct, and accepts success only when a
fresh observation visibly proves the resulting state. It uses no fixed coordinates and cannot read
or send messages, edit Slack status text/emoji, change preferences, or simulate activity. An
unexpected approval requirement stops that KVM rather than blocking an unattended schedule.
When Slack is foreground but its profile menu is not visible, the workflow performs a focused
vision pass for the small account/avatar control. The focused point must be inside the returned
profile bounding box before it can reach policy or HID; no fixed coordinate is used. An invalid
focused result receives at most one retry with the configured fallback model at medium reasoning.
An ambiguous low-confidence/unknown screen initially classified as unsafe similarly receives one
read-only fallback-model confirmation. Authentication, lock, unexpected-dialog, destructive, and
disconnect warnings remain immediate stops and are never overridden.
If Slack applies a toggle with too little full-screen change for generic verification, the workflow
performs at most one separate read-only pass that reopens the profile menu and interprets its manual
toggle. It never repeats the availability click during that verification pass.

For manual diagnosis, add `--trace` to a direct availability command. It prints only sanitized
controller states, action types, resolved normalized/pixel coordinates, policy decisions, and
verification outcomes; it does not print screenshots or visible Slack content.

Sanitized JSONL operation records are stored at
`~/Library/Logs/pikvm-work-agent/slack-availability.jsonl`. They contain the KVM name, requested and
observed availability, changed/no-op result, outcome, and sanitized error. Screenshots, visible
Slack content, credentials, TOTP values, and API keys are never written there.

## Mac-local Slack availability schedule

Inspect and exercise reconciliation before installing anything:

```bash
pikvm-agent schedule slack-availability run-now
pikvm-agent schedule slack-availability reconcile
```

`run-now` applies the state currently required by the Asia/Karachi schedule to all configured KVMs.
For an explicit harmless scheduler test, it also accepts `--availability active|away`.
`reconcile` calculates desired state without an LLM and checks/applies it. The periodic launchd
reconciler uses a local success-state file to avoid PiKVM/OpenAI calls when the same desired state
was already verified.

Every scheduler invocation makes one initial attempt. If any profile fails, the same scheduler
process waits five minutes and retries only the failed profiles, for at most two retry rounds.
Successful profiles are recorded after each round and are not repeated. Each retry starts a fresh,
idempotent OBSERVE → REASON → ACT → VERIFY workflow, so HID operations themselves are never blindly
replayed. Direct `pikvm-agent slack availability ...` commands remain single-attempt.

Install three inspectable per-user LaunchAgents (Active, Away, and hourly missed-event
reconciliation), show their state, or remove only those generated files:

```bash
pikvm-agent schedule slack-availability install
pikvm-agent schedule slack-availability status
pikvm-agent schedule slack-availability uninstall
```

The files live under `~/Library/LaunchAgents/`; no root access or remote-machine scheduler is used.
Because launchd calendar triggers use the Mac's system time zone, installation requires the Mac
system time zone to be `Asia/Karachi`. The jobs use the active virtual environment's Python and the
current repository as their working directory, so keep both paths available after installation.
Local state is stored at
`~/Library/Application Support/pikvm-work-agent/slack-availability-state.json`.

## Milestone 5 manual validation

Do not install the schedule until the direct GUI workflow succeeds. Run these in order with the
actual configured profile names:

```bash
pikvm-agent slack availability get --kvm heidrick
pikvm-agent slack availability set active --kvm heidrick
pikvm-agent slack availability set away --kvm heidrick
```

Then validate sequential multi-KVM processing and deterministic scheduling without waiting for a
timer:

```bash
pikvm-agent slack availability set active --all-kvms
pikvm-agent schedule slack-availability reconcile
pikvm-agent schedule slack-availability run-now --availability away
```

After the results are visually correct on every KVM, install and inspect launchd:

```bash
pikvm-agent schedule slack-availability install
pikvm-agent schedule slack-availability status
```

Confirm that KVMs are handled one at a time, already-correct states report a no-op, each changed
state is visible in Slack, automatic Keychain TOTP does not prompt, and all three LaunchAgents show
`installed=yes, loaded=yes`. Do not test message reading/sending; that belongs to a later milestone.

## Private manual evaluation files

Put real screenshots and any expected-results manifest under `evals/private/`. Put temporary TOTP
QR images under `.local-secrets/`. Those directories and the standard screenshot/overlay/QR names
are ignored by Git. Unit tests use generated neutral images and fake TOTP QR codes plus mocked
Keychain, PiKVM, and OpenAI responses; they never spend API credits or access real credentials.

## Run automated checks

```bash
pytest
ruff check .
ruff format --check .
mypy
```

These checks do not access the real macOS Keychain, contact PiKVM/OpenAI, consume API credits,
prompt for credentials, or send keyboard/mouse input.

## API references

- [PiKVM HTTP API](https://docs.pikvm.org/api/)
- [zxing-cpp Python bindings](https://pypi.org/project/zxing-cpp/)
- [Python keyring](https://pypi.org/project/keyring/)
- [PyOTP](https://pyauth.github.io/pyotp/)
- [OpenAI Responses API](https://developers.openai.com/api/docs/guides/migrate-to-responses)
- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [OpenAI image inputs](https://developers.openai.com/api/docs/guides/images-vision)
