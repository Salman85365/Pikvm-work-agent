# PiKVM Work Agent — Long-Term Roadmap

## 1. Vision

Build a personal engineering/work automation system that runs on the
user's Mac and operates remote work computers exclusively through PiKVM.

The remote computers remain agentless.

The long-term goal is to move from:

> "Open PiKVM and manually perform every task."

toward:

> "Tell the local Work Agent what needs to be accomplished, and let it
> perform safe routine work while escalating decisions and consequential
> actions to the user."

The system should eventually behave like a personal work assistant capable
of operating multiple isolated work environments.

It should understand and interact with:

- Slack
- browsers
- ticketing systems
- VS Code
- terminals
- Git
- test suites
- CI systems
- PR/review interfaces
- other internal applications visible through PiKVM

without installing automation software on those remote machines.

---

# 2. Core Architecture

The architectural boundary remains:

                        USER
                          │
                          ▼
                ┌───────────────────┐
                │ PiKVM Work Agent  │
                │      on Mac       │
                └─────────┬─────────┘
                          │
             ┌────────────┴────────────┐
             │                         │
             ▼                         ▼
        Local services             OpenAI API
        - scheduling               - vision
        - state                    - reasoning
        - policies                 - planning
        - task DB
        - Keychain
             │                         │
             └────────────┬────────────┘
                          ▼
                  Generic Controller
                          │
             OBSERVE → REASON → ACT → VERIFY
                          │
                     Policy Layer
                          │
                          ▼
                     PiKVM Client
                          │
              ┌───────────┼───────────┐
              ▼           ▼           ▼
            KVM A       KVM B       KVM C
              │           │           │
         Work PC A    Work PC B    Work PC C

Remote machines receive only ordinary:

- display capture
- keyboard input
- mouse input

through PiKVM.

No remote automation agent is required.

---

# 3. Permanent Engineering Principles

These principles should survive every future milestone.

## Agentless remote machines

Do not require installation of:

- Python
- ChatGPT/OpenAI clients
- browser extensions
- remote daemons
- automation services
- Slack integrations
- GitHub integrations
- custom agents

on the remote work computers.

## Observe → Reason → Act → Verify

Never assume a GUI operation succeeded.

Every meaningful action should eventually be verified from a fresh screen
state.

## One controlled action at a time

Avoid long blind sequences such as:

    click
    click
    type
    enter
    click

Prefer:

    observe
    click
    verify

    observe
    type
    verify

    observe
    enter
    verify

## Deterministic actions first

Prefer reliable keyboard operations over visual clicking where practical.

Examples:

- OS search to open applications
- keyboard shortcuts
- browser address-bar shortcuts
- known application shortcuts

Use vision where UI understanding is actually necessary.

## Policy before execution

Model output is treated as untrusted input.

The local policy engine determines whether an action is:

- automatically allowed
- approval-required
- denied

## Human approval for consequential actions

Examples that should normally require approval:

- sending external messages
- pushing Git changes
- merging PRs
- deleting files
- deleting branches
- deployments
- production changes
- destructive confirmations

## Fail safely

When uncertain:

STOP rather than guess.

Examples:

- unexpected authentication prompt
- low-confidence target
- unfamiliar dialog
- disconnected PiKVM
- stale screen
- ambiguous HID result
- destructive UI

---

# 4. Current Foundation

## Milestone 1 — PiKVM Transport ✅

Capabilities:

- PiKVM authentication
- screenshot retrieval
- keyboard input
- hotkeys
- text input
- mouse positioning
- clicks
- double-click
- scrolling
- retries for safe read operations
- safe handling of HID operations

Real hardware verified.

---

## Milestone 2 — HID Validation ✅

Explicit keyboard and mouse actions have been validated against real
PiKVM hardware.

Important invariant:

Ambiguous HID operations must never be automatically retried in ways that
could duplicate clicks or keystrokes.

---

## Milestone 3 — Screen Understanding ✅

The agent gained EYES.

Capabilities include:

- screenshot → OpenAI vision
- structured screen analysis
- application identification
- UI-state identification
- element localization
- normalized coordinates
- confidence information
- safe/unsafe state detection
- visual overlays
- API telemetry

Screenshots remain ephemeral by default.

OpenAI API calls use:

    store=False

---

## Milestone 4 — Generic Safe Agent Controller ✅

The agent gained controlled HANDS.

Architecture:

    OBSERVE
        ↓
    ANALYZE
        ↓
    PLAN ONE ACTION
        ↓
    POLICY CHECK
        ↓
    STALE-SCREEN CHECK
        ↓
    EXECUTE
        ↓
    SCREEN SETTLE
        ↓
    OBSERVE AGAIN
        ↓
    VERIFY

Capabilities include:

- action planning
- action policy
- approval handling
- stale-screen protection
- local screen-change detection
- screen-settle detection
- loop protection
- runtime limits
- step mode
- dry-run mode
- single-controller locking
- execution telemetry

Real harmless multi-step PiKVM navigation has been verified.

---

## Milestone 4.5 — Automatic PiKVM TOTP

Goal:

Remove repeated human TOTP entry while keeping PiKVM server-side 2FA
enabled.

Architecture:

    local QR enrollment
          ↓
    local QR decode
          ↓
    macOS Keychain
          ↓
    local TOTP generation
          ↓
    PiKVM authentication

Secrets never go to OpenAI.

Runtime does not depend on the QR image after enrollment.

---

# 5. Phase I — Useful Work Automation

These milestones should deliver immediate daily productivity.

---

# Milestone 5 — Slack Availability Automation

## Goal

Automatically manage Slack Active/Away state across all configured work
KVMs.

Desired schedule:

    ACTIVE
    Monday-Friday
    18:00 Asia/Karachi

    AWAY
    Tuesday-Saturday
    02:00 Asia/Karachi

The cross-midnight schedule represents:

    Monday 18:00 → Tuesday 02:00
    ...
    Friday 18:00 → Saturday 02:00

## Capabilities

    slack availability get
    slack availability set active
    slack availability set away

Single KVM and:

    --all-kvms

support.

Multiple KVMs run sequentially.

## Scheduler

Use macOS launchd.

Add:

    schedule install
    schedule uninstall
    schedule status
    schedule run-now
    schedule reconcile

## Desired-state reconciliation

The system should calculate what Slack availability SHOULD currently be.

Example:

    Monday 20:00
    → active

    Wednesday 01:00
    → active

    Wednesday 03:00
    → away

This allows recovery after:

- Mac reboot
- sleep
- missed schedule
- temporary PiKVM outage

## Boundary

Do not simulate artificial activity.

The feature controls Slack's explicit Active/Away setting only.

---

# Milestone 6 — Slack Inbox & Triage

## Goal

Allow the Work Agent to inspect Slack across all KVMs and tell the user
what genuinely needs attention.

Command concept:

    pikvm-agent slack check --all-kvms

## Detect

- unread DMs
- unread channels
- mentions
- replies to user's threads
- review requests
- explicit questions
- blockers
- requests for updates
- requests for approval
- messages likely requiring a response
- informational/FYI messages

## Output

Example:

    HEIDRICK

    Needs attention: 3

    Patrick
    Asked whether the retry publisher has been validated.

    Anthony
    Asked whether enrichment is working after the infrastructure fix.

    Shafiq
    Asked whether the PR is ready for review.

    FYI: 4

No messages are sent.

## Important capability

The agent should understand conversation context rather than simply
reporting every unread badge.

This is the first major INFORMATION TRIAGE skill.

---

# Milestone 7 — Slack Reply Drafting

## Goal

Turn Slack triage into actionable response suggestions.

Workflow:

    find important message
           ↓
    inspect conversation context
           ↓
    understand request
           ↓
    gather available work context
           ↓
    draft concise response
           ↓
    show user

Example:

    Patrick asked:
    "Any update on the retry publisher?"

    Suggested response:

    "The infra-side issue is resolved. I'm validating the retry publisher
    now and will update the ticket once that is confirmed."

## Important boundary

Drafting is automatic.

Sending remains separate.

No message should be sent simply because a draft exists.

---

# Milestone 8 — Approved Slack Communication

## Goal

Allow actual Slack messages after explicit approval.

Workflow:

    draft
      ↓
    user approves
      ↓
    open Slack composer
      ↓
    type message
      ↓
    verify composer text
      ↓
    policy approval
      ↓
    send
      ↓
    verify message appeared

Example:

    Proposed external action:

    Send reply to Patrick:

    "..."

    Approve? [y/N]

## Future policy options

Eventually some low-risk categories could be configured differently, but
the default should remain conservative.

---

# Milestone 9 — Daily Work Brief

## Goal

At the beginning of the work period, automatically determine what deserves
attention across all work environments.

Potential scheduled time:

    shortly after Slack availability becomes active

Example output:

    DAILY WORK BRIEF

    Heidrick
    - 2 Slack messages need responses
    - PR review feedback from Patrick
    - Ticket 743 mentioned again
    - latest build appears successful

    Other Work Environment
    - 1 review request
    - no urgent Slack activity

    Suggested priority:

    1. Reply to Patrick
    2. Address PR feedback
    3. Verify ticket 743
    4. Check secondary review request

## Value

Instead of manually opening every KVM every day, the user receives a
single prioritized starting point.

---

# 6. Phase II — Development Assistant

After Slack/context automation is reliable, begin automating engineering
work.

---

# Milestone 10 — Development Environment Inspection

## Goal

Give the agent read-oriented understanding of the development environment.

Supported interfaces may include:

- VS Code
- terminal
- browser
- Git UI
- Jenkins
- internal CI dashboards
- ticketing systems

Initial skills:

    current branch
    git status
    inspect git diff
    identify modified files
    inspect test results
    inspect build result
    inspect linter output
    inspect current ticket

Example request:

> Check whether my current branch has uncommitted changes.

Agent:

    open terminal
    ↓
    git status
    ↓
    read output
    ↓
    summarize

No source modification required.

---

# Milestone 11 — Controlled Terminal Skills

## Goal

Allow safe, known development commands through the remote terminal.

Initially allow a restricted command family such as:

    pwd
    ls
    git status
    git diff
    git branch --show-current
    pytest <known target>
    ruff check
    mypy

## Policy categories

### Automatically allowed

Known read-only commands.

### Allowed with normal agent policy

Known test/lint commands.

### Approval required

    git commit
    git push
    migrations
    package installation
    deployment commands
    database mutation

### Denied by generic automation

Unclassified destructive shell commands.

## Request example

> Run the relevant tests for my current work and tell me what's failing.

The agent:

    opens terminal
    ↓
    runs known test command
    ↓
    waits for completion
    ↓
    reads output
    ↓
    classifies failures
    ↓
    reports

---

# Milestone 12 — PR / Review Feedback Assistant

## Goal

Handle the repetitive cycle around code review.

Workflow:

    detect review notification
            ↓
    open PR/review interface
            ↓
    collect review comments
            ↓
    correlate comments with code
            ↓
    summarize requested changes

Example:

    PR REVIEW

    3 comments

    1. serializer.py
       Reviewer wants validation moved to shared helper.

    2. tests/test_open_items.py
       Missing regression case.

    3. UI
       Reviewer requested disabled-state handling.

## Advanced mode

For straightforward comments:

    inspect comment
    ↓
    inspect local code
    ↓
    make focused change
    ↓
    run relevant tests
    ↓
    inspect diff
    ↓
    STOP

Report:

    2 review comments addressed.

    Files changed:
    - serializer.py
    - test_open_items.py

    Tests:
    18 passed

    No commit or push performed.

---

# Milestone 13 — Ticket → Implementation Workflow

## Goal

Allow the agent to understand an assigned engineering ticket and carry the
work through a controlled implementation cycle.

Example:

> Work on SCR-4238.

Workflow:

    open ticket
        ↓
    understand requirements
        ↓
    inspect linked Slack discussions
        ↓
    inspect relevant code
        ↓
    propose implementation plan
        ↓
    USER / POLICY GATE
        ↓
    implement
        ↓
    run tests
        ↓
    inspect diff
        ↓
    summarize result
        ↓
    USER decides commit/push

## Required phase boundaries

### Automatic

- gather context
- inspect repository
- understand task
- propose plan
- run read-only inspection

### Controlled

- code modification
- running tests
- fixing deterministic failures

### Approval

- commit
- push
- merge
- destructive operations

This milestone is the beginning of meaningful autonomous engineering work.

---

# 7. Phase III — Personal Work Orchestration

At this stage the agent should begin managing work, not merely individual
applications.

---

# Milestone 14 — Local Work Queue

## Goal

Create a local source-of-truth for tasks discovered across isolated work
environments.

Use a local database, likely SQLite initially.

Conceptual schema:

    Task
    ├── id
    ├── title
    ├── source
    ├── source_reference
    ├── KVM
    ├── project
    ├── priority
    ├── status
    ├── created_at
    ├── updated_at
    ├── last_checked
    ├── requires_response
    └── requires_approval

Sources might include:

- Slack
- ticket system
- PR review
- build failure
- user-created task

## Example

Slack:

    Patrick:
    "Can you validate the retry publisher?"

becomes:

    Task #81
    Project: Heidrick
    Source: Slack
    Priority: medium
    Status: open

Command:

    pikvm-agent tasks

Output:

    1. [Heidrick] Validate retry publisher
    2. [Heidrick] Reply to Patrick
    3. [Natrium] Address review comments
    4. [NBC] Prepare standup update

## Important feature

Deduplicate tasks.

The same request encountered multiple times should not create endless
duplicates.

---

# Milestone 15 — Standup / Daily Update Generator

## Goal

Automatically prepare concise work summaries.

Sources:

    Slack discussions
        +
    local task history
        +
    Git activity
        +
    test runs
        +
    tickets
        +
    PR activity

Output example:

    • Completed ticket 743 identity attribute changes.
    • Addressed PR review comments and reran tests.
    • Investigated enrichment consumer issue after infrastructure fix.
    • Retry publisher validation remains pending.
    • Next: complete validation and update Patrick.

The output is prepared automatically.

Sending remains approval-controlled.

Potential commands:

    pikvm-agent standup today
    pikvm-agent standup yesterday
    pikvm-agent standup --kvm heidrick
    pikvm-agent standup --all-kvms

---

# Milestone 16 — Proactive Monitoring

## Goal

Move from:

> "Check whether something happened."

to:

> "Tell me when something actually requires my attention."

During configured work hours:

    lightweight scheduled check
             ↓
    inspect Slack/UI indicators
             ↓
    anything changed?
          /       \
        no         yes
        ↓           ↓
       stop       inspect
                    ↓
              requires attention?
                /         \
              no           yes
              ↓             ↓
             stop      local notification

Possible Mac notification:

    Heidrick requires attention

    Patrick asked you for an update on the deployment issue.

## Efficiency

Do not continuously stream PiKVM screenshots to an LLM.

Use:

- scheduled checks
- local screen comparison
- changed-state detection
- targeted model calls

The AI should think only when useful.

---

# 8. Phase IV — Higher-Level Engineering Agent

After the previous layers are proven, the system can begin taking broader
objectives.

---

# Milestone 17 — Work Session Orchestrator

Example request:

> Start my Heidrick work session.

Possible workflow:

    ensure Slack availability
        ↓
    inspect new Slack
        ↓
    inspect task queue
        ↓
    inspect current branch
        ↓
    inspect PR feedback
        ↓
    inspect CI
        ↓
    produce prioritized plan

Output:

    WORK SESSION PLAN

    1. Respond to Patrick about ticket 743.
    2. Address two PR comments.
    3. Run the enrichment regression tests.
    4. Validate retry publisher.
    5. Prepare standup update.

The user can then say:

> Start task 2.

---

# Milestone 18 — Semi-Autonomous Routine Work

## Goal

Allow the system to complete well-understood low-risk engineering tasks
without requiring approval after every microscopic operation.

Examples:

- fix formatting/lint
- update tests for an obvious change
- apply straightforward review suggestions
- rerun known tests
- inspect failures
- clean debugging artifacts
- prepare commit contents

Still require approval for:

- committing
- pushing
- merging
- deployment
- external communication

The autonomy unit should be a controlled WORKFLOW, not unrestricted
computer control.

---

# Milestone 19 — Cross-Application Context Engine

## Goal

Allow the agent to understand that information across different
applications belongs to the same work item.

Example:

    Slack:
    "Patrick says #743 still fails"

             ↓

    Task correlation:
    ticket #743

             ↓

    Browser:
    open ticket #743

             ↓

    VS Code:
    identify related code

             ↓

    Terminal:
    run related tests

             ↓

    CI:
    inspect latest pipeline

             ↓

    Result:

    "Patrick's Slack message refers to ticket 743. Your current branch
    contains the related changes, but the regression test is still failing."

This is one of the highest-level capabilities of the entire system.

---

# Milestone 20 — Personal Engineering Copilot

## Long-Term Goal

The interaction moves from low-level commands:

    click this
    open Slack
    run pytest

to high-level intent:

> Check everything that needs me.

> Handle the routine items and surface decisions.

> Review today's PR feedback.

> Work through my low-risk review comments.

> Run the relevant tests and fix straightforward failures.

> Prepare my standup.

> Tell me which work machine actually needs my attention.

The Work Agent becomes an orchestration layer across otherwise isolated
remote machines.

---

# 9. Suggested Priority Order

Not every milestone has equal value.

Recommended sequence after Milestone 5:

    M6   Slack triage
          ↓
    M7   Slack drafting
          ↓
    M9   Daily work brief
          ↓
    M10  Development inspection
          ↓
    M11  Controlled terminal
          ↓
    M15  Standup generation
          ↓
    M12  PR review workflows
          ↓
    M14  Local task queue
          ↓
    M16  Proactive monitoring
          ↓
    M13  Ticket → implementation
          ↓
    M17+ Higher autonomy

Why this order:

The highest early return comes from reducing:

- context switching
- Slack checking
- finding what needs attention
- repetitive inspection
- test execution
- status reporting

before attempting full autonomous coding.

---

# 10. Autonomy Levels

Every skill should eventually declare an autonomy level.

## Level 0 — Observe

Examples:

- screenshots
- screen analysis
- Slack reading
- Git inspection
- CI inspection

No changes.

## Level 1 — Safe navigation

Examples:

- open Slack
- open browser
- navigate menus
- open ticket
- open terminal

## Level 2 — Local/reversible action

Examples:

- edit code
- run formatter
- run tests
- prepare draft
- prepare commit

May run automatically when skill policy permits.

## Level 3 — External/consequential

Examples:

- send Slack message
- create Git commit
- push
- submit forms
- approve review

Normally requires approval.

## Level 4 — High-risk

Examples:

- merge
- production changes
- deployments
- deleting data
- force pushes

Require explicit, immediate approval and additional verification.

Some may remain permanently unavailable to autonomous mode.

---

# 11. Multi-KVM Orchestration

Eventually every high-level skill should understand:

    one KVM

or:

    multiple KVMs

Example:

> Check all work environments.

Controller:

    Heidrick
        ↓
    Slack triage
        ↓
    task extraction

    Natrium
        ↓
    Slack triage
        ↓
    PR inspection

    Other KVM
        ↓
    no urgent activity

        ↓

    unified local summary

KVM operations should generally remain sequential unless there is a clear
technical reason for parallelism.

---

# 12. Local Memory

The agent should gradually gain LOCAL persistent memory.

Store useful operational facts such as:

- task state
- last successful checks
- previous notifications
- KVM application preferences
- known Slack channels
- known project names
- repository working conventions
- successful navigation patterns
- repeated failures
- user approvals/preferences

Do not persist:

- unnecessary screenshots
- Slack message bodies indefinitely
- secrets
- TOTP
- passwords
- private screen dumps

Memory should be useful, bounded, inspectable, and deletable.

---

# 13. Cost Optimization

The system should avoid turning an 8-hour workday into an 8-hour AI
stream.

Preferred model:

    event
      ↓
    local processing
      ↓
    does AI reasoning add value?
        /     \
       no     yes
       ↓       ↓
      $0     API call

Techniques:

- screenshot-change detection
- screen settling
- deterministic keyboard operations
- bounded context
- low-cost vision models
- stronger-model escalation only when needed
- local scheduling
- local task state
- no repeated analysis of unchanged screens

The goal is:

AI calls when something requires understanding.

Not:

AI continuously watches a static monitor.

---

# 14. Reliability Metrics

As the project matures, track more than whether commands "worked."

Useful metrics:

## GUI reliability

- action success rate
- verification failure rate
- stale-action cancellation rate
- unexpected-dialog frequency
- stuck-loop frequency

## Vision

- application identification accuracy
- target localization accuracy
- false-positive element rate
- confidence calibration

## Workflows

- Slack triage precision
- missed actionable messages
- duplicate task creation
- draft acceptance rate
- workflow completion rate

## Performance

- average model latency
- average task duration
- model calls per workflow
- tokens per workflow
- reauthentication count

## Safety

- approval requests
- denied actions
- ambiguous HID failures
- unsafe retry count (target: zero)

---

# 15. Evaluation Strategy

Do not rely entirely on subjective testing.

Maintain private/local evaluation scenarios.

Examples:

    Slack main window
    Slack DM
    Slack mention
    Slack profile menu
    ticket page
    VS Code
    terminal
    test failure
    CI success
    CI failure
    unexpected dialog

For each skill, maintain cases for:

- normal success
- already-complete state
- partial state
- unexpected state
- loading state
- low confidence
- network failure

Real work screenshots should remain local/ignored where appropriate.

---

# 16. Skill Architecture

Future features should generally become reusable SKILLS rather than
adding special behavior directly to AgentController.

Conceptually:

    skills/
        slack/
            availability
            triage
            draft
            send

        git/
            status
            diff
            branch
            commit

        tests/
            pytest
            lint
            typecheck

        browser/
            ticket
            pr
            ci

        work/
            daily_brief
            standup
            task_queue
            monitoring

AgentController remains generic.

Skills define:

- objective
- application-specific state
- success conditions
- allowed actions
- approval requirements
- recovery behavior

---

# 17. Things Explicitly NOT to Build Too Early

Avoid premature work on:

- unrestricted autonomous computer use
- arbitrary AI shell execution
- continuous mouse activity
- fake-presence simulation
- automated production deployment
- autonomous PR merging
- permanent screenshot recording
- giant multi-agent frameworks
- unnecessary vector databases
- complicated distributed infrastructure

The current single-Mac architecture is a feature, not a limitation.

Keep the system understandable.

---

# 18. Ultimate User Experience

The final product should feel less like a remote-control script and more
like a personal work operating layer.

Morning/evening interaction might eventually be:

    User:
    "What's going on?"

    Agent:
    "Two Heidrick items need attention.
     Patrick wants an update on #743 and there are two PR review comments.
     Natrium has no urgent activity.
     The latest Heidrick CI run passed."

Then:

    User:
    "Handle the review comments."

Agent:

    reads comments
    ↓
    examines code
    ↓
    makes focused changes
    ↓
    runs tests
    ↓
    reviews diff
    ↓

    "Both comments are addressed.
     18 tests passed.
     No commit or push performed."

Then:

    User:
    "Draft Patrick's update."

Agent prepares it.

Then:

    User:
    "Send it."

Policy approval occurs, PiKVM performs the operation, and the result is
verified.

At that point, PiKVM is no longer merely remote-access hardware.

It becomes the hardware interface through which a Mac-local personal
engineering agent can safely operate otherwise isolated work
environments.