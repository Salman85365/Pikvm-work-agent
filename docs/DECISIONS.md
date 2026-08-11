# Architecture decisions

## Named multi-PiKVM configuration

Status: accepted and implemented.

Multiple PiKVMs are configured as local `.env` profiles. `PIKVM_PROFILES` declares case-insensitive
slug names, `PIKVM_PROFILE` chooses the default, and the global `--profile` option overrides it for
one CLI invocation. Each name maps to an isolated `PIKVM_<PROFILE>_` variable prefix containing the
endpoint, credentials, TLS/network behavior, keymap, and TOTP policy. Hyphens map to underscores;
configured names that would collide after that mapping are rejected.

When several profiles exist, absence of both a default and explicit selection is an error. This is
a safety boundary: the program never guesses which KVM should receive credentials or HID input.
Profiles with `TOTP_REQUIRED=false` do not read Keychain or request a code. Profiles with 2FA use the
existing exact-host Keychain convention, preserving isolation even if profile names later change.
Legacy unprefixed configuration remains compatible only when named-profile selection is absent.

## Automatic local PiKVM TOTP

Status: accepted, implemented, and locally validated.

Each PiKVM's raw TOTP seed is stored as a dedicated generic-password entry in macOS Keychain. The
service is `pikvm-work-agent.totp`, and the account is the normalized lowercase PiKVM hostname with
a non-default port retained. The Mac retrieves the exact host's seed and generates a current
six-digit RFC 6238 code locally. Neither the seed nor generated code is sent to OpenAI, the remote
computer, logs, telemetry, debug artifacts, environment configuration, or project files.

The preferred enrollment path uses zxing-cpp to decode exactly one PNG/JPEG provisioning QR on the
Mac. The result must be a supported `otpauth://totp` credential; arbitrary URLs, HOTP, malformed or
ambiguous QR content, and nonstandard algorithm/digit/period settings are rejected. Import performs
a Keychain read-back and harmless PiKVM screenshot authentication before succeeding. Source-image
deletion is explicit and occurs only after verification. The QR/URI is never sent to OpenAI or used
by the runtime after enrollment. The hidden `setup-secret` command remains a manual fallback.

The interactive hidden terminal provider remains an explicit debugging option. It is used only
when the selected profile's `TOTP_PROVIDER=interactive`, or when keychain mode fails and that
profile's `TOTP_INTERACTIVE_FALLBACK=true`. Fallback defaults to false so unattended runs stop
instead of silently waiting for terminal input.

This intentionally gives the Mac programmatic access to both the PiKVM password source and TOTP
seed so local automation can run unattended. Protection of the Mac and macOS account therefore
becomes especially important. PiKVM server-side 2FA remains enabled.

## Agentless remote computer

Status: accepted.

All automation code, credentials, reasoning, and policy enforcement run on the controlling Mac.
The remote work computer receives only ordinary display, keyboard, and mouse interaction through
PiKVM. No agent, script, runtime, browser extension, API client, or automation service may be added
to the remote computer.

## OpenAI processing path

Status: accepted and implemented for perception and controlled planning.

Selected PiKVM screenshots may be sent from the Mac to the OpenAI API for visual reasoning. The
Milestone 3 path is PiKVM screenshot to Mac, Mac to the Responses API, and strict `ScreenAnalysis`
back to the Mac CLI. Milestone 4 adds a separate stateless text planner that sees sanitized
structured screen data, never the PiKVM client. Requests use `store=False`, and live screenshots
remain in memory unless the user explicitly requests debug artifacts. Models, service tier,
reasoning effort, and image detail are configuration rather than controller policy.

The screen analyzer is provider-independent at its boundary, but only the OpenAI implementation is
included. The vision model returns relevant controls in normalized 0-1000 coordinates. Autonomous
planning can reference only current element IDs; conversion to screenshot pixels occurs locally
inside the executor after policy and stale-screen checks.

## GUI control loop

Status: accepted and implemented as a bounded generic controller.

Meaningful GUI actions must follow `OBSERVE -> REASON -> ACT -> VERIFY`. The controller must stop
when an authentication screen or dialog is unexpected, the visual state is unknown, confidence is
low, or the next action could be destructive.

No model can call PiKVM HID directly. The analyzer produces screen state, the text planner proposes
one typed action, local policy and approval classify it, and a fresh-screen guard invalidates stale
plans. The executor may then send exactly one existing HID operation. Local polling waits for a
settled frame, and a fresh vision observation verifies the prior action before another is planned.
Failed or uncertain verification stops rather than repeating the action.

The generic controller excludes raw-coordinate planning, shell/script execution, power actions,
and application-specific workflows. Those boundaries remain in force for later skills.

## HID delivery semantics

Status: accepted and implemented at the transport layer.

Keyboard and mouse actions are explicit, deterministic operations. Mutating HID requests are never
automatically retried because a timeout can occur after PiKVM has already applied the input. Screen
coordinates are bounds-checked and converted to PiKVM's signed absolute-HID range on the Mac.

## Credential handling

Status: accepted and implemented.

PiKVM credentials and OpenAI credentials remain local and uncommitted. The TOTP seed exists only in
the dedicated macOS Keychain item; generated codes are validated and appended to the PiKVM password
only in memory for one client authentication context. Safe screenshot reads may create a fresh
client and retry once after authentication expiry. HID operations are never replayed.

## Consequential actions

Status: accepted.

Reading visible state and performing explicitly requested diagnostic navigation are allowed.
External or destructive operations, including sending messages, pushing or merging Git changes,
deleting data, modifying production, and submitting consequential forms, require explicit user
approval.

## Slack manual availability workflow

Status: accepted and implemented; real multi-KVM validation pending.

Milestone 5 is a bounded application workflow layered on the existing generic controller. It may
open Slack, navigate to the profile menu, visually read the manual Active/Away toggle, and select
only that toggle when a change is required. It does not use Slack APIs, fixed coordinates, remote
software, status text/emoji edits, message access, or simulated activity. Completion is accepted
only when a fresh observation contains explicit visible toggle evidence for the requested state.
Because the account avatar is a small edge control, Slack-foreground observations receive a
stateless focused localization pass before profile navigation. Its click point must fall inside the
model-returned profile bounding box. One invalid result may be retried with the configured fallback
model at medium reasoning. An ambiguous low-confidence/unknown Slack-workflow observation may also
receive one read-only fallback-model confirmation. Authentication, lock, unexpected-dialog,
destructive, and disconnect warnings remain immediate stops. The generic observation still
supplies prior-action verification, and every vision call is included in usage, latency, retry, and
call-count telemetry.

The generic policy retains its normal edit/communication approval rules. The sole unattended
exception is a single click on a visible Slack menu item whose label is exactly `Set yourself as
active` or `Set yourself as away`. Unknown targets and every message/status-text control remain
approval-gated or denied. Scheduled runs use a noninteractive approval provider, so any unexpected
approval requirement stops safely.

Named PiKVM profiles are processed sequentially and independently. A sanitized failure for one
profile does not prevent later profiles from running. Logs contain only operation metadata and do
not duplicate screen content.

## Mac-local Slack availability scheduling

Status: accepted and implemented; real launchd validation pending.

Three inspectable user LaunchAgents apply Active Monday-Friday at 18:00, Away Tuesday-Saturday at
02:00, and hourly deterministic reconciliation. launchd calendar intervals use the Mac system time
zone, so installation requires `Asia/Karachi`; the process environment also sets `TZ` explicitly.
The hourly reconciler calculates desired state using `zoneinfo`, never an LLM, and consults a local
record of successfully verified applications so unchanged desired state does not repeatedly invoke
PiKVM/OpenAI. Missed transitions remain due until every affected profile succeeds. Scheduling runs
only on the Mac and never concurrently across KVMs.

A scheduled invocation retries workflow-level failures twice, with a five-minute delay before each
retry round. Only failed profiles are retried, and verified successes are persisted after every
round. This does not retry individual HID requests: each later attempt creates a fresh bounded,
idempotent controller session that observes current Slack state before deciding whether any action
is still required. Interactive Slack CLI operations remain single-attempt.
