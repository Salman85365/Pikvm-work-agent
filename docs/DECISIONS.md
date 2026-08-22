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

## Managed PiKVM profiles and the disabled list

Status: accepted and implemented.

Profiles come from two sources. `.env` (`PIKVM_PROFILES` + `PIKVM_<NAME>_*`) remains the
hand-edited source. Profiles added from the dashboard or `pikvm-agent profiles add` are *managed*:
non-secret fields live in `~/Library/Application Support/pikvm-work-agent/profiles.json` (owner
only, atomic writes) and the password lives in macOS Keychain under the
`pikvm-work-agent.password` service keyed by profile name, so no file ever holds it. TOTP seeds
keep their existing `pikvm-work-agent.totp` home keyed by host, and the dashboard can enroll one by
decoding a provisioning QR entirely in memory (bytes posted to the loopback server, never written
to disk, seed never returned), verifying with a harmless screenshot read and rolling back on
failure.

Any profile from either source can be disabled. The disabled list lives in the same JSON; disabled
profiles are skipped by `--all-kvms`, the scheduler, the dashboard's targets and state, and an
explicit selection is refused with a message naming the remedy rather than "unknown profile". A
KVM that is down or repurposed is therefore switched off in one place instead of being left to fail
every hour. `.env` profiles cannot be removed from the dashboard, only disabled; the CLI
(`pikvm-agent profiles ...`) can do everything the dashboard can, so the dashboard adds no
authority.

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
when an authentication or lock screen appears, the visual state is unknown, confidence is low, the
feed is disconnected, or the next action could be destructive. An unexpected dialog or notification
is not a stop: it is reported as a warning, the planner is told to bring the wanted application in
front instead, and the policy engine denies answering the dialog (its buttons, Enter, Escape, Space)
while it is on screen. Before this (2026-08-19) every macOS update nag or notification banner ended
the run, and the model's own `safe_to_continue=false` was also honoured when it merely meant
"nothing left to do"; now safety is decided locally from warning categories and a model caution
without a hazard becomes an advisory note in the summary.

No model can call PiKVM HID directly. The analyzer produces screen state, the text planner proposes
one typed action, local policy and approval classify it, and a fresh-screen guard invalidates stale
plans. The executor may then send exactly one existing HID operation. Local polling waits for a
settled frame, and a fresh vision observation verifies the prior action before another is planned.

Verification failure follows a short ladder rather than an immediate stop: an `uncertain` verdict
earns one delayed re-observation (the UI may still have been animating), and a verdict that stays
unconfirmed is handed to the planner as feedback so it can take a different route, bounded by
`AGENT_MAX_VERIFICATION_RECOVERIES` per session. The unverified action is never proposed again on
that screen; the transport never replays an HID request whose outcome is unknown. Model output that
the local code can repair (a `target_found` flag disagreeing with `target`, an inverted box, an
empty label, a spurious first-frame verification, an over-long evidence string) is normalised
instead of rejected, and a planner proposal that references a non-clickable element earns one
correction round instead of ending the session; only unrepairable output ends it, and then as
`model_output_invalid`. The planner receives element geometry, the policy's pre-approved keys, and
the previous verdict, but never element visible text.

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
only in memory for one client authentication context.

The client authenticates once with `POST /api/auth/login` and holds PiKVM's `auth_token` cookie
for the life of the session. It previously sent `X-KVMD-User`/`X-KVMD-Passwd` headers with the
TOTP embedded on every request; measured against real hardware, kvmd accepts such a code only for
the current 30-second window plus one, so any HID request landing later than about a minute into a
session was rejected with 401 and reported as `transport_failed`. Because kvmd checks
authentication before dispatching to a handler, a 401/403 proves the request was not applied: the
client logs in again and repeats that one request exactly once, for reads and HID alike. That is
the only HID repeat the transport performs; timeouts and connection drops are still never retried.

## Diagnostics and failure taxonomy

Status: accepted and implemented.

Every process writes an owner-only rotating log to `~/Library/Logs/pikvm-work-agent/agent.log`
containing what the code itself authored: controller states, per-step action/policy/transport/
verification outcomes, stop codes, exception classes with their sanitized messages, and the class
names of the cause chain. Screen content, model prose, credentials, and TOTP material never go
there; pydantic validation failures are logged by field path and error type only, because their
`str()` embeds the rejected input.

The controller distinguishes `pikvm_unreachable`, `pikvm_auth_failed`, `model_provider_error`, and
`model_output_invalid` from `internal_error`, and the Slack availability workflow appends the
sanitized exception message for those codes. Before this, an offline KVM logged twenty-two
consecutive "A sanitized local error stopped the controller." entries and nothing said why.
Workflow JSONL logs also carry per-run telemetry (sessions, steps, HID actions, model calls,
tokens, runtime) so cost and duration are visible without model content.

## Endpoint lease

Status: accepted and implemented.

One `ControllerLock` lease per workflow, held across every controller phase (foreground, recovery,
read-back), acquired with a bounded wait of 185 seconds rather than failing on contact. Lock files
live in `~/Library/Application Support/pikvm-work-agent/locks/` instead of `TMPDIR`, which differs
between a login shell, launchd, ssh, and `env -i`. A lease that could not be obtained is reported
with the `lock_busy` stop code, not as a workflow failure.

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

Three inspectable user LaunchAgents fire Monday-Friday at 18:00, Tuesday-Saturday at 02:00, and
hourly; all three run `reconcile --if-due` and compute the desired state at fire time. The
calendar agents used to force `--availability active/away`, but launchd replays a missed
`StartCalendarInterval` on wake, so a Mac asleep across a boundary applied the forced value hours
late. Retries likewise re-read the clock after each wait, so a retry that crosses a boundary
applies the state that is now due. launchd calendar intervals use the Mac system time
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
input. It is positional: `SlackTriagePolicyEngine` permits input only while Slack is not yet the
foreground application, so once Slack is in front every conversation is out of reach by construction.
A target whose name begins with `#` is additionally refused. Everything else defers to the generic
engine.

A launcher-label allowlist was tried first and rejected. Requiring an exact `Slack` label with no
other visible text denied the very click that brings Slack forward, because a Dock icon routinely
carries an unread badge — the workflow would have failed at its first step on real hardware. A policy
that blocks the intended path is not a safe policy; it is a broken one. Prefer the narrowest rule
that actually names the harmful action.

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

## Receive-only meeting capture and conservative ownership

Status: accepted and implemented; real PiKVM audio validation pending.

Meeting recording is explicit and Mac-local. It opens a separate authenticated PiKVM Janus/WebRTC
viewer for the selected profile and requests only incoming HDMI audio: audio receive is enabled,
microphone transmission and camera video are disabled, and no local media source is created. It does
not control the remote meeting application, join a call, use a meeting API or bot, or add software to
the remote computer. One global recording is allowed because `meeting stop` deliberately has no KVM
selector; that also prevents two profiles from being mixed accidentally.

Finalized Ogg/Opus parts, state, and processing intermediates are private and atomic. The recorder
does not persist transcript content in normal logs. A disconnect or processing failure preserves
completed work for an explicit retry, while exact session/KVM binding prevents artifacts from being
substituted across recordings.

Transcription and intelligence are separate provider boundaries. The initial transcription provider
uses diarization but maps provider-local speaker tags to anonymous `Speaker N` labels; profile work
identity is textual ownership context, never voice identification. The selected KVM's identity is
snapshotted at capture time. Exact named assignments can be classified as `our_identity`, indirect
context can be only `possibly_our_identity`, and ambiguous ownership stays `unknown`. A local guard
rechecks these rules before reporting so provider output cannot broaden ownership on its own.

## Meeting transcription through Deepgram

Status: accepted and implemented; real audio validation pending.

`MEETING_TRANSCRIPTION_PROVIDER=deepgram` routes finalized Ogg/Opus parts to Deepgram's
pre-recorded endpoint (`nova-3`, `diarize=true&utterances=true`) instead of OpenAI's audio
transcription. The provider implements the same `TranscriptionProvider` protocol and produces the
same anonymous `Speaker N` transcript, so the intelligence stage and report are provider-blind.
Intelligence stays on the OpenAI meeting model; configuring Deepgram for it is refused. Per-part
responses are cached beside the audio (owner-only) so a failure in a later stage never re-uploads
or re-bills a part. The dashboard's Meeting recorder panel and `pikvm-agent meeting
list|show|validate` read sessions through `meeting/library.py`, a read-only view over the session
directories; meeting content reaches the browser only when one specific session is opened and is
never logged.
