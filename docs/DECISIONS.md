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

PiKVM credentials and OpenAI credentials remain local and uncommitted. A prompted six-digit code is
validated, appended to the PiKVM password only in memory for that authentication attempt, and is
never stored or logged.

## Consequential actions

Status: accepted.

Reading visible state and performing explicitly requested diagnostic navigation are allowed.
External or destructive operations, including sending messages, pushing or merging Git changes,
deleting data, modifying production, and submitting consequential forms, require explicit user
approval.
