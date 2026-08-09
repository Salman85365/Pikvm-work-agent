# Roadmap

## Milestone overview

| Milestone | Objective | Status |
| --- | --- | --- |
| M1 | PiKVM transport, authentication, and screenshots | Complete |
| M2 | Real HID validation | Next |
| M3 | OpenAI screen understanding | Planned |
| M4 | Observe, act, and verify controller | Planned |
| M5 | Slack status skill | Planned |
| M6 | Slack reading and summarization | Planned |
| M7 | Development-tool skills | Planned |
| M8 | Higher-level work agent | Planned |

## Milestone 1: PiKVM transport and screenshots — complete

Delivered:

- typed synchronous PiKVM HTTP client;
- environment-based configuration and sanitized errors;
- hidden interactive PiKVM 2FA prompting;
- validated JPEG screenshot retrieval and explicit local saving;
- keyboard, text, absolute-mouse, click, double-click, and scroll transport methods;
- bounded screenshot retries and non-retrying HID requests;
- mocked unit tests plus Ruff and mypy configuration.

Verified on real hardware:

- connection from the controlling Mac to PiKVM;
- PiKVM authentication with a freshly prompted 2FA code;
- retrieval and saving of a correct remote-computer screenshot.

HID actions have not yet been verified on real hardware.

## Milestone 2: Explicit HID verification — next

Expose narrowly scoped CLI commands for one explicitly requested operation at a time:

- key press;
- hotkey;
- text entry;
- absolute mouse movement;
- click;
- scroll.

Each command must validate its inputs, prompt securely for authentication, avoid automatic retries,
and report sanitized failures. Unit tests and static checks must pass before hardware testing. Real
PiKVM testing will then proceed incrementally using a safe visible target: observe the screen,
perform one action, capture a new screen state, and verify the result before testing the next action.

Autonomous GUI control and OpenAI vision are out of scope for this milestone.

## Milestone 3: Visual screen understanding

Add selective screenshot submission from the Mac to the OpenAI API and return structured screen
observations with confidence information. This milestone should understand visible state without
performing autonomous actions.

## Milestone 4: Verified GUI controller

Introduce the `OBSERVE -> REASON -> ACT -> VERIFY` controller with deterministic operations first,
single-action execution, post-action screenshots, confidence thresholds, and safe stopping behavior.

## Later workflow milestones

Build approved workflows incrementally for Slack and development tools while preserving the
agentless remote architecture and explicit approval boundaries for external or destructive actions.
