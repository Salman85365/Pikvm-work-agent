# Architecture decisions

## Human-assisted PiKVM 2FA

Status: accepted and implemented.

PiKVM 2FA remains human-assisted because it is intentionally a human checkpoint. When PiKVM
authentication requires TOTP, the local CLI prompts the user securely in the terminal. The agent
must never attempt to obtain, generate, store, or persist the 2FA secret.

## Agentless remote computer

Status: accepted.

All automation code, credentials, reasoning, and policy enforcement run on the controlling Mac.
The remote work computer receives only ordinary display, keyboard, and mouse interaction through
PiKVM. No agent, script, runtime, browser extension, API client, or automation service may be added
to the remote computer.

## OpenAI processing path

Status: accepted for a future milestone.

Selected PiKVM screenshots may be sent from the Mac to the OpenAI API for visual reasoning. The
intended path is PiKVM screenshot to Mac, Mac to OpenAI, reasoning result to the Mac controller,
and explicit HID input from the Mac through PiKVM. The current implementation does not yet call the
OpenAI API.

## GUI control loop

Status: accepted for future GUI automation.

Meaningful GUI actions must follow `OBSERVE -> REASON -> ACT -> VERIFY`. The controller must stop
when an authentication screen or dialog is unexpected, the visual state is unknown, confidence is
low, or the next action could be destructive.

## HID delivery semantics

Status: accepted and implemented at the transport layer.

Keyboard and mouse actions are explicit, deterministic operations. Mutating HID requests are never
automatically retried because a timeout can occur after PiKVM has already applied the input. Screen
coordinates are bounds-checked and converted to PiKVM's signed absolute-HID range on the Mac.

## Credential handling

Status: accepted and implemented.

PiKVM credentials and OpenAI credentials remain local and uncommitted. A prompted six-digit code is
validated, appended to the PiKVM password only in memory for that authentication attempt, and is
never stored or logged.

## Consequential actions

Status: accepted.

Reading visible state and performing explicitly requested diagnostic navigation are allowed.
External or destructive operations, including sending messages, pushing or merging Git changes,
deleting data, modifying production, and submitting consequential forms, require explicit user
approval.
