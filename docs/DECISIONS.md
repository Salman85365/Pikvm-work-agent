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

Because a plist records an absolute interpreter path, installing from the wrong shell produced agents
that launchd reported as loaded while every run died with `No module named work_agent`. Install now
probes the recorded interpreter against the recorded working directory before writing any plist, and
a separate health check probes what the *already installed* agents would run. `status` surfaces that
result and exits non-zero when scheduled runs cannot work, so a loaded job is never mistaken for a
working one.

## Non-destructive Slack triage

Status: accepted and implemented for the sidebar tier.

Reading a Slack conversation through the GUI marks it read. That is a side effect on the user's own
inbox, is not reliably reversible from automation, and would destroy exactly the unread signal triage
exists to report — worst of all in an unattended scheduled run. Milestone 6 therefore reads only what
Slack already displays: sidebar conversation names, unread counts, mention badges, and muted state.

The boundary is enforced locally, not requested in the prompt, because model output is untrusted
input. `SlackTriagePolicyEngine` is an allowlist: an application launcher labelled exactly `Slack`,
`MetaLeft+Space`, the literal navigation search text `slack`, and Enter or Escape. Clicking a sidebar
row, double-clicking, and scrolling are denied, and a conservative stop from the generic engine is
never overridden. The exact-label match matters: a sidebar entry named `Slack` would otherwise be
indistinguishable from the Dock icon, so a launcher must additionally carry no extra visible text.

Foregrounding and reading are separate phases. The bounded controller brings Slack forward; the read
then runs on a plain screenshot with no executor attached, so it cannot send HID at all rather than
merely being instructed not to.

Structured output is a dedicated strict schema rather than encoded into `UIElement` labels, using a
new generic `OpenAIScreenAnalyzer.perceive` entry point that later skills can reuse. The schema has
no field capable of holding message text, so the model cannot return conversation contents even if
the prompt were ignored — a stronger guarantee than instruction alone.

Ranking is limited to three honest tiers: mention, direct message, other unread. Deciding whether a
message is a question, a blocker, or an approval request requires opening the conversation, so the
richer triage the long-term roadmap describes is deliberately deferred rather than approximated.

Only counts are logged. Conversation names are Slack content, and the roadmap forbids persisting
Slack content locally, so the JSONL record carries unread/mention/direct totals and nothing that
identifies a channel or person.

## Two-signal screen-change detection

Status: accepted and implemented.

Local screen comparison downscales to a 64x36 greyscale grid. The whole-screen mean is the right
signal for the stale-plan guard, because it must not cancel a valid plan over cursor blink or video
noise. It is the wrong signal for "did anything happen", and a real `nbc_kvm` trace proved it: a
click that visibly opened Slack's profile menu measured 0.0046 — below the 0.015 change threshold —
so the detector reported `changed=no` and burned its full five-second timeout on a screen that had
already settled. The menu covers roughly 6% of the screen at about 26 grey levels of contrast, which
a whole-screen average cannot see.

Settle detection therefore also computes the fraction of grid cells that moved at least 10 grey
levels, and treats either signal as a change. Unchanged real frames measured 0.0001 mean, so the
per-cell floor sits far above sensor noise while staying far below a real popover. The stale-plan
guard deliberately keeps using the mean alone.

This matters beyond wasted time: `AGENT_MAX_NO_CHANGE_STEPS` counts consecutive unchanged steps, so a
workflow that opens a menu, clicks a toggle, and reopens the menu to verify could otherwise be
stopped as stuck on changes that genuinely happened.

## Structured controller stop codes

Status: accepted and implemented.

`AgentSessionResult` carries a `StopCode` alongside its prose summary, and every one of the
controller's stop paths sets one. Skills, logs, and telemetry must classify outcomes from that code
and never by matching the summary text.

This replaced string matching that had silently lost information. `slack.agent_operator` mapped 3 of
the controller's 8 possible failure summaries and flattened the rest into "The verified controller
stopped with status failed", so 13 of 26 real failures were recorded with the cause discarded:
completion-validation failures, policy denials, HID transport failures, missing verification, and
caught exceptions were indistinguishable. Reliability work on Milestone 5 was blocked because the
most common failure was unattributable.

The reason table is exhaustive over `StopCode` and tested for exhaustiveness, distinctness, and
reachability, so adding a stop path without a reason fails the suite rather than degrading into a
generic message. Reasons remain sanitized: no screen content, credentials, or provider detail.

`stop_code` is written to the sanitized JSONL log. Records predating the field fall back to text
matching, which is retained only for continuity of the existing history and must not be extended.

## Local operations dashboard

Status: accepted and implemented.

A Mac-local FastAPI/uvicorn dashboard presents the existing workflows in a browser. It is a view and
a trigger, not a new authority: availability runs call the same `SlackAvailabilityService`, the same
bounded controller, the same local policy, and the same visible verification as the CLI, with the
noninteractive approval provider so an unexpected approval requirement stops that KVM. Nothing is
added to the remote computer, and the agentless constraint is unaffected.

The dashboard can start real HID workflows, so it is deliberately confined. It binds only a loopback
address; a non-loopback `Host` header is refused so DNS rebinding cannot reach it from another page.
Every API request carries a per-session token generated at startup and embedded in the served page,
never a URL parameter. Blocking workflows run in background threads and stream sanitized trace lines
over Server-Sent Events read through `fetch`, since `EventSource` cannot send the token header. One
workflow runs per KVM at a time; a request for a busy KVM is refused rather than queued. Profiles
configured for interactive TOTP entry are rejected before a run starts, because a server must never
block on a hidden terminal prompt. Passwords, TOTP seeds, generated codes, and API keys never reach
the browser, and live screenshots are streamed with `no-store` and never written to disk.

Charts follow the project's read-only-observability principle: they aggregate only the sanitized
JSONL operation log and the local reconciliation state. Success rate is shown as a same-ramp meter
and stop reasons as single-hue bars, because status green-versus-red fails colour-vision separation;
every outcome is therefore carried by an icon and a label as well as colour, and each chart has a
table view. Stop reasons are additionally grouped into the long-term roadmap's GUI-reliability
categories, matched against the strings `slack.agent_operator` actually emits, so the most common
real reason never lands in an uninformative "other" bucket.

The information architecture is taken from the long-term roadmap rather than from the current feature
set. `ROADMAP_UPDATED_LONGTERM.md` §11 makes multi-KVM orchestration the normal case, so a fleet rail
keeps every configured environment visible and independently actionable instead of hiding them behind
a selector; §16 defines skills as a navigable tree, so the sidebar is grouped by skill domain with
planned milestones present but disabled; §10 defines autonomy levels, so live skills display theirs.
Long-running work streams into a drawer docked at the bottom rather than a card, because a run
started from the fleet rail must stay observable from any section.
