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

## Authentication and PiKVM 2FA

The PiKVM requires two-factor authentication.

The existing implementation prompts interactively in the terminal:

PiKVM 2FA code:

Requirements:

- prompt for a fresh code when authentication is required;
- input must remain hidden;
- validate six-digit input;
- never store the 2FA code;
- never write the code to logs;
- never place the code in `.env`;
- keep the code only in memory for the current authentication attempt.

Do not remove or weaken this behavior unless explicitly requested.

## Security and credentials

Never commit:

- PiKVM usernames;
- passwords;
- 2FA codes;
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
4. allow the user to enter PiKVM 2FA;
5. inspect the actual resulting output.

## Current project status

Milestone 1 is complete and has been validated against the real PiKVM.

Implemented:

- typed PiKVM client;
- PiKVM HTTP authentication;
- interactive terminal 2FA;
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

Therefore:

PIKVM TRANSPORT + AUTHENTICATION + SCREENSHOT CAPTURE HAVE BEEN VERIFIED ON REAL HARDWARE.

Do not redo Milestone 1 unless a specific defect requires it.

## Current next milestone

Proceed with Milestone 2 incrementally.

The next objective is to safely verify HID operations against the real PiKVM.

Do not begin general autonomous GUI control yet.

Implement/test explicit commands such as:

- key;
- hotkey;
- type;
- mouse move;
- click;
- scroll.

Each command should perform exactly the explicitly requested operation.

Avoid automatic retries for HID actions where retrying could duplicate input.

After the transport is proven, proceed to visual screen understanding as a separate milestone.

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