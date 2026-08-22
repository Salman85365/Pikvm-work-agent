from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from work_agent.agenda.agenda import (
    AGENDA_PROMPT,
    DISMISS_OBJECTIVE,
    FOREGROUND_OBJECTIVE,
    SCROLL_OBJECTIVE,
    build_items,
    classify,
    parse_clock,
)
from work_agent.agenda.analyzer import AgendaScreenAnalyzer, blocked_only_by_a_dialog
from work_agent.agenda.cli import execute_agenda_command, format_agenda_batch
from work_agent.agenda.errors import AgendaError
from work_agent.agenda.models import (
    AgendaBatchResult,
    AgendaPerception,
    AgendaReport,
    CalendarSurface,
    MeetingStatus,
    VisibleMeeting,
)
from work_agent.agenda.operator import AgendaOperator
from work_agent.agenda.policy import AgendaPolicyEngine
from work_agent.agenda.service import AgendaService, JsonlAgendaLogger
from work_agent.agent.lock import ControllerLock
from work_agent.agent.models import (
    ActionProposal,
    AgentFinalStatus,
    ClickElementAction,
    PolicyDecisionKind,
    PressKeyAction,
    RiskCategory,
    ScrollAction,
    ScrollDirection,
    StopCode,
    TextPurpose,
    TypeTextAction,
)
from work_agent.agent.policy import PolicyEngine
from work_agent.vision import (
    AnalysisUsage,
    ImageDetail,
    NormalizedPoint,
    ObservationContext,
    ReasoningEffort,
    SafetyWarning,
    ScreenAnalysis,
    ScreenObservation,
    ScreenState,
    ServiceTier,
    UIElement,
    UIElementRole,
)


def _meeting(
    title: str,
    *,
    start: str | None = "9:00 AM",
    end: str | None = "9:30 AM",
    all_day: bool = False,
    location: str | None = None,
    organizer: str | None = None,
    online: bool = False,
    declined: bool = False,
) -> VisibleMeeting:
    return VisibleMeeting(
        title=title,
        start_text=start,
        end_text=end,
        all_day=all_day,
        location=location,
        organizer=organizer,
        is_online=online,
        declined=declined,
    )


def _perception(
    meetings: list[VisibleMeeting],
    *,
    visible: bool = True,
    today: bool = True,
    safe: bool = True,
    now: str | None = "10:00 AM",
    later_truncated: bool = False,
    earlier_truncated: bool = False,
    obstructed: bool = False,
) -> AgendaPerception:
    return AgendaPerception(
        calendar_visible=visible,
        surface=CalendarSurface.TEAMS,
        showing_today=today,
        date_text="Tuesday, 12 August",
        current_time_text=now,
        meetings=meetings,
        later_truncated=later_truncated,
        earlier_truncated=earlier_truncated,
        obstructed=obstructed,
        obstruction="a notification toast" if obstructed else None,
        summary="Teams calendar day view is visible.",
        safe_to_read=safe,
        stop_reason=None if safe else "The remote feed is blank.",
        confidence=0.93,
    )


def _screen(
    elements: list[UIElement],
    *,
    application: str = "Finder",
    screen_state: ScreenState = ScreenState.APPLICATION,
    warnings: list[SafetyWarning] | None = None,
    safe: bool = True,
    stop_reason: str | None = None,
    confidence: float = 0.96,
) -> ScreenAnalysis:
    return ScreenAnalysis(
        objective=FOREGROUND_OBJECTIVE,
        application=application,
        screen_state=screen_state,
        summary="Desktop with a dock.",
        target_found=False,
        target=None,
        relevant_elements=elements,
        warnings=warnings or [],
        safe_to_continue=safe,
        stop_reason=stop_reason,
        confidence=confidence,
        screenshot_width=1920,
        screenshot_height=1080,
        requested_model="vision",
        model="vision",
        requested_service_tier=ServiceTier.DEFAULT,
        service_tier="default",
        image_detail=ImageDetail.HIGH,
        reasoning_effort=ReasoningEffort.LOW,
        usage=AnalysisUsage(
            input_tokens=1,
            cached_input_tokens=0,
            cache_write_tokens=0,
            output_tokens=1,
            reasoning_tokens=0,
            total_tokens=2,
        ),
        latency_seconds=0.1,
        retries=0,
        escalated=False,
        attempted_models=["vision"],
    )


def _element(
    element_id: str,
    label: str,
    *,
    role: UIElementRole = UIElementRole.ICON,
    visible_text: str = "",
) -> UIElement:
    return UIElement(
        id=element_id,
        label=label,
        role=role,
        visible_text=visible_text,
        bounding_box=None,
        click_point={"x": 100, "y": 900},  # type: ignore[arg-type]
        confidence=0.95,
    )


def _proposal(action: object, *, risk: RiskCategory = RiskCategory.NAVIGATION) -> ActionProposal:
    return ActionProposal(
        action=action,  # type: ignore[arg-type]
        expected_outcome="The calendar comes into view.",
        confidence=0.97,
        risk=risk,
        reason_summary="Reach the open calendar.",
    )


# ---------------- clock reading ----------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("9:30 AM", 570),
        ("9:30am", 570),
        ("09:30", 570),
        ("12:00 PM", 720),
        ("12:00 AM", 0),
        ("12:45 AM", 45),
        ("14:00", 840),
        ("2 PM", 840),
        ("23:59", 1439),
        # macOS and Outlook on the web separate the meridiem with non-breaking spaces.
        ("9:30\u202fAM", 570),
        ("9:30\u00a0AM", 570),
        ("9.30 AM", None),
        ("25:00", None),
        ("9:75", None),
        ("13 PM", None),
        ("tomorrow", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_clock_reads_displayed_times_and_refuses_the_rest(
    text: str | None,
    expected: int | None,
) -> None:
    assert parse_clock(text) == expected


def test_classify_places_meetings_against_the_screens_own_clock() -> None:
    now = 600  # 10:00
    assert classify(540, 570, now, all_day=False) is MeetingStatus.ENDED
    assert classify(570, 630, now, all_day=False) is MeetingStatus.IN_PROGRESS
    assert classify(630, 660, now, all_day=False) is MeetingStatus.UPCOMING
    # An end exactly at the current minute is over.
    assert classify(540, 600, now, all_day=False) is MeetingStatus.ENDED
    assert classify(660, None, now, all_day=False) is MeetingStatus.UPCOMING
    assert classify(540, None, now, all_day=False) is MeetingStatus.IN_PROGRESS
    assert classify(None, None, now, all_day=True) is MeetingStatus.UPCOMING


def test_an_unreadable_clock_leaves_every_timed_meeting_unknown() -> None:
    """Without the remote clock, calling a meeting "over" would be a guess."""

    perception = _perception([_meeting("Standup", start="9:00 AM", end="9:15 AM")], now=None)
    items = build_items(perception)

    assert [item.status for item in items] == [MeetingStatus.UNKNOWN]


def test_build_items_orders_live_then_upcoming_then_ended() -> None:
    perception = _perception(
        [
            _meeting("Retro", start="4:00 PM", end="5:00 PM"),
            _meeting("Standup", start="9:00 AM", end="9:15 AM"),
            _meeting("Sprint review", start="9:45 AM", end="10:30 AM"),
            _meeting("Company all-hands", all_day=True, start=None, end=None),
            _meeting("Design sync", start="11:00 AM", end="11:30 AM"),
        ],
        now="10:00 AM",
    )

    items = build_items(perception)

    assert [item.title for item in items] == [
        "Sprint review",
        "Company all-hands",
        "Design sync",
        "Retro",
        "Standup",
    ]
    assert items[0].status is MeetingStatus.IN_PROGRESS
    assert items[-1].status is MeetingStatus.ENDED


def test_build_items_drops_blank_titles_and_keeps_declined_meetings() -> None:
    items = build_items(
        _perception([_meeting("   "), _meeting("Optional review", declined=True)]),
    )

    assert [item.title for item in items] == ["Optional review"]
    assert items[0].declined is True


# ---------------- perception schema and prompts ----------------


def test_perception_rejects_meetings_without_a_visible_calendar() -> None:
    with pytest.raises(ValueError, match="no calendar is visible"):
        _perception([_meeting("Standup")], visible=False)


def test_perception_requires_a_reason_when_not_readable() -> None:
    with pytest.raises(ValueError, match="must explain why"):
        AgendaPerception(
            calendar_visible=True,
            surface=CalendarSurface.BROWSER,
            showing_today=True,
            date_text=None,
            current_time_text=None,
            meetings=[],
            later_truncated=False,
            earlier_truncated=False,
            summary="Blank.",
            safe_to_read=False,
            stop_reason=None,
            confidence=0.2,
        )


def _flat(text: str) -> str:
    """Objectives are wrapped prose; match on wording, not on where lines happen to break."""

    return " ".join(text.casefold().split())


def test_objectives_forbid_joining_answering_and_opening_a_calendar() -> None:
    foreground = _flat(FOREGROUND_OBJECTIVE)
    assert "never click join, accept, decline, or tentative" in foreground
    assert "do not open a new tab or window" in foreground
    assert "do not sign in anywhere" in foreground
    assert "do not click anything" in _flat(SCROLL_OBJECTIVE)
    assert "never open, hover into, or expand a meeting" in _flat(AGENDA_PROMPT)


def test_the_prompt_reads_todays_column_out_of_a_week_view() -> None:
    """Teams opens on a week grid, so refusing week views would refuse the normal case."""

    prompt = _flat(AGENDA_PROMPT)
    assert "a week view is fine and is the common case" in prompt
    assert "read only today's column" in prompt
    assert "ignore every other day" in prompt
    assert "set showing_today false only when today cannot be read at all" in prompt


def test_the_objective_puts_teams_first_and_the_browser_second() -> None:
    foreground = _flat(FOREGROUND_OBJECTIVE)
    assert "try microsoft teams first" in foreground
    assert foreground.index("microsoft teams") < foreground.index("browser window")
    assert "only when teams cannot be reached at all" in foreground


def test_the_objectives_walk_around_a_blocking_dialog_rather_than_answering_it() -> None:
    foreground = _flat(FOREGROUND_OBJECTIVE)
    assert "leave it exactly as it is" in foreground
    assert "do not click it, do not dismiss it, do not press escape at it" in foreground
    assert "never click open software update, install, restart, later, or remind me" in foreground
    assert "puts the dialog behind it" in foreground
    # The overlay clearing pass must offer the same window-focus route before any key press.
    dismiss = _flat(DISMISS_OBJECTIVE)
    assert dismiss.index("do not touch it") < dismiss.index("press escape")
    assert "never click the overlay" in dismiss


# ---------------- policy ----------------


def test_policy_denies_joining_a_meeting() -> None:
    screen = _screen([_element("join", "Join", role=UIElementRole.BUTTON)], application="Teams")
    decision = AgendaPolicyEngine().evaluate(
        _proposal(ClickElementAction(type="click_element", element_id="join", button="left")),
        screen,
    )

    assert decision.decision is PolicyDecisionKind.DENY
    assert "joins a call" in decision.reason


@pytest.mark.parametrize("label", ["Accept", "Decline", "Tentative", "RSVP", "Propose new time"])
def test_policy_denies_answering_an_invitation(label: str) -> None:
    screen = _screen([_element("rsvp", label, role=UIElementRole.BUTTON)], application="Teams")
    decision = AgendaPolicyEngine().evaluate(
        _proposal(ClickElementAction(type="click_element", element_id="rsvp", button="left")),
        screen,
    )

    assert decision.decision is PolicyDecisionKind.DENY


def test_policy_allows_reaching_an_open_calendar() -> None:
    """The deny rule must not also block the click that reveals the calendar."""

    screen = _screen([_element("dock-teams", "Microsoft Teams 3")])
    decision = AgendaPolicyEngine().evaluate(
        _proposal(
            ClickElementAction(type="click_element", element_id="dock-teams", button="left"),
            risk=RiskCategory.UNKNOWN,
        ),
        screen,
    )

    assert decision.decision is PolicyDecisionKind.ALLOW
    assert decision.inferred_risk is RiskCategory.NAVIGATION


def test_policy_allows_the_calendar_rail_inside_teams() -> None:
    screen = _screen(
        [_element("rail-calendar", "Calendar", role=UIElementRole.BUTTON)],
        application="Microsoft Teams",
    )
    decision = AgendaPolicyEngine().evaluate(
        _proposal(
            ClickElementAction(type="click_element", element_id="rail-calendar", button="left"),
            risk=RiskCategory.UNKNOWN,
        ),
        screen,
    )

    assert decision.decision is PolicyDecisionKind.ALLOW


def test_policy_does_not_confuse_a_state_label_with_a_control() -> None:
    """A chip reading "Accepted" reports an existing RSVP, not the button that sends one."""

    screen = _screen(
        [_element("chip", "Accepted", role=UIElementRole.LIST_ITEM)],
        application="Microsoft Teams",
    )
    decision = AgendaPolicyEngine().evaluate(
        _proposal(
            ClickElementAction(type="click_element", element_id="chip", button="left"),
            risk=RiskCategory.UNKNOWN,
        ),
        screen,
    )

    assert decision.decision is not PolicyDecisionKind.DENY


def test_policy_allows_scrolling_the_day() -> None:
    decision = AgendaPolicyEngine().evaluate(
        _proposal(ScrollAction(type="scroll", direction=ScrollDirection.DOWN, amount=3)),
        _screen([], application="Microsoft Teams"),
    )

    assert decision.decision is PolicyDecisionKind.ALLOW


def test_policy_allows_escape_to_clear_an_overlay() -> None:
    decision = AgendaPolicyEngine().evaluate(
        _proposal(PressKeyAction(type="press_key", key="Escape")),
        _screen([], application="Microsoft Teams"),
    )

    assert decision.decision is PolicyDecisionKind.ALLOW


def test_policy_never_escapes_a_system_dialog() -> None:
    """The dialog is walked around, not answered - and Escape answers it."""

    decision = AgendaPolicyEngine().evaluate(
        _proposal(PressKeyAction(type="press_key", key="Escape")),
        _screen(
            [],
            application="Microsoft Teams",
            warnings=[SafetyWarning.UNEXPECTED_DIALOG],
        ),
    )

    assert decision.decision is PolicyDecisionKind.DENY
    assert "answer it" in decision.reason


def test_the_agenda_engine_owns_the_escape_denial(monkeypatch: pytest.MonkeyPatch) -> None:
    """The generic engine also refuses this today, but the calendar rules must not depend on it."""

    monkeypatch.setattr(PolicyEngine, "_dialog_rule", lambda self, action, screen: None)

    decision = AgendaPolicyEngine().evaluate(
        _proposal(PressKeyAction(type="press_key", key="Escape")),
        _screen([], application="Microsoft Teams", warnings=[SafetyWarning.UNEXPECTED_DIALOG]),
    )

    assert decision.decision is PolicyDecisionKind.DENY
    assert "Escape would answer it" in decision.reason


def test_policy_denies_editing_or_sending_text() -> None:
    engine = AgendaPolicyEngine()
    screen = _screen([], application="Microsoft Teams")
    for purpose in (TextPurpose.CONTENT_EDIT, TextPurpose.EXTERNAL_COMMUNICATION):
        decision = engine.evaluate(
            _proposal(TypeTextAction(type="type_text", text="hello", purpose=purpose)),
            screen,
        )
        assert decision.decision is PolicyDecisionKind.DENY, purpose


@pytest.mark.parametrize(
    "label",
    ["Open Software Update", "Install", "Restart", "Later", "Remind me", "Not now"],
)
def test_policy_never_answers_a_system_dialog(label: str) -> None:
    # By the time policy runs, AgendaScreenAnalyzer has already cleared the dialog warning so the
    # session can continue; refusing to click the dialog is then policy's job alone.
    screen = _screen([_element("dlg", label, role=UIElementRole.BUTTON)])
    decision = AgendaPolicyEngine().evaluate(
        _proposal(
            ClickElementAction(type="click_element", element_id="dlg", button="left"),
            risk=RiskCategory.UNKNOWN,
        ),
        screen,
    )

    assert decision.decision is PolicyDecisionKind.DENY
    assert "system dialog" in decision.reason


def test_policy_refuses_the_dialog_surface_itself() -> None:
    screen = _screen([_element("panel", "Software update reminder", role=UIElementRole.DIALOG)])
    decision = AgendaPolicyEngine().evaluate(
        _proposal(
            ClickElementAction(type="click_element", element_id="panel", button="left"),
            risk=RiskCategory.UNKNOWN,
        ),
        screen,
    )

    assert decision.decision is PolicyDecisionKind.DENY


def test_policy_still_allows_the_dock_icon_while_a_dialog_is_up() -> None:
    """Walking around the dialog is the whole plan; denying this would remove the feature."""

    screen = _screen([_element("dock-teams", "Microsoft Teams")])
    decision = AgendaPolicyEngine().evaluate(
        _proposal(
            ClickElementAction(type="click_element", element_id="dock-teams", button="left"),
            risk=RiskCategory.UNKNOWN,
        ),
        screen,
    )

    assert decision.decision is PolicyDecisionKind.ALLOW


# ---------------- blocking-dialog analyzer ----------------


class _StubAnalyzer:
    def __init__(self, analysis: ScreenAnalysis) -> None:
        self._analysis = analysis
        self.observations = 0

    def analyze(self, screenshot: bytes, **kwargs: object) -> ScreenAnalysis:
        return self._analysis

    def observe(self, screenshot: bytes, **kwargs: object) -> ScreenObservation:
        self.observations += 1
        return ScreenObservation(
            analysis=self._analysis,
            previous_action_verification=None,
            vision_calls=1,
        )


def _observe(analysis: ScreenAnalysis) -> ScreenAnalysis:
    events: list[str] = []
    wrapped = AgendaScreenAnalyzer(_StubAnalyzer(analysis), event_sink=events.append)  # type: ignore[arg-type]
    observation = wrapped.observe(
        b"jpeg",
        context=ObservationContext(objective=FOREGROUND_OBJECTIVE),
        width=1920,
        height=1080,
    )
    return observation.analysis


def _dialog_screen(
    *,
    elements: list[UIElement] | None = None,
    **overrides: object,
) -> ScreenAnalysis:
    defaults: dict[str, object] = {
        "warnings": [SafetyWarning.UNEXPECTED_DIALOG],
        "safe": False,
        "stop_reason": "An unexpected software-update dialog is blocking the screen.",
        "screen_state": ScreenState.DIALOG,
    }
    defaults.update(overrides)
    return _screen(elements or [], **defaults)  # type: ignore[arg-type]


def test_a_blocking_dialog_alone_no_longer_ends_the_session() -> None:
    analysis = _observe(_dialog_screen())

    assert analysis.safe_to_continue is True
    assert analysis.stop_reason is None
    # The warning stays: policy must still see the dialog to refuse answering it.
    assert SafetyWarning.UNEXPECTED_DIALOG in analysis.warnings


def test_the_analyzer_reports_warning_categories_by_name_only() -> None:
    seen: list[tuple[str, ...]] = []
    wrapped = AgendaScreenAnalyzer(
        _StubAnalyzer(_dialog_screen()),  # type: ignore[arg-type]
        warning_sink=seen.append,
    )
    wrapped.observe(
        b"jpeg",
        context=ObservationContext(objective=FOREGROUND_OBJECTIVE),
        width=1920,
        height=1080,
    )

    assert seen == [("unexpected_dialog",)]


@pytest.mark.parametrize(
    "warning",
    [
        SafetyWarning.AUTHENTICATION_PROMPT,
        SafetyWarning.LOCK_SCREEN,
        SafetyWarning.DESTRUCTIVE_CONFIRMATION,
        SafetyWarning.REMOTE_DISCONNECT,
        SafetyWarning.LOW_CONFIDENCE,
        SafetyWarning.UNKNOWN_STATE,
    ],
)
def test_a_dialog_never_excuses_a_second_problem(warning: SafetyWarning) -> None:
    """Only the dialog is forgiven. Anything a window focus cannot fix must still stop."""

    analysis = _observe(_dialog_screen(warnings=[SafetyWarning.UNEXPECTED_DIALOG, warning]))

    assert analysis.safe_to_continue is False
    assert analysis.stop_reason is not None


@pytest.mark.parametrize("state", [ScreenState.AUTHENTICATION, ScreenState.LOCK_SCREEN])
def test_a_login_or_lock_screen_is_never_treated_as_a_dismissible_dialog(
    state: ScreenState,
) -> None:
    analysis = _observe(_dialog_screen(screen_state=state))

    assert analysis.safe_to_continue is False


def test_an_unsafe_screen_without_a_dialog_is_left_alone() -> None:
    analysis = _observe(
        _screen([], warnings=[SafetyWarning.UNKNOWN_STATE], safe=False, stop_reason="Blank feed.")
    )

    assert analysis.safe_to_continue is False
    assert analysis.stop_reason == "Blank feed."


def test_the_dock_click_survives_the_analyzer_and_the_policy_together() -> None:
    """The whole point: a blocked screen still lets the calendar be brought to the front.

    The analyzer clears the dialog so the session continues, and the policy then allows the Dock
    icon while still refusing the dialog's own button on that same screen.
    """

    raw = _dialog_screen(
        warnings=[SafetyWarning.UNEXPECTED_DIALOG],
        elements=[
            _element("dock-teams", "Microsoft Teams"),
            _element("update", "Open Software Update", role=UIElementRole.BUTTON),
        ],
    )
    cleared = _observe(raw)
    engine = AgendaPolicyEngine()

    dock = engine.evaluate(
        _proposal(
            ClickElementAction(type="click_element", element_id="dock-teams", button="left"),
            risk=RiskCategory.UNKNOWN,
        ),
        cleared,
    )
    dialog = engine.evaluate(
        _proposal(
            ClickElementAction(type="click_element", element_id="update", button="left"),
            risk=RiskCategory.UNKNOWN,
        ),
        cleared,
    )

    assert dock.decision is PolicyDecisionKind.ALLOW
    assert dialog.decision is PolicyDecisionKind.DENY
    # Without the analyzer the session would never have got this far.
    assert (
        engine.evaluate(
            _proposal(
                ClickElementAction(type="click_element", element_id="dock-teams", button="left"),
                risk=RiskCategory.UNKNOWN,
            ),
            raw,
        ).decision
        is PolicyDecisionKind.DENY
    )


def test_a_healthy_screen_passes_through_untouched() -> None:
    healthy = _screen([_element("dock-teams", "Microsoft Teams")])

    assert _observe(healthy) == healthy
    assert blocked_only_by_a_dialog(healthy) is False


# ---------------- operator ----------------


class _TestControllerLock:
    def __init__(self) -> None:
        self.depth = 0
        self.wait_timeout: float | None = None

    @property
    def held(self) -> bool:
        return self.depth > 0

    def acquire(
        self,
        *,
        timeout_seconds: float = 0.0,
        poll_interval_seconds: float = 0.1,
        on_wait: object = None,
    ) -> None:
        self.wait_timeout = timeout_seconds
        self.depth += 1

    def release(self) -> None:
        self.depth -= 1


class _Operator(AgendaOperator):
    def __init__(self, perceptions: list[AgendaPerception], **kwargs: object) -> None:
        kwargs.setdefault("controller_lock_factory", lambda _kvm: _TestControllerLock())
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._perceptions = perceptions
        self.reads = 0

    def _read(self, kvm: str) -> AgendaPerception:
        index = min(self.reads, len(self._perceptions) - 1)
        self.reads += 1
        return self._perceptions[index]


def _ok_executor(args: object, **kwargs: object) -> object:
    return SimpleNamespace(status=AgentFinalStatus.SUCCESS, stop_code=StopCode.COMPLETED)


def test_operator_uses_the_agenda_policy_and_never_approves_interactively() -> None:
    received: dict[str, object] = {}

    def executor(args: object, **kwargs: object) -> object:
        received["args"] = args
        received.update(kwargs)
        return SimpleNamespace(status=AgentFinalStatus.SUCCESS, stop_code=StopCode.COMPLETED)

    report = _Operator(
        [_perception([_meeting("Sprint review", start="10:30 AM", end="11:00 AM")])],
        executor=executor,
    ).execute("work-kvm")

    assert report.success is True
    assert [item.title for item in report.upcoming] == ["Sprint review"]
    assert isinstance(received["policy_engine"], AgendaPolicyEngine)
    assert received["approval_provider"].__class__.__name__ == "NonInteractiveApprovalProvider"
    assert received["vision_detail"] is ImageDetail.HIGH
    transform = received["analyzer_transform"]
    assert isinstance(transform(_StubAnalyzer(_screen([])), None), AgendaScreenAnalyzer)  # type: ignore[operator]


def test_operator_holds_one_endpoint_lock_across_navigation_read_and_scroll() -> None:
    lock = _TestControllerLock()
    observed_while_locked: list[bool] = []

    def executor(args: object, **kwargs: object) -> object:
        assert kwargs["controller_lock"] is lock
        observed_while_locked.append(lock.held)
        return SimpleNamespace(status=AgentFinalStatus.SUCCESS, stop_code=StopCode.COMPLETED)

    first = _perception(
        [_meeting("Standup", start="9:00 AM", end="9:15 AM")],
        later_truncated=True,
    )
    second = _perception(
        [_meeting("Retro", start="4:00 PM", end="5:00 PM")],
        later_truncated=False,
    )
    operator = _Operator(
        [first, second],
        executor=executor,
        controller_lock_factory=lambda _kvm: lock,
        controller_lock_wait_seconds=0.25,
    )

    report = operator.execute("work-kvm")

    assert report.success is True
    assert observed_while_locked == [True, True]
    assert lock.wait_timeout == 0.25
    assert lock.held is False


def test_operator_explains_when_the_bounded_controller_wait_expires(tmp_path: Path) -> None:
    held_lock = ControllerLock(tmp_path / "controller.lock")
    waiting_lock = ControllerLock(tmp_path / "controller.lock")
    trace: list[str] = []
    held_lock.acquire()
    try:
        report = _Operator(
            [],
            executor=_ok_executor,
            trace_output=trace.append,
            controller_lock_factory=lambda _kvm: waiting_lock,
            controller_lock_wait_seconds=0.01,
        ).execute("work-kvm")
    finally:
        held_lock.release()

    assert report.success is False
    assert "waited for the other local workflow" in (report.error or "")
    assert "Let it finish" in (report.error or "")
    assert trace == [
        "work-kvm  | Another local workflow is finishing; this calendar read is queued."
    ]


def test_operator_says_the_screen_was_blocked_rather_than_blaming_a_missing_calendar() -> None:
    """The two failures look alike and mean opposite things."""

    def executor(args: object, **kwargs: object) -> object:
        return SimpleNamespace(
            status=AgentFinalStatus.PAUSED,
            stop_code=StopCode.SCREEN_UNSAFE,
            summary="An unexpected software-update dialog is blocking the screen.",
        )

    report = _Operator([], executor=executor).execute("work-kvm")

    assert report.success is False
    assert report.stop_code == "screen_unsafe"
    assert "could not be used safely" in (report.error or "")
    assert "no calendar was looked for" in (report.error or "")


def test_operator_still_reports_a_genuinely_missing_calendar_plainly() -> None:
    def executor(args: object, **kwargs: object) -> object:
        return SimpleNamespace(
            status=AgentFinalStatus.PAUSED,
            stop_code=StopCode.USER_ASSISTANCE_REQUESTED,
            summary="No Teams calendar or calendar tab is open.",
        )

    report = _Operator([_perception([], visible=False)], executor=executor).execute("work-kvm")

    assert "No calendar could be reached" in (report.error or "")


def test_operator_reports_the_stop_code_when_no_calendar_can_be_reached() -> None:
    def executor(args: object, **kwargs: object) -> object:
        return SimpleNamespace(
            status=AgentFinalStatus.FAILED,
            stop_code=StopCode.POLICY_DENIED,
            summary="Policy denied the proposed action.",
        )

    operator = _Operator([_perception([], visible=False)], executor=executor)
    report = operator.execute("work-kvm")

    assert report.success is False
    assert report.stop_code == "policy_denied"
    assert report.items == ()
    # The harmless read was still attempted before blaming the foreground phase.
    assert operator.reads == 1


@pytest.mark.parametrize(
    "stop_code",
    [
        StopCode.RUNTIME_LIMIT,
        StopCode.STEP_LIMIT,
        StopCode.APPROVAL_DENIED,
        StopCode.PLANNER_LOW_CONFIDENCE,
        StopCode.VERIFICATION_FAILED,
        StopCode.INTERNAL_ERROR,
    ],
)
def test_operator_still_reads_a_visible_calendar_after_a_failed_foreground(
    stop_code: StopCode,
) -> None:
    """A controller that ran out of time may have left a perfectly readable calendar in view."""

    def executor(args: object, **kwargs: object) -> object:
        return SimpleNamespace(status=AgentFinalStatus.FAILED, stop_code=stop_code, summary="x")

    operator = _Operator(
        [_perception([_meeting("Sprint review", start="10:30 AM", end="11:00 AM")])],
        executor=executor,
    )
    report = operator.execute("work-kvm")

    assert report.success is True
    assert operator.reads == 1
    assert [item.title for item in report.items] == ["Sprint review"]


@pytest.mark.parametrize(
    "stop_code",
    [
        StopCode.SCREEN_UNSAFE,
        StopCode.SCREEN_LOW_CONFIDENCE,
        StopCode.PIKVM_UNREACHABLE,
        StopCode.PIKVM_AUTH_FAILED,
    ],
)
def test_operator_does_not_read_when_the_screen_or_pikvm_was_the_problem(
    stop_code: StopCode,
) -> None:
    def executor(args: object, **kwargs: object) -> object:
        return SimpleNamespace(status=AgentFinalStatus.FAILED, stop_code=stop_code, summary="x")

    operator = _Operator([_perception([])], executor=executor)
    report = operator.execute("work-kvm")

    assert report.success is False
    assert report.stop_code == stop_code.value
    assert operator.reads == 0


@pytest.mark.parametrize(
    ("stop_code", "phrase"),
    [
        (StopCode.RUNTIME_LIMIT, "ran out of time"),
        (StopCode.PIKVM_UNREACHABLE, "The PiKVM could not be reached"),
        (StopCode.PIKVM_AUTH_FAILED, "rejected this profile's credentials"),
        (StopCode.MODEL_PROVIDER_ERROR, "OpenAI could not be used"),
        (StopCode.INTERNAL_ERROR, "A local error stopped"),
    ],
)
def test_operator_names_environment_failures_instead_of_blaming_the_calendar(
    stop_code: StopCode,
    phrase: str,
) -> None:
    def executor(args: object, **kwargs: object) -> object:
        return SimpleNamespace(
            status=AgentFinalStatus.FAILED,
            stop_code=stop_code,
            summary="details",
        )

    report = _Operator([_perception([], visible=False)], executor=executor).execute("work-kvm")

    assert report.success is False
    assert phrase in (report.error or "")
    assert "No open calendar could be brought into view" not in (report.error or "")


def test_operator_carries_warning_categories_into_a_failed_report() -> None:
    def executor(args: object, **kwargs: object) -> object:
        transform = kwargs["analyzer_transform"]
        wrapped = transform(  # type: ignore[operator]
            _StubAnalyzer(
                _screen([], warnings=[SafetyWarning.LOCK_SCREEN], safe=False, stop_reason="Locked.")
            ),
            None,
        )
        wrapped.observe(
            b"jpeg",
            context=ObservationContext(objective=FOREGROUND_OBJECTIVE),
            width=1920,
            height=1080,
        )
        return SimpleNamespace(
            status=AgentFinalStatus.PAUSED,
            stop_code=StopCode.SCREEN_UNSAFE,
            summary="Locked.",
        )

    report = _Operator([], executor=executor).execute("work-kvm")

    assert report.warnings == ("lock_screen",)


def test_operator_refuses_a_calendar_that_is_not_showing_today() -> None:
    report = _Operator(
        [_perception([_meeting("Tomorrow standup")], today=False)],
        executor=_ok_executor,
    ).execute("work-kvm")

    assert report.success is False
    assert "not on today" in (report.error or "")
    assert "Tuesday, 12 August" in (report.error or "")


def test_operator_refuses_when_no_calendar_is_visible() -> None:
    report = _Operator([_perception([], visible=False)], executor=_ok_executor).execute("work-kvm")

    assert report.success is False
    assert "never opens one itself" in (report.error or "")


def test_operator_stops_on_an_untrusted_screen() -> None:
    report = _Operator([_perception([], safe=False)], executor=_ok_executor).execute("work-kvm")

    assert report.success is False
    assert report.error == "The remote feed is blank."


def test_operator_dismisses_an_overlay_before_trusting_the_read() -> None:
    covered = _perception([], obstructed=True)
    clear_view = _perception([_meeting("Design sync", start="11:00 AM", end="11:30 AM")])
    operator = _Operator([covered, clear_view], executor=_ok_executor)

    report = operator.execute("work-kvm")

    assert operator.reads == 2
    assert report.success is True
    assert [item.title for item in report.upcoming] == ["Design sync"]


def test_operator_never_reports_an_empty_day_it_could_not_see() -> None:
    operator = _Operator([_perception([], obstructed=True)], executor=_ok_executor)

    report = operator.execute("work-kvm")

    assert report.success is False
    assert report.obstructed is True
    assert "empty day cannot be trusted" in (report.error or "")


def test_operator_scrolls_a_clipped_day_and_merges_what_it_finds() -> None:
    first = _perception(
        [_meeting("Standup", start="9:00 AM", end="9:15 AM")],
        later_truncated=True,
    )
    second = _perception(
        [
            _meeting("Standup", start="9:00 AM", end="9:15 AM"),
            _meeting("Retro", start="4:00 PM", end="5:00 PM"),
        ],
        later_truncated=False,
    )
    operator = _Operator([first, second], executor=_ok_executor)

    report = operator.execute("work-kvm")

    assert report.scrolled is True
    assert operator.reads == 2
    assert [item.title for item in report.items] == ["Retro", "Standup"]
    assert report.later_truncated is False


def test_operator_stops_scrolling_when_nothing_new_appears() -> None:
    stuck = _perception([_meeting("Standup", start="9:00 AM", end="9:15 AM")], later_truncated=True)
    operator = _Operator([stuck], executor=_ok_executor)

    report = operator.execute("work-kvm")

    # One scroll, one re-read, then it gives up rather than looping to the bound.
    assert operator.reads == 2
    assert report.success is True
    assert report.later_truncated is True


def test_operator_keeps_the_first_reads_view_of_what_sat_above() -> None:
    """Scrolling down hides the top of the day; the earlier warning must survive it."""

    first = _perception([], later_truncated=True, earlier_truncated=True)
    second = _perception(
        [_meeting("Retro", start="4:00 PM", end="5:00 PM")],
        later_truncated=False,
        earlier_truncated=False,
    )
    report = _Operator([first, second], executor=_ok_executor).execute("work-kvm")

    assert report.earlier_truncated is True


def test_operator_splits_the_day_around_the_remote_clock() -> None:
    report = _Operator(
        [
            _perception(
                [
                    _meeting("Standup", start="9:00 AM", end="9:15 AM"),
                    _meeting("Sprint review", start="10:30 AM", end="11:00 AM"),
                ],
                now="10:00 AM",
            )
        ],
        executor=_ok_executor,
    ).execute("work-kvm")

    assert [item.title for item in report.upcoming] == ["Sprint review"]
    assert [item.title for item in report.earlier] == ["Standup"]
    assert report.clock_read is True
    assert report.current_time_text == "10:00 AM"


# ---------------- service, logging, CLI ----------------


class _FakeOperator:
    def __init__(self, reports: dict[str, AgendaReport]) -> None:
        self._reports = reports
        self.calls: list[str] = []

    def execute(self, kvm: str) -> AgendaReport:
        self.calls.append(kvm)
        return self._reports[kvm]


def _report(kvm: str, *, success: bool = True) -> AgendaReport:
    perception = _perception(
        [
            _meeting("Standup", start="9:00 AM", end="9:15 AM"),
            _meeting("Sprint review", start="10:30 AM", end="11:00 AM", location="Room 4"),
        ]
    )
    return AgendaReport(
        kvm=kvm,
        success=success,
        surface=CalendarSurface.TEAMS,
        date_text="Tuesday, 12 August",
        current_time_text="10:00 AM",
        items=build_items(perception) if success else (),
        confidence=0.93,
        error=None if success else "No calendar was visible.",
    )


def test_the_log_records_counts_and_never_a_meeting_title(tmp_path: Path) -> None:
    path = tmp_path / "calendar-agenda.jsonl"
    JsonlAgendaLogger(path).record(_report("work-kvm"))

    raw = path.read_text(encoding="utf-8")
    entry = json.loads(raw)

    assert entry["meetings"] == 2
    assert entry["upcoming"] == 1
    assert entry["clock_read"] is True
    assert "Sprint review" not in raw
    assert "Standup" not in raw
    assert "Room 4" not in raw
    assert path.stat().st_mode & 0o777 == 0o600


def test_the_log_records_the_stop_code_and_warning_names_of_a_failure(tmp_path: Path) -> None:
    path = tmp_path / "calendar-agenda.jsonl"
    JsonlAgendaLogger(path).record(
        AgendaReport(
            kvm="work-kvm",
            success=False,
            error="The remote screen could not be used safely.",
            stop_code="screen_unsafe",
            warnings=("unexpected_dialog", "lock_screen"),
        )
    )

    entry = json.loads(path.read_text(encoding="utf-8"))

    assert entry["stop_code"] == "screen_unsafe"
    assert entry["warnings"] == ["unexpected_dialog", "lock_screen"]


def test_the_service_records_every_report_and_survives_an_operator_crash() -> None:
    class _Exploding:
        def execute(self, kvm: str) -> AgendaReport:
            raise RuntimeError("boom")

    recorded: list[AgendaReport] = []

    class _Logger:
        def record(self, report: AgendaReport) -> None:
            recorded.append(report)

    batch = AgendaService(_Exploding(), _Logger()).run(("work-kvm",))

    assert batch.success is False
    assert (
        batch.reports[0].error
        == "An unexpected local error stopped this calendar read (RuntimeError)."
    )
    assert len(recorded) == 1


def test_a_logging_failure_is_surfaced_without_losing_the_reading() -> None:
    class _Logger:
        def record(self, report: AgendaReport) -> None:
            raise AgendaError("The local calendar log could not be written.")

    batch = AgendaService(_FakeOperator({"work-kvm": _report("work-kvm")}), _Logger()).run(
        ("work-kvm",)
    )

    assert batch.reports[0].success is True
    assert batch.reports[0].log_error == "The local calendar log could not be written."


def test_the_cli_runs_every_configured_profile_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "work_agent.agenda.cli.configured_pikvm_profiles",
        lambda: ("heidrick", "nbc_kvm"),
    )
    operator = _FakeOperator({name: _report(name) for name in ("heidrick", "nbc_kvm")})

    class _Logger:
        def record(self, report: AgendaReport) -> None:
            return None

    result = execute_agenda_command(
        argparse.Namespace(calendar_command="today", kvm=None, all_kvms=True, trace=False),
        service=AgendaService(operator, _Logger()),
    )

    assert operator.calls == ["heidrick", "nbc_kvm"]
    assert result.success is True


def test_the_cli_rejects_an_unknown_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("work_agent.agenda.cli.configured_pikvm_profiles", lambda: ("heidrick",))

    with pytest.raises(AgendaError, match="Unknown PiKVM profile"):
        execute_agenda_command(
            argparse.Namespace(calendar_command="today", kvm="nope", all_kvms=False, trace=False),
        )


def test_the_formatter_leads_with_what_is_still_ahead() -> None:
    text = format_agenda_batch(AgendaBatchResult(reports=(_report("work-kvm"),)))

    assert "1 still ahead" in text
    assert "clock 10:00 AM" in text
    assert "Sprint review" in text
    assert "Room 4" in text
    assert "Earlier today: 1" in text
    assert text.index("Sprint review") < text.index("Earlier today")


def test_the_formatter_warns_when_the_remote_clock_could_not_be_read() -> None:
    report = AgendaReport(
        kvm="work-kvm",
        success=True,
        items=build_items(_perception([_meeting("Standup")], now=None)),
        confidence=0.8,
    )

    text = format_agenda_batch(AgendaBatchResult(reports=(report,)))

    assert "the remote clock could not be read" in text


def test_scroll_phase_policy_only_permits_scrolling() -> None:
    from work_agent.agenda.policy import AgendaScrollPolicyEngine
    from work_agent.agent.models import (
        ActionProposal,
        ClickElementAction,
        FinishAction,
        PolicyDecisionKind,
        RiskCategory,
        ScrollAction,
    )

    engine = AgendaScrollPolicyEngine()
    dock = UIElement(
        id="dock-outlook",
        label="Outlook",
        role=UIElementRole.ICON,
        visible_text="",
        bounding_box=None,
        click_point=NormalizedPoint(x=326, y=956),
        confidence=0.98,
    )
    screen = _screen([dock], application="Microsoft Teams")

    def proposal(action: object) -> ActionProposal:
        return ActionProposal.model_validate(
            {
                "action": action,
                "expected_outcome": "The calendar scrolls.",
                "confidence": 0.95,
                "risk": RiskCategory.NAVIGATION,
                "reason_summary": "Scroll.",
            }
        )

    scroll = ScrollAction(type="scroll", direction="down", amount=2)
    click = ClickElementAction(type="click_element", element_id="dock-outlook", button="left")
    finish = FinishAction(type="finish", summary="Done.")
    assert engine.evaluate(proposal(scroll), screen).decision is PolicyDecisionKind.ALLOW
    assert engine.evaluate(proposal(click), screen).decision is PolicyDecisionKind.DENY
    assert engine.evaluate(proposal(finish), screen).decision is PolicyDecisionKind.ALLOW
