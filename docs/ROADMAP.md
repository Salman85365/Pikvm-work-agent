# Roadmap

## Milestone overview

| Milestone | Objective | Status |
| --- | --- | --- |
| M1 | PiKVM transport, authentication, and screenshots | Complete, hardware verified |
| M2 | Explicit HID operations | Complete, hardware verified |
| M3 | OpenAI screen understanding | Complete, hardware/API verified |
| M4 | Observe, act, and verify controller | Complete, hardware/API verified |
| M4.5 | Automatic PiKVM TOTP from macOS Keychain | Complete, hardware verified |
| M5 | Slack manual availability skill and scheduling | Implemented; real multi-KVM validation pending |
| M6 | Slack reading and summarization | Planned |
| M7 | Development-tool skills | Planned |
| M8 | Higher-level work agent | Planned |

## Milestone 1: PiKVM transport and screenshots — complete

Delivered and verified on real hardware:

- typed synchronous PiKVM HTTP client;
- environment configuration and sanitized errors;
- hidden interactive PiKVM 2FA prompting;
- validated JPEG screenshot retrieval and explicit local saving;
- bounded screenshot retries.

## Milestone 2: Explicit HID operations — complete

Delivered and verified on real hardware:

- one-operation CLI commands for key presses, hotkeys, text, absolute mouse movement, clicks, and
  scrolling;
- input validation before authentication;
- no automatic HID retries;
- sanitized uncertain-outcome errors.

The OpenAI analyzer is not connected to these methods.

## Milestone 3: Visual screen understanding — complete

Implemented:

- Mac-only OpenAI Responses API integration using strict Structured Outputs;
- typed screen state, safety warnings, relevant UI controls, normalized bounding boxes, and click
  points;
- provider-independent `ScreenAnalyzer` protocol and OpenAI implementation;
- environment and CLI model, service-tier, reasoning-effort, and image-detail configuration;
- `store=False`, in-memory live screenshots, and ignored private evaluation/debug paths;
- offline `analyze-file` and single-screenshot `analyze-screen` commands;
- local optional coordinate-debug overlay;
- usage, latency, retries, actual model/tier, and escalation telemetry;
- bounded transient API retries and sanitized non-retryable errors;
- disabled-by-default, one-step Luna-to-Terra escalation;
- mocked automated tests proving that screen analysis does not call HID methods.

Manual validation succeeded with a saved 1920x1080 Slack screenshot, Slack profile-control
localization and overlay inspection, and one live Chrome screenshot through the terminal PiKVM 2FA
flow.

Milestone 3 is perception only. It must stop after displaying `ScreenAnalysis`.

## Milestone 4: Verified GUI controller — complete

Implemented:

- explicit controller states and strict one-action planning;
- combined current-screen analysis and previous-action verification;
- local policy decisions, terminal approvals, `safe`/`every` approval modes, dry-run, and step mode;
- element-ID actions with local normalized-coordinate resolution;
- fresh pre-action screenshot guard and resolution invalidation;
- local downscaled grayscale screen-change and settle detection;
- no automatic HID retry, including uncertain-outcome handling;
- provider-backed read-only PiKVM authentication refresh;
- hard step/runtime caps, confidence stops, no-change/repetition guards, and Ctrl+C handling;
- local single-controller lock, ephemeral screenshots by default, optional ignored debug artifacts,
  and sanitized session telemetry;
- mocked unit coverage with no default OpenAI/PiKVM calls.

The user validated the controller against the real PiKVM/OpenAI path. HID operations remain
single-shot and every meaningful action requires a fresh visual verification.

## Milestone 4.5: Automatic local PiKVM TOTP — complete

Implemented:

- exact URL-host to macOS Keychain account mapping with non-default port isolation;
- named multi-PiKVM profiles with explicit/default selection and independent optional 2FA;
- local PNG/JPEG provisioning-QR decoding with strict TOTP and ambiguity validation;
- local RFC 6238 generation from Base32 or a standard TOTP provisioning URI;
- four-second expiration-boundary guard;
- QR-first import with Keychain read-back, harmless PiKVM verification, rollback on failure, and
  explicit deletion-after-success;
- manual hidden-secret fallback plus Keychain status, verify, and confirmed removal commands;
- keychain-by-default configuration with explicit hidden-terminal provider/fallback;
- common screenshot, screen-analysis, explicit HID, and AgentController authentication path;
- one bounded read-only reauthentication and no HID replay;
- generated fake QR fixtures and mocked Keychain/PiKVM tests that never access real credentials or
  hardware.

Real local enrollment and automatic authentication have been validated. PiKVMs that do not require
2FA remain isolated from Keychain lookup and prompting.

## Milestone 5: Slack manual availability — implementation complete, manual validation pending

Implemented:

- GUI-only get/set Active/Away workflows through the existing bounded controller;
- visible current-state inference and final-state verification from Slack's manual toggle;
- idempotent no-op behavior for already-correct states;
- sequential all-profile execution with per-KVM failure isolation;
- a narrow local policy allowance for only the visible manual Active/Away toggle;
- sanitized Mac-local JSONL operation logging;
- deterministic Asia/Karachi schedule and missed-event reconciliation;
- inspectable per-user launchd install/status/run-now/reconcile/uninstall commands;
- mocked transition, multi-KVM, timezone, state, CLI, and launchd tests.

Before marking M5 complete, validate get, both transitions, no-op behavior, all-KVM sequential
execution, reconciliation, and installed LaunchAgent status on the real environment. Do not begin
Slack message reading or sending.

## Later workflow milestones

Build approved workflows incrementally for Slack and development tools while preserving the
agentless remote architecture and explicit approval boundaries for external or destructive actions.
