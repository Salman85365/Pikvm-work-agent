# PiKVM Work Agent — Codex Instructions

## Purpose

This repository implements a personal productivity automation agent running entirely on the user's Mac.

It controls a remote work computer through an existing PiKVM connection.

The remote computer must remain agentless.

The architecture is:

Mac Python application → OpenAI API for reasoning/vision → PiKVM HTTP/HID interfaces → remote computer.

## Hard architectural constraint

Do not install or require any custom automation software on the remote work computer.

Do not solve tasks by adding software, agents, daemons, scripts, Python runtimes, OpenAI clients, browser extensions, Slack integrations, Slack API tokens, GitHub integrations, automation services, or similar components to the remote computer.

Applications on the remote computer must be operated through the same visible keyboard, mouse, and display interfaces a human using PiKVM would use.

The remote computer provides:

- display output through PiKVM;
- keyboard/mouse input through PiKVM HID.

All automation intelligence runs on the user's Mac.

## OpenAI architecture

This project uses Option A:

PiKVM screenshot → Mac → OpenAI API → reasoning/vision result → Mac controller → PiKVM HID.

Selected screenshots may therefore be sent from the Mac to the OpenAI API for visual reasoning.

Do not assume access to application APIs running inside the remote work environment.

For example, Slack automation should operate the Slack GUI through PiKVM rather than requiring Slack API credentials from the remote environment.

## Core interaction model

All GUI automation must follow:

OBSERVE → REASON → ACT → VERIFY

Never assume that a GUI operation succeeded.

After a meaningful action:

1. retrieve a new screen state;
2. verify the expected result;
3. continue only when sufficiently confident.

Prefer one verified action over long sequences of blind clicks.

## Deterministic operations first

Prefer deterministic keyboard/HID interactions whenever practical.

Examples:

- keyboard shortcut instead of visually finding a menu;
- OS application search instead of finding a taskbar icon;
- Ctrl/Cmd shortcuts instead of mouse operations;
- direct text input instead of clicking an on-screen keyboard.

Use visual reasoning when the UI state genuinely requires it.

## Named PiKVM profiles and 2FA

The Mac may control multiple PiKVMs through named local profiles. `PIKVM_PROFILES` lists the names,
`PIKVM_PROFILE` selects the default, and `pikvm-agent --profile NAME ...` selects one explicitly for
a command. Every profile has isolated URL, username, password, TLS, network, keymap, and TOTP
configuration. Never guess a profile when more than one is configured, and never send one profile's
credentials or HID input to another profile's endpoint.

Some PiKVMs require 2FA and others do not. Respect each selected profile's `TOTP_REQUIRED` value.
When false, do not retrieve a Keychain seed or prompt for a TOTP code. Legacy unprefixed single-KVM
variables remain supported when named-profile selection is absent.

For a profile requiring 2FA, normal operation retrieves the exact PiKVM host's raw TOTP seed from
the dedicated `pikvm-work-agent.totp` macOS Keychain service and generates a current six-digit RFC
6238 code locally. The seed and generated code must never go to OpenAI, the remote computer, logs,
telemetry, debug artifacts, `.env`, or project files.

The preferred enrollment path decodes one PNG/JPEG PiKVM provisioning QR locally with zxing-cpp,
validates a standard `otpauth://totp` credential, stores its normalized seed in the exact host's
Keychain item, and verifies it through a harmless PiKVM screenshot read. Never send a provisioning
QR or URI to OpenAI. Temporary real QR images belong under `.local-secrets/`, which is Git-ignored
but is not encrypted storage; delete them after successful verification. The runtime never depends
on the QR after import.

The existing hidden terminal prompt remains available only through the explicit interactive
provider or configured fallback. Fallback defaults to false so unattended operation never silently
blocks for input. Do not disable PiKVM server-side 2FA or touch Apple Passwords/authenticator data.

## Security and credentials

Never commit:

- PiKVM usernames;
- passwords;
- 2FA codes;
- TOTP provisioning QR images or URIs;
- OpenAI API keys;
- session tokens;
- secrets.

`.env` must remain ignored by Git.

Avoid logging sensitive information.

Sanitize exceptions where credentials could otherwise appear.

## GUI safety

Stop rather than guess when:

- an unexpected authentication screen appears;
- an unexpected dialog appears;
- visual confidence is low;
- screen state does not match the expected workflow;
- an operation may be destructive;
- the application appears to be in an unknown state.

The goal is useful automation, not maximum autonomy.

## Approval policy

Generally safe without additional approval:

- retrieving screenshots;
- examining visible UI;
- opening applications;
- navigating applications;
- reading visible information;
- checking Slack;
- summarizing Slack;
- setting the user's own Slack status;
- inspecting source code;
- viewing Git state;
- running known non-destructive tests.

Require explicit approval before external or destructive actions such as:

- sending Slack messages;
- pushing Git commits;
- merging pull requests;
- deleting files;
- deleting branches;
- force pushing;
- modifying production;
- running destructive commands;
- submitting consequential forms.

## Coding philosophy

Keep changes focused and natural to the existing project.

Before changing code:

1. inspect nearby implementation;
2. understand existing patterns;
3. make the smallest reasonable change.

Prefer:

- existing abstractions;
- existing project conventions;
- clear naming;
- small functions;
- type hints;
- meaningful tests.

Avoid:

- unnecessary comments;
- speculative abstractions;
- large unrelated refactors;
- generated-looking boilerplate;
- unnecessary wrappers;
- excessive documentation for trivial code;
- unrelated cleanup.

Do not change code merely to make it look different.

## Git discipline

Before substantial work:

- inspect `git status`;
- understand uncommitted changes;
- do not overwrite user changes.

After changes:

- inspect the diff;
- run relevant tests;
- run configured lint/type checks;
- remove debugging artifacts;
- report files changed.

Do not push unless explicitly requested.

Do not rewrite, squash, amend, or otherwise modify existing Git history unless explicitly requested.

Keep commits meaningful rather than producing fixup/noise commits.

## Testing

Never claim hardware interaction was tested unless it actually ran against the user's PiKVM.

Automated unit tests may mock PiKVM behavior.

For real hardware milestones:

1. prepare the code;
2. run local automated verification where possible;
3. provide the exact command to execute;
4. allow the user to perform any required local Keychain enrollment or interactive authentication;
5. inspect the actual resulting output.

## Current project status

Milestones 1 through 4 are complete and have been validated against the real PiKVM/OpenAI path.

Implemented:

- typed PiKVM client;
- PiKVM HTTP authentication;
- interactive terminal 2FA;
- automatic local TOTP generation from host-isolated macOS Keychain entries;
- local TOTP provisioning-QR import with safe post-verification deletion;
- named multi-PiKVM profiles with independent optional 2FA configuration;
- bounded read-only reauthentication without HID replay;
- screenshot retrieval;
- keyboard transport;
- text transport;
- mouse transport;
- click;
- double-click;
- scrolling;
- bounded screenshot retries;
- coordinate conversion;
- sanitized errors;
- environment configuration;
- tests and static analysis.

Important real-world result:

The user successfully executed the screenshot command from the Mac, entered the PiKVM 2FA code interactively, and received a correct screenshot of the remote computer.

The user also validated the explicit Milestone 2 keyboard, hotkey, text, mouse movement, click, and scroll commands against the real PiKVM.

Therefore:

PIKVM TRANSPORT + AUTHENTICATION + SCREENSHOT CAPTURE + EXPLICIT HID OPERATIONS HAVE BEEN VERIFIED ON REAL HARDWARE.

The user also validated Milestone 3 against a saved real Slack screenshot and a live PiKVM
screenshot. Application recognition, normalized Slack profile-control localization, overlay output,
and the interactive live 2FA analysis flow all succeeded.

Automatic Keychain TOTP and named mixed-2FA PiKVM profiles have also been validated locally.

Do not redo Milestones 1 through 4.5 unless a specific defect requires it.

## Current next milestone

Milestone 5 Slack manual Active/Away automation is implemented and awaiting real multi-KVM GUI
validation. It uses the existing controller, sequential named profiles, visible final-state
verification, sanitized local logs, and Mac-local launchd scheduling/reconciliation.

The controller implements:

OBSERVE → ANALYZE/VERIFY → PLAN ONE ACTION → POLICY → STALE-SCREEN GUARD → ONE HID ACTION → OBSERVE.

Milestone 4 may:

- perform bounded, harmless GUI navigation;
- request local human approval;
- execute exactly one locally validated action per cycle;
- verify every executed action from a fresh settled screenshot;
- stop on uncertainty, unsafe state, stale plans, loops, or configured limits.

Milestone 5 may only read or change the user's manual Slack Active/Away availability. It must not
read/send messages, edit status text or emoji, change preferences, or simulate activity. Do not
start Milestone 6 until real single- and multi-KVM availability transitions and the generated
LaunchAgents have been validated by the user.

Read-only OpenAI analysis may use bounded transient retries. The existing rule against retrying HID actions remains unchanged.

## Long-term objective

Eventually the user should be able to request tasks such as:

- check Slack;
- summarize unread Slack activity;
- identify messages needing attention;
- set Slack status;
- draft responses;
- inspect development tools;
- run tests;
- inspect Git changes;
- assist with routine development work.

These capabilities must continue to operate through PiKVM while keeping the remote computer agentless.
