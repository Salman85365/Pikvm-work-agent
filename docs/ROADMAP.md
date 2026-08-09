# Roadmap

## Milestone overview

| Milestone | Objective | Status |
| --- | --- | --- |
| M1 | PiKVM transport, authentication, and screenshots | Complete, hardware verified |
| M2 | Explicit HID operations | Complete, hardware verified |
| M3 | OpenAI screen understanding | Complete, hardware/API verified |
| M4 | Observe, act, and verify controller | Implemented; hardware validation pending |
| M5 | Slack status skill | Planned |
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

## Milestone 4: Verified GUI controller — implementation complete, manual validation pending

Implemented:

- explicit controller states and strict one-action planning;
- combined current-screen analysis and previous-action verification;
- local policy decisions, terminal approvals, `safe`/`every` approval modes, dry-run, and step mode;
- element-ID actions with local normalized-coordinate resolution;
- fresh pre-action screenshot guard and resolution invalidation;
- local downscaled grayscale screen-change and settle detection;
- no automatic HID retry, including uncertain-outcome handling;
- human-assisted read-only PiKVM authentication refresh;
- hard step/runtime caps, confidence stops, no-change/repetition guards, and Ctrl+C handling;
- local single-controller lock, ephemeral screenshots by default, optional ignored debug artifacts,
  and sanitized session telemetry;
- mocked unit coverage with no default OpenAI/PiKVM calls.

Before marking M4 complete, manually inspect Phase A dry-run, Phase B approval-every step mode, Phase
C safe mode, and the profile-menu element-ID objective. Do not change Slack status or send/read
messages during M4 validation.

## Later workflow milestones

Build approved workflows incrementally for Slack and development tools while preserving the
agentless remote architecture and explicit approval boundaries for external or destructive actions.
