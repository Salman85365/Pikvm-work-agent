# PiKVM Work Agent

A local Python application that observes and controls a remote computer only through an existing
PiKVM connection. Nothing is installed on the remote computer.

## Current status: Milestones 6.5–6.6 implemented; real meeting validation pending

Milestones 1 through 4.5 are complete and have been validated against the real PiKVM/OpenAI path from
the controlling Mac. Authentication, screenshots, explicit HID operations, screen perception, and
the bounded observe/reason/act/verify controller have all been proven on hardware.

Real Milestone 3 tests correctly identified Slack from a saved 1920x1080 screenshot, localized its
profile control in normalized coordinates with a useful overlay, and identified Chrome from a live
PiKVM screenshot after the interactive 2FA flow.

Milestone 4 implements the bounded controller:

```text
observe -> analyze and verify -> plan one action -> local policy -> fresh-screen guard
-> one HID action -> local screen settle -> fresh observation
```

The planner returns exactly one strict `ActionProposal`; it cannot invoke PiKVM. A local policy,
human approvals, stale-screen comparison, hard step/runtime limits, repetition detection, and
post-action visual verification gate progress. HID requests remain non-retryable. Automated tests
pass.

Milestone 4.5 adds automatic local RFC 6238 TOTP generation. Each PiKVM seed is stored in a separate
generic-password item in macOS Keychain and selected by normalized PiKVM hostname. Enrollment now
decodes the PiKVM provisioning QR entirely on the Mac and verifies it with a harmless PiKVM read.
Real Keychain enrollment and hardware verification succeeded. Never paste a real TOTP seed or
provisioning URI into Codex, ChatGPT, a Git file, or any OpenAI prompt.

Explicit receive-only meeting capture and diarized meeting intelligence are now implemented and
covered by mocked local tests. They have not yet been exercised against real PiKVM HDMI audio; use
the manual validation commands below before treating that path as hardware-verified.

Milestone 5 adds one bounded Slack GUI workflow for reading and setting manual Active/Away
availability. It uses the existing controller, visually verifies the menu's current-state toggle,
processes named KVMs sequentially, and records only sanitized operation metadata. A Mac-local
launchd schedule applies Active Monday-Friday at 18:00 and Away Tuesday-Saturday at 02:00 in
Asia/Karachi, with deterministic reconciliation for missed events. Automated validation passes;
real Slack transitions across the configured KVMs must still be performed by the user before the
milestone is marked complete.

## Requirements

- macOS with Python 3.11 or newer;
- access to the user's macOS login Keychain;
- network access from the Mac to PiKVM and the OpenAI API;
- a PiKVM user with access to snapshot and HID APIs;
- an OpenAI API key for screen-analysis commands.

## Install on the Mac

```bash
cd pikvm-work-agent
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
cp .env.example .env
```

Configure one or more named PiKVM profiles in `.env`. `PIKVM_PROFILE` selects the default:

```dotenv
PIKVM_PROFILES=heidrick,lab-kvm
PIKVM_PROFILE=heidrick

PIKVM_HEIDRICK_URL=https://heidrick-pikvm.example.local/kvm
PIKVM_HEIDRICK_USERNAME=admin
PIKVM_HEIDRICK_PASSWORD=change-me
PIKVM_HEIDRICK_TOTP_REQUIRED=true
PIKVM_HEIDRICK_TOTP_PROVIDER=keychain
PIKVM_HEIDRICK_TOTP_INTERACTIVE_FALLBACK=false
PIKVM_HEIDRICK_VERIFY_SSL=false

PIKVM_LAB_KVM_URL=https://lab-pikvm.example.local
PIKVM_LAB_KVM_USERNAME=admin
PIKVM_LAB_KVM_PASSWORD=change-me
PIKVM_LAB_KVM_TOTP_REQUIRED=false
PIKVM_LAB_KVM_VERIFY_SSL=false
```

Profile names are case-insensitive slugs containing letters, numbers, hyphens, or underscores. A
hyphen becomes an underscore in environment-variable names, so `lab-kvm` uses the
`PIKVM_LAB_KVM_` prefix. Each profile has independent credentials, 2FA policy, TLS verification,
timeouts, retry settings, and keymap. A trusted certificate with verification enabled is
preferable; local self-signed devices commonly require their profile's `VERIFY_SSL=false`.

There is intentionally no TOTP-secret environment setting. Profiles that require 2FA use an
exact-host Keychain entry; profiles with `TOTP_REQUIRED=false` never read that entry or prompt for a
code. Legacy `PIKVM_URL`, `PIKVM_USERNAME`, and related single-KVM variables remain supported only
when neither `PIKVM_PROFILES` nor `PIKVM_PROFILE` is configured.

Use the default profile by omitting a selector, or override it for one invocation. The global option
must precede the command:

```bash
pikvm-agent screenshot --output heidrick-screen.jpg
pikvm-agent --profile lab-kvm screenshot --output lab-screen.jpg
```

When multiple profiles are listed and no default is configured, the CLI stops and requires
`--profile`; it never guesses which KVM should receive credentials or HID input.

For OpenAI perception and planning, also set:

```dotenv
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-5.6-luna
OPENAI_FALLBACK_MODEL=gpt-5.6-terra
OPENAI_SERVICE_TIER=default
OPENAI_REASONING_EFFORT=low
OPENAI_IMAGE_DETAIL=auto
OPENAI_STORE=false
OPENAI_REQUEST_TIMEOUT_SECONDS=30
OPENAI_MAX_RETRIES=2
OPENAI_ESCALATION_ENABLED=false
OPENAI_CONFIDENCE_THRESHOLD=0.80
OPENAI_VISION_MODEL=gpt-5.6-luna
OPENAI_VISION_SERVICE_TIER=default
OPENAI_VISION_REASONING_EFFORT=low
OPENAI_PLANNER_MODEL=gpt-5.6-terra
OPENAI_PLANNER_SERVICE_TIER=default
OPENAI_PLANNER_REASONING_EFFORT=low
```

Only `default` and `flex` service tiers are accepted. Supported reasoning efforts are `none`, `low`,
and `medium`; supported image details are `low`, `auto`, and `high`. The application rejects
`OPENAI_STORE=true` and always sends `store=False` to the Responses API.

`.env` is ignored by Git. Never commit an API key, PiKVM credential, TOTP code, real work
screenshot, or analysis overlay.

## Manage PiKVM profiles

`.env` profiles stay as they are. You can also add, disable, enable, remove, and test profiles
from the dashboard's Profiles panel or the CLI; the dashboard calls the same service, so neither
has authority the other lacks:

```bash
pikvm-agent profiles list
pikvm-agent profiles add lab --url https://100.64.0.9 --username admin
pikvm-agent profiles enroll-2fa lab --qr ~/Downloads/pikvm-totp.png
pikvm-agent profiles test lab
pikvm-agent profiles disable heidrick
pikvm-agent profiles enable heidrick
pikvm-agent profiles remove lab
```

`add` prompts for the password and stores it in macOS Keychain (service
`pikvm-work-agent.password`); the non-secret fields go to
`~/Library/Application Support/pikvm-work-agent/profiles.json` (owner only). Disabled profiles are
skipped by `--all-kvms`, the scheduler, and the dashboard, and selecting one explicitly says that it
is disabled. `.env` profiles can be disabled but not removed here. `enroll-2fa` decodes the
provisioning QR locally and verifies with a screenshot read; in the dashboard the same enrollment
takes a dropped image and never writes it to disk.

## Enroll automatic PiKVM TOTP

Place or crop the existing PiKVM TOTP provisioning QR into the Git-ignored `.local-secrets/`
directory, then import it. QR detection, provisioning-URI parsing, code generation, and Keychain
storage all happen locally; the image, URI, seed, and generated code are never sent to OpenAI. Run
this once for each profile that has `TOTP_REQUIRED=true`:

```bash
pikvm-agent --profile heidrick auth keychain import-qr \
  --qr .local-secrets/heidrick.totp-qr.png \
  --delete-qr-after-success
pikvm-agent --profile heidrick auth keychain status
pikvm-agent --profile heidrick auth keychain verify
```

The import accepts PNG and JPEG files containing exactly one QR code, requires a standard
six-digit, 30-second, SHA-1 `otpauth://totp` credential, reads the stored entry back, generates a
code locally, and authenticates to PiKVM through the existing read-only screenshot path. It never
silently overwrites an entry. `--delete-qr-after-success` deletes only the supplied image and only
after all those checks succeed. Without that option, the command leaves the image in place and
reminds you to remove it.

`.local-secrets/` and conventional `*.totp-qr.png`/`.jpg` names are ignored by Git, but Git ignore
is not encryption or secure storage. A provisioning QR is equivalent to possession of the TOTP
setup credential. Keychain is the permanent runtime store; the application never reads the QR
again after enrollment.

The default Keychain convention is:

```text
service: pikvm-work-agent.totp
account: heidrick-pikvm.example.local
```

Schemes, paths, queries, fragments, and default ports are excluded from the account. A non-default
port is retained, so separate endpoints and profiles do not accidentally share a seed. The selected
profile supplies the host. To prevent credentials crossing endpoints, a named profile cannot be
combined with a different auth `--host`; the option remains available for legacy single-KVM use.

Removal affects only the exact local generic-password entry and defaults to no:

```bash
pikvm-agent --profile heidrick auth keychain remove
```

It does not disable PiKVM 2FA, modify the PiKVM password, or touch Apple Passwords/another
authenticator. Arbitrary URLs, HOTP, empty/malformed secrets, ambiguous multiple QRs, and unsupported
TOTP parameters are rejected with sanitized errors.

If QR import is unavailable, the optional manual fallback uses a hidden terminal prompt and accepts
either Base32 or a provisioning URI. The value is never accepted as a command-line argument:

```bash
pikvm-agent --profile heidrick auth keychain setup-secret
```

For explicit manual debugging, set the selected profile's `TOTP_PROVIDER=interactive` to use the
existing hidden `PiKVM 2FA code:` prompt. Alternatively, keychain mode may use that prompt only
when that profile's `TOTP_INTERACTIVE_FALLBACK=true`. Fallback defaults to false so an unattended
session stops cleanly rather than hanging for input.

Automatic TOTP intentionally gives the Mac programmatic access to both the PiKVM password source
and the TOTP seed. Protecting the Mac and macOS account is therefore especially important. This
does not weaken or disable PiKVM server-side 2FA.

## Analyze an existing screenshot

Offline file analysis does not load PiKVM credentials, request PiKVM 2FA, or execute HID input:

```bash
pikvm-agent analyze-file pikvm-screen.jpg \
  --objective "Identify the visible application" \
  --json
```

Locate a relevant control and explicitly create a local overlay:

```bash
pikvm-agent analyze-file pikvm-screen.jpg \
  --objective "Locate the Slack profile control" \
  --overlay analysis-overlay.jpg \
  --json
```

The overlay draws relevant bounding boxes, labels, confidence, and the target click point. It is a
debugging artifact only; no click occurs. Use `--detail low|auto|high`,
`--reasoning-effort none|low|medium`, `--service-tier default|flex`, or `--model MODEL` to override
one run without changing `.env`.

## Analyze one live PiKVM screen

```bash
pikvm-agent analyze-screen \
  --objective "Identify the visible application" \
  --json
```

This loads the existing PiKVM configuration, obtains a fresh TOTP locally from the configured
provider, captures exactly one screenshot, closes the PiKVM client, analyzes the JPEG in memory, and
exits. It does not save the screenshot unless `--overlay` is explicitly supplied, and it performs
no HID action. With a working Keychain entry there is no terminal TOTP prompt.

Screen coordinates in structured output are integers from 0 through 1000. Pixel coordinates shown
by the human-readable CLI and overlay are local display conversions only. Confidence is an advisory
model signal; results below the configured threshold are marked unsafe and receive a
`low_confidence` warning.

## Capture a screenshot explicitly

```bash
pikvm-agent screenshot --output pikvm-screen.jpg
```

The configured Keychain seed is used to generate the current code locally; neither value is printed
or logged. Unlike `analyze-screen`, the screenshot command writes the image because saving is its
explicit purpose. If a safe read discovers expired authentication, it creates one fresh client/code
and retries once.

## Explicit HID commands

Milestone 2 provides one-operation commands:

```bash
pikvm-agent key Enter
pikvm-agent hotkey ControlLeft KeyL
pikvm-agent type "hello" --delay 0.05
pikvm-agent mouse-move 960 540 --screen-width 1920 --screen-height 1080
pikvm-agent click --button left
pikvm-agent scroll -120
```

Key names use PiKVM/DOM `KeyboardEvent.code` values. Mouse movement and clicking remain separate.
HID requests are never retried because a timeout can occur after PiKVM has already applied the
input. Authentication failure or transport ambiguity during HID also never replays input. These
commands are not connected to the OpenAI analyzer.

## Controlled agent commands

Inspect one complete proposal and policy decision with no HID:

```bash
pikvm-agent agent-step \
  --objective "Open Slack"
```

`agent-step` is dry-run by default. Add `--execute` to permit at most one locally confirmed HID
action followed by a fresh visual verification.

Run a bounded dry-run session:

```bash
pikvm-agent agent-run \
  --objective "Open Slack and stop when the main Slack window is visible" \
  --dry-run
```

The dry run captures one screen, analyzes it, plans one action, applies policy, prints what would
happen, and exits without HID. For explicitly requested local debug artifacts, add a Git-ignored
directory such as `--debug-dir .local-debug/session-001`. Those screenshots may contain private
work information.

Available controller safeguards include `--max-steps`, `--timeout`, `--approval-mode safe|every`,
`--step`, and model overrides. `safe` is the default approval mode. There is deliberately no
approve-everything mode, raw-coordinate autonomous action, shell action, power action, or direct
OpenAI-to-PiKVM tool path.

How the controller copes with the ordinary mess of a real desktop:

- an unexpected dialog, update prompt, or notification is a warning the planner routes around
  (bring the wanted application in front); policy denies answering the dialog itself;
- an uncertain verification earns one delayed re-observation, then bounded re-planning
  (`AGENT_MAX_VERIFICATION_RECOVERIES`); the unverified action is never proposed again and no HID
  request is ever replayed;
- model output the local code can repair is normalised, and a planner slip (an element id that is
  not clickable) earns one correction round;
- the PiKVM session logs in once and holds the session cookie, so a long session no longer loses
  authentication when the TOTP window rolls;
- everything the code decides is written to `~/Library/Logs/pikvm-work-agent/agent.log`
  (rotating, owner-only, no screen content), and failures are classified as `pikvm_unreachable`,
  `pikvm_auth_failed`, `model_provider_error`, `model_output_invalid`, or `internal_error`.

## Slack manual availability

Read or set one named KVM through Slack's visible GUI:

```bash
pikvm-agent slack availability get --kvm heidrick
pikvm-agent slack availability set active --kvm heidrick
pikvm-agent slack availability set away --kvm heidrick
```

Apply a requested state to every `PIKVM_PROFILES` entry in declaration order:

```bash
pikvm-agent slack availability set active --all-kvms
```

Each KVM runs to completion before the next starts. One failure is reported but does not stop later
profiles. The workflow opens Slack/profile navigation as needed, checks the manual availability
toggle first, avoids a click when the state is already correct, and accepts success only when a
fresh observation visibly proves the resulting state. It uses no fixed coordinates and cannot read
or send messages, edit Slack status text/emoji, change preferences, or simulate activity. An
unexpected approval requirement stops that KVM rather than blocking an unattended schedule.
When Slack is foreground but its profile menu is not visible, the workflow performs a focused
vision pass for the small account/avatar control. The focused point must be inside the returned
profile bounding box before it can reach policy or HID; no fixed coordinate is used. An invalid
focused result receives at most one retry with the configured fallback model at medium reasoning.
An ambiguous low-confidence/unknown screen initially classified as unsafe similarly receives one
read-only fallback-model confirmation. Authentication, lock, unexpected-dialog, destructive, and
disconnect warnings remain immediate stops and are never overridden.
If Slack applies a toggle with too little full-screen change for generic verification, the workflow
performs at most one separate read-only pass that reopens the profile menu and interprets its manual
toggle. It never repeats the availability click during that verification pass.

For manual diagnosis, add `--trace` to a direct availability command. It prints only sanitized
controller states, action types, resolved normalized/pixel coordinates, policy decisions, and
verification outcomes; it does not print screenshots or visible Slack content.

Sanitized JSONL operation records are stored at
`~/Library/Logs/pikvm-work-agent/slack-availability.jsonl`. They contain the KVM name, requested and
observed availability, changed/no-op result, outcome, a machine-readable `stop_code`, and a sanitized
error. Screenshots, visible Slack content, credentials, TOTP values, and API keys are never written
there.

`stop_code` names exactly why the controller stopped — for example `completion_unverified`,
`policy_denied`, `transport_failed`, `screen_unsafe`, or `stuck_no_screen_change`. Classify failures
from that field rather than from the error text, which is prose and may be reworded. Records written
before this field existed have `stop_code: null` and appear as "Recorded before stop codes existed"
in the dashboard rather than being guessed at.

## Slack inbox triage

Read what Slack already shows about unread activity, for one KVM or every configured KVM:

```bash
pikvm-agent slack triage --kvm heidrick
pikvm-agent slack triage --all-kvms
```

Output groups unread entries by how much they appear to want:

```text
heidrick  ✓ needs attention: 2
    @ #deploys (1)
    · patrick (2)
  FYI: 1
    · #general (7)
    ! sidebar was clipped; more unread entries may exist below
```

`@` marks a mention badge, `·` a direct message or an ordinary unread channel.

**This never opens a conversation.** Opening one marks it read in Slack, which cannot be reliably
undone and would destroy the unread state triage exists to report. The workflow therefore reads only
the conversation sidebar: names, unread counts, mention badges, and muted state. It runs in two
separate phases — a bounded controller brings Slack to the foreground, then a plain screenshot is
read with no executor attached, so the reading phase is structurally incapable of sending HID.

The boundary is positional rather than a list of approved labels: input is permitted only while
Slack is not yet the foreground application, and once Slack is in front there is nothing left to
click. A target whose name begins with `#` is also refused, since that unambiguously names a channel.
Everything else is ordinary navigation and is left to the generic policy engine, so a Dock icon with
an unread badge still works.

Because the sidebar carries no message text, triage reports *that* something is unread, never what it
says. Ranking mentions above direct messages above channels is the most a non-destructive read
supports; judging whether a message is a question, a blocker, or an approval request needs the
conversation opened, which this workflow will not do.

The dashboard's **Inbox & triage** panel shows the same results with full detail — conversation
name, kind, unread count, mention badge, and why each entry ranked where it did.

Counts only are recorded to `~/Library/Logs/pikvm-work-agent/slack-triage.jsonl` — the number of
unread conversations, mentions, and direct messages. Conversation names are shown live in the
terminal and the dashboard but never written to disk.

## Today's meetings

Read the meetings already visible on a calendar that is open on the remote machine, for one KVM or
every configured KVM:

```bash
pikvm-agent calendar today --kvm heidrick
pikvm-agent calendar today --all-kvms
```

Output leads with what is still ahead, then lists what has already finished:

```text
heidrick  ✓ Tuesday, 12 August · clock 10:04 AM — 3 still ahead
    ▸ 9:45 AM–10:30 AM  Sprint review (online, in progress)
    · all day  Company all-hands
    · 11:00 AM–11:30 AM  Design sync (Room 4)
  Earlier today: 1
    · 9:00 AM–9:15 AM  Standup (Room 1)
    ! the day was still clipped; later meetings may exist below
```

`▸` marks a meeting already running, `·` everything else.

**Microsoft Teams is tried first.** Its Dock or taskbar icon is clicked, then its Calendar rail if a
different Teams section is showing. Only when Teams cannot be reached at all does it fall back to a
browser window whose calendar is already open.

**This never opens or joins a meeting.** A calendar has two controls that a read-only workflow must
never touch: *Join*, which would place you into a live call with microphone and camera, and
*Accept*/*Decline*/*Tentative*, which would send a reply on your behalf. Both are denied by name in
the policy engine, along with editing event text. Ordinary navigation — a Dock icon, Teams' Calendar
rail, an already-open browser tab — is left to the generic policy, so the workflow can still reach
the calendar.

It never opens a new tab, navigates to a URL, or signs in anywhere. If neither Teams nor an open
calendar tab can be reached it stops and says so rather than guessing.

### A dialog covering the screen

Work machines often have a system dialog parked in the middle of the screen — a corporate update
prompt, say — and the generic controller stops on any unexpected dialog. That would make this
workflow permanently unusable on exactly the machines that have one.

So the calendar workflow walks *around* the dialog instead of answering it. Clicking the calendar
application's own Dock icon brings its window to the front, which puts the dialog behind it — the
same thing a person does. The dialog is never clicked, never dismissed, and never sent an Escape,
and the policy engine separately refuses to click anything inside it: *Open Software Update*,
*Install*, *Restart*, and even *Later* or *Remind me*.

This is the only safety relaxation, and it is narrow: only `unexpected_dialog` is cleared, only when
it is the sole problem. An authentication prompt, lock screen, destructive confirmation,
disconnected feed, or low-confidence read still stops the session exactly as before. The trace says
plainly when a dialog was stepped around.

A run that stops because the screen could not be used safely now says so, rather than reporting that
no calendar was found — those look alike and mean opposite things.

A week view is read as-is — only today's column is reported, identified by the header the calendar
marks as today. Teams opens on a week grid by default, so refusing week views would refuse the
normal case.

Times are compared against the clock read from the *same screenshot* — the remote machine's menu bar,
taskbar, or the calendar's current-time line — not this Mac's clock, because the two may be in
different timezones. When that clock cannot be read, nothing is marked as already over and the output
says why. A calendar left on a day other than today is refused rather than reported.

When the day is clipped, a bounded scroll (at most three rounds, stopping early once nothing new
appears) reveals later meetings, and each read is merged into one list.

The dashboard's **Today's meetings** panel shows the same results with full detail — time, title,
location, organizer, online and declined tags, and whether anything was clipped or covered.

Counts only are recorded to `~/Library/Logs/pikvm-work-agent/calendar-agenda.jsonl`. Meeting titles,
organizers, and locations are shown live in the terminal and the dashboard but never written to disk,
for the same reason Slack conversation names are not.

## Meeting capture and intelligence

Start and stop a recording explicitly, from the CLI or the dashboard's **Meeting recorder** panel
(start/stop/status, the list of recorded sessions, and a report viewer with the summary, action
items grouped by ownership, decisions, the transcript, and `report.md`):

```bash
pikvm-agent meeting start --kvm nbc_kvm
pikvm-agent meeting status
pikvm-agent meeting stop
pikvm-agent meeting list
pikvm-agent meeting show --session-id meeting-…
```

First-time hardware check in one command - play audio you are allowed to record on the remote
computer, then:

```bash
pikvm-agent meeting validate --kvm nbc_kvm --seconds 60
```

It starts a recording, reports progress every 15 s, stops after the requested time, transcribes,
extracts action items, and prints the summary, your action items, decisions, and the report path.

`start` opens a separate authenticated, receive-only WebRTC viewer connection to the selected
PiKVM. It requests PiKVM's incoming HDMI audio with `audio=true`, `mic=false`, and `camera=false`;
the audio transceiver is `recvonly`, the video transceiver is inactive, and the implementation has
no Mac audio-input path. It does not tap the browser, open a meeting, join a call, use a meeting bot,
or install anything on the remote computer. Recording never starts automatically.

Only one recording can be active at a time. This makes the selector-free `meeting stop` unambiguous
and prevents audio from two named KVMs being mixed. A second `start` is refused before it opens
another connection.

The recorder writes private five-minute Ogg/Opus parts. `start` reports success only after an audio
track has negotiated and an incoming frame has arrived. `stop` asks the detached Mac-local worker to
finish the current part, then:

1. transcribes every finalized part with timestamps and diarization;
2. converts provider labels to anonymous `Speaker 1`, `Speaker 2`, and so on;
3. extracts a strict evidence-linked meeting record;
4. applies a local ownership guard; and
5. writes a deterministic Markdown report.

If WebRTC disconnects after audio has arrived, finalized parts are preserved. `meeting status`
reports the interruption and a later `meeting stop` processes what was safely finalized. If
transcription or intelligence fails, the audio and any completed intermediate stage remain in place;
running `meeting stop` again resumes rather than recording again. A stop timeout never force-kills
the worker.

### Work identity and ownership

Configure identity independently for each named PiKVM:

```dotenv
PIKVM_HEIDRICK_WORK_IDENTITY_NAME=Shafiq
PIKVM_HEIDRICK_WORK_IDENTITY_ALIASES=Shafiq,Shafique
```

The selected profile's identity is snapshotted into that recording's protected manifest. An exact
name or alias assignment can become `our_identity`. Context such as “your side” may become only
`possibly_our_identity`, at medium confidence or lower, with a reason. Ambiguous ownership remains
unknown, and another KVM never inherits Heidrick's identity.

Identity is about action ownership, not voice recognition. Textual aliases are never sent as known
speaker references and never rename `Speaker N`.

### Providers and local artifacts

Transcription and intelligence are separate provider boundaries:

```dotenv
MEETING_TRANSCRIPTION_PROVIDER=deepgram   # or openai
DEEPGRAM_API_KEY=…
DEEPGRAM_MODEL=nova-3
DEEPGRAM_LANGUAGE=en
MEETING_INTELLIGENCE_PROVIDER=openai
OPENAI_MEETING_MODEL=gpt-5.6-terra
```

With `deepgram`, each finalized audio part is posted once to Deepgram's pre-recorded `/v1/listen`
endpoint with diarization and utterances; speakers become anonymous `Speaker N` exactly as with
the OpenAI transcriber, and the raw response is cached beside the part so a retry never re-uploads
it. With `openai`, the Audio transcription endpoint (`gpt-4o-transcribe-diarize`) is used instead.
Intelligence (summary, action items, decisions) always uses the OpenAI meeting model through the
stateless Responses path with `store=false`. Audio goes only to the configured transcription
provider; the timestamped transcript and the selected KVM's identity go only to the intelligence
provider. PiKVM credentials, TOTP values, unrelated profile identities, and artifact paths are
never included.

Artifacts are stored with `0700` directories and `0600` files in a stable Mac-local directory:

```text
~/Library/Application Support/pikvm-work-agent/meetings/<kvm>/<date>/<session-id>/
  manifest.json
  audio-0001.ogg
  transcript.json
  intelligence.json
  report.md
```

The built-in directory is outside the repository; `.local-data/` also remains Git-ignored for an
explicit project-local override. Use an absolute `MEETING_DATA_DIR` for any override. Normal logs
contain no audio, transcript, summary, speaker, identity, or action-item content. Recordings and
reports can contain sensitive work information: retain them only as long as needed and follow the
meeting participants' and organization's recording rules.

To clean up, first confirm `pikvm-agent meeting status` is not recording or processing, then remove
only the exact completed session directory shown by the CLI. There is deliberately no automatic
retention or broad cleanup command.

### Manual meeting validation

After setting the Heidrick identity variables above and confirming the test audio is already audible
through PiKVM, start one short, harmless recording:

```bash
pikvm-agent meeting start --kvm heidrick
```

Play or join only content you are permitted to record, then finish it explicitly:

```bash
pikvm-agent meeting stop
```

Open the printed `report.md` and verify useful timestamps and anonymous speaker separation, that a
direct assignment to Shafiq appears under **OUR ACTION ITEMS**, that indirect wording remains under
**POSSIBLE OUR ACTION ITEMS**, and that other participants' work stays separate. This is the required
real PiKVM/provider validation; the automated suite uses mocked media and provider boundaries and
does not claim hardware audio coverage.

## Mac-local Slack availability schedule

Inspect and exercise reconciliation before installing anything:

```bash
pikvm-agent schedule slack-availability run-now
pikvm-agent schedule slack-availability reconcile
```

`run-now` applies the state currently required by the Asia/Karachi schedule to all configured KVMs.
For an explicit harmless scheduler test, it also accepts `--availability active|away`.
`reconcile` calculates desired state without an LLM and checks/applies it. The periodic launchd
reconciler uses a local success-state file to avoid PiKVM/OpenAI calls when the same desired state
was already verified.

Every scheduler invocation makes one initial attempt. If any profile fails, the same scheduler
process waits five minutes and retries only the failed profiles, for at most two retry rounds.
Successful profiles are recorded after each round and are not repeated. Before each retry the
desired state is re-read from the clock, so a retry that crosses 18:00 or 02:00 applies the state
that is now due rather than the one that just expired. Each retry starts a fresh, idempotent
OBSERVE → REASON → ACT → VERIFY workflow, so HID operations themselves are never blindly replayed.
Direct `pikvm-agent slack availability ...` commands remain single-attempt. An unreachable PiKVM
fails in seconds with `pikvm_unreachable` and no model calls.

Availability navigation has one additional bounded recovery. A fresh screen that visibly contains
Slack's exact manual Active/Away control proves that the profile menu opened, even when it correctly
still shows the pre-change state. If a validated Slack/profile navigation click remains visually
uncertain, the workflow may start one fresh observe-first controller pass. An uncertain availability
toggle is never repeated by that recovery path; it receives only a read-only final-state check. Final
success still requires fresh visible evidence of the requested Active/Away state.

Install three inspectable per-user LaunchAgents (18:00 weekdays, 02:00 Tuesday-Saturday, and
hourly), show their state, or remove only those generated files. All three run
`reconcile --if-due` and compute the desired state at fire time, so a trigger launchd replays late
after sleep is a harmless no-op:

```bash
pikvm-agent schedule slack-availability install
pikvm-agent schedule slack-availability status
pikvm-agent schedule slack-availability uninstall
```

The files live under `~/Library/LaunchAgents/`; no root access or remote-machine scheduler is used.
Because launchd calendar triggers use the Mac's system time zone, installation requires the Mac
system time zone to be `Asia/Karachi`. The jobs record an absolute interpreter path and the current
repository as their working directory, so keep both paths available after installation.
Local state is stored at
`~/Library/Application Support/pikvm-work-agent/slack-availability-state.json`.

Install writes a plist only after confirming that the recorded interpreter can import `work_agent`
from the recorded working directory. Run it with the interpreter that has the project installed:

```bash
.venv/bin/python -m work_agent schedule slack-availability install
```

Installing with a bare `python3` records that interpreter instead, and every scheduled run then
fails with `No module named work_agent` while `launchctl` still reports the job as loaded. `status`
therefore reports the recorded interpreter, probes it, and exits non-zero when scheduled runs cannot
actually work.

## Local operations dashboard

Serve a read-and-run console for the existing workflows on loopback only:

```bash
pikvm-agent dashboard
```

It binds `127.0.0.1:8787`, prints the URL, and opens a browser tab. `--host` accepts only a loopback
address, `--port` accepts 1024 through 65535, and `--no-browser` suppresses the automatic tab.

The sidebar leads with the seven working sections. Future skills live in a compact **Coming later**
disclosure instead of competing with controls that work today. On phones it becomes one labelled
section picker, while the current page is also reflected in the URL fragment and page heading.

A **fleet rail** shows one card per listed KVM: its freshest verified availability, whether that
matches what the schedule requires now, readiness, its selected-range completion ratio, and explicit
Check / Set active / Set away actions. Current state is resolved independently of the selected
history range and uses per-KVM verification times. The rail is fully expanded on Overview and
collapses to a compact status summary on focused sections.

Sections:

- **Overview** — reliability counts (workflows completed, states changed, already-correct no-ops,
  read-only checks), stop reasons grouped into the roadmap's GUI-reliability categories, and the
  most recent runs.
- **Slack › Availability** — all-environment actions plus a per-environment table of observed state,
  required state, last verified state, and actions.
- **Inbox & triage** — visible unread signals grouped into attention and informational sections,
  with capture time, incomplete-result warnings, and an explicit clear action.
- **Today's meetings** — what is still ahead on an already-open calendar, with an in-progress
  meeting first and finished ones below, plus the remote clock the split was based on, capture time,
  incomplete-result warnings, and an explicit clear action.
- **Schedule** — a plain-language nightly rule, required state, next transition, covered
  environments, last dashboard sync, and one primary Sync action. Interpreter, LaunchAgent, repair,
  test, and remove controls are kept in a diagnostics disclosure.
- **Availability history** — sanitized stop reasons and the full run history, filterable by KVM and
  outcome.
- **Remote screen** — one temporary PiKVM frame with clear and full-size controls. The browser
  revokes it when the user leaves the section or after five minutes.

Long-running work streams into a recoverable **Task center** docked at the bottom. Independent jobs
remain visible when another KVM starts work, mixed batches are labelled **Completed with issues**,
and a browser refresh reconnects to work still running in that dashboard process. Technical trace
text is available on demand without being announced line by line. The drawer is height-capped and
scrollable, and page content reserves its measured height. Charts each have a table view.

Dashboard refreshes retain previously loaded panels when one source fails, identify the stale source,
and keep current-state reads separate from the selected analytics range. Batch workflows run every
eligible profile and return an explicit skipped result for each unconfigured or interactive-TOTP
profile instead of aborting ready profiles.

The dashboard is a local view over the same code paths as the CLI. It adds no new automation
authority: runs go through the same bounded controller, the same local policy, and the same visible
verification, and it uses the noninteractive approval provider, so an unexpected approval requirement
stops that KVM instead of waiting. It cannot read or send Slack messages, edit status text or emoji,
or act on raw coordinates.

Safety properties worth knowing:

- non-loopback `Host` headers are refused, so DNS rebinding cannot drive HID from another page;
- every API request requires a per-session token that is generated at startup and embedded in the
  served page, never placed in a URL;
- one workflow at a time per KVM; a second request for a busy KVM is refused rather than queued;
- passwords, TOTP seeds, generated codes, and API keys are never sent to the browser;
- a profile configured for interactive TOTP entry is rejected before a run starts, because a server
  must never block on a terminal prompt;
- job responses and screenshots are served with `no-store`; screenshots are never written to disk.

Captured frames show real work-computer content in a browser tab. Close the tab when finished.

## Milestone 5 manual validation

Do not install the schedule until the direct GUI workflow succeeds. Run these in order with the
actual configured profile names:

```bash
pikvm-agent slack availability get --kvm heidrick
pikvm-agent slack availability set active --kvm heidrick
pikvm-agent slack availability set away --kvm heidrick
```

Then validate sequential multi-KVM processing and deterministic scheduling without waiting for a
timer:

```bash
pikvm-agent slack availability set active --all-kvms
pikvm-agent schedule slack-availability reconcile
pikvm-agent schedule slack-availability run-now --availability away
```

After the results are visually correct on every KVM, install and inspect launchd:

```bash
pikvm-agent schedule slack-availability install
pikvm-agent schedule slack-availability status
```

Confirm that KVMs are handled one at a time, already-correct states report a no-op, each changed
state is visible in Slack, automatic Keychain TOTP does not prompt, and all three LaunchAgents show
`installed=yes, loaded=yes`. Do not test message reading/sending; that belongs to a later milestone.

## Private manual evaluation files

Put real screenshots and any expected-results manifest under `evals/private/`. Put temporary TOTP
QR images under `.local-secrets/`. Those directories and the standard screenshot/overlay/QR names
are ignored by Git. Unit tests use generated neutral images and fake TOTP QR codes plus mocked
Keychain, PiKVM, and OpenAI responses; they never spend API credits or access real credentials.

## Diagnose a failed run

```bash
tail -50 ~/Library/Logs/pikvm-work-agent/agent.log
tail -5 ~/Library/Logs/pikvm-work-agent/slack-availability.jsonl
```

The JSONL carries the stop code, the sanitized reason, and per-run telemetry (sessions, steps, HID
actions, model calls, tokens, runtime); `agent.log` carries the step-by-step controller trace and
the exception class behind any error. Neither contains screen content.

## Run automated checks

```bash
pytest
ruff check .
ruff format --check .
mypy
```

These checks do not access the real macOS Keychain, contact PiKVM/OpenAI, consume API credits,
prompt for credentials, or send keyboard/mouse input.

## API references

- [PiKVM HTTP API](https://docs.pikvm.org/api/)
- [zxing-cpp Python bindings](https://pypi.org/project/zxing-cpp/)
- [Python keyring](https://pypi.org/project/keyring/)
- [PyOTP](https://pyauth.github.io/pyotp/)
- [OpenAI Responses API](https://developers.openai.com/api/docs/guides/migrate-to-responses)
- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [OpenAI image inputs](https://developers.openai.com/api/docs/guides/images-vision)
