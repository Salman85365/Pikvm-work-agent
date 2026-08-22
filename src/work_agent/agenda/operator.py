from __future__ import annotations

import argparse
from collections.abc import Callable

from work_agent.agenda.agenda import (
    AGENDA_CONTEXT,
    AGENDA_PROMPT,
    DISMISS_OBJECTIVE,
    FOREGROUND_OBJECTIVE,
    SCROLL_OBJECTIVE,
    build_items,
    meeting_key,
)
from work_agent.agenda.analyzer import AgendaScreenAnalyzer
from work_agent.agenda.models import AgendaPerception, AgendaReport, VisibleMeeting
from work_agent.agenda.policy import AgendaPolicyEngine, AgendaScrollPolicyEngine
from work_agent.agent.approval import NonInteractiveApprovalProvider
from work_agent.agent.cli import execute_agent_command
from work_agent.agent.errors import AgentError, ControllerLockError
from work_agent.agent.lock import ControllerLock
from work_agent.agent.models import (
    AgentFinalStatus,
    AgentSessionResult,
    ApprovalMode,
    StopCode,
)
from work_agent.agent.pikvm_session import PiKVMSession
from work_agent.pikvm import PiKVMError, PiKVMSettings, build_totp_provider
from work_agent.vision import (
    AnalysisOptions,
    ImageDetail,
    OpenAIScreenAnalyzer,
    VisionError,
    VisionSettings,
)

# A day view rarely hides more than a couple of screens of meetings, and every extra round is
# another OpenAI read. Bounded scrolling also keeps a mis-scrolled view from wandering.
_MAX_SCROLL_ROUNDS = 3
# A scheduled controller normally has a 180-second runtime bound. Queue a foreground calendar
# read long enough to take the endpoint as soon as that controller releases it, rather than making
# the user race a launchd run. The lock remains bounded so a genuinely wedged process is surfaced.
_CONTROLLER_LOCK_WAIT_SECONDS = 185.0


# Foreground outcomes after which the harmless read is not even attempted: the screen itself
# was the problem, or the PiKVM was, and a fresh screenshot would meet the same wall.
_NO_READ_AFTER = frozenset(
    {
        StopCode.SCREEN_UNSAFE,
        StopCode.SCREEN_LOW_CONFIDENCE,
        StopCode.PIKVM_UNREACHABLE,
        StopCode.PIKVM_AUTH_FAILED,
        StopCode.INTERRUPTED,
    }
)

_FOREGROUND_FAILURE_PHRASES: dict[StopCode, str] = {
    StopCode.SCREEN_UNSAFE: (
        "The remote screen could not be used safely, so no calendar was looked for"
    ),
    StopCode.SCREEN_LOW_CONFIDENCE: (
        "The remote screen could not be read confidently, so no calendar was looked for"
    ),
    StopCode.USER_ASSISTANCE_REQUESTED: "No calendar could be reached",
    StopCode.RUNTIME_LIMIT: "The controller ran out of time before a calendar could be confirmed",
    StopCode.STEP_LIMIT: "The controller ran out of steps before a calendar could be confirmed",
    StopCode.PIKVM_UNREACHABLE: "The PiKVM could not be reached",
    StopCode.PIKVM_AUTH_FAILED: "The PiKVM rejected this profile's credentials",
    StopCode.MODEL_PROVIDER_ERROR: "OpenAI could not be used",
    StopCode.MODEL_OUTPUT_INVALID: "OpenAI returned an unusable answer",
    StopCode.INTERNAL_ERROR: "A local error stopped the calendar controller",
    StopCode.INTERRUPTED: "The calendar controller was interrupted",
}


def _foreground_failure(session: AgentSessionResult) -> str:
    """Say which of the very different failures happened.

    "No calendar could be found" and "the screen could not be worked with" read the same to a
    person but mean opposite things, and reporting the first when the second happened sends
    someone looking for a calendar that was open the whole time. A time limit, an unreachable
    PiKVM, or a provider outage says nothing about the calendar either, so each is named as what
    it is.
    """

    phrase = _FOREGROUND_FAILURE_PHRASES.get(
        session.stop_code, "No open calendar could be brought into view"
    )
    return f"{phrase}: {session.summary}"


def _reads_after_foreground(session: AgentSessionResult) -> bool:
    """A failed walk to the calendar does not prove no calendar is showing.

    A time or step limit, a denied approval, a low-confidence plan, or a failed verification can
    all end the controller while a calendar sits in plain view. The read that follows sends no
    HID at all, so it is attempted and the perception decides; only when it finds no calendar
    surface is the foreground failure reported.
    """

    return session.stop_code not in _NO_READ_AFTER


class AgendaOperator:
    """Bring an open calendar into view, then read today's meetings without opening any of them.

    Navigation and reading stay separate phases, as in Slack triage: every read runs on a plain
    screenshot with no executor attached, so it is structurally incapable of sending HID.
    """

    def __init__(
        self,
        *,
        executor: Callable[..., AgentSessionResult] = execute_agent_command,
        trace_output: Callable[[str], None] | None = None,
        controller_lock_factory: Callable[[str], ControllerLock] | None = None,
        controller_lock_wait_seconds: float = _CONTROLLER_LOCK_WAIT_SECONDS,
    ) -> None:
        if controller_lock_wait_seconds < 0:
            raise ValueError("Agenda controller lock timeout cannot be negative.")
        self._executor = executor
        self._trace_output = trace_output or (lambda _: None)
        self._controller_lock_factory = controller_lock_factory or self._controller_lock_for_profile
        self._controller_lock_wait_seconds = controller_lock_wait_seconds
        self._last_warnings: tuple[str, ...] = ()

    def execute(self, kvm: str) -> AgendaReport:
        self._last_warnings = ()
        try:
            controller_lock = self._controller_lock_factory(kvm)
            controller_lock.acquire(
                timeout_seconds=self._controller_lock_wait_seconds,
                on_wait=lambda: self._trace_output(
                    f"{kvm}  | Another local workflow is finishing; this calendar read is queued."
                ),
            )
        except ControllerLockError:
            return AgendaReport(
                kvm=kvm,
                success=False,
                error=(
                    "This calendar read waited for the other local workflow, but that workflow "
                    "was still using this PiKVM. Let it finish, then try the calendar read again."
                ),
            )
        except (AgentError, PiKVMError, VisionError) as exc:
            return AgendaReport(kvm=kvm, success=False, error=str(exc))
        except (OSError, ValueError):
            return AgendaReport(
                kvm=kvm,
                success=False,
                error="The local calendar controller could not be started.",
            )

        try:
            return self._execute_locked(kvm, controller_lock)
        finally:
            controller_lock.release()

    def _execute_locked(self, kvm: str, controller_lock: ControllerLock) -> AgendaReport:
        try:
            session = self._foreground(kvm, controller_lock)
        except (AgentError, PiKVMError, VisionError) as exc:
            return AgendaReport(kvm=kvm, success=False, error=str(exc))
        except (OSError, ValueError):
            return AgendaReport(
                kvm=kvm,
                success=False,
                error="The local calendar controller could not be started.",
            )

        foreground_failed = session.status is not AgentFinalStatus.SUCCESS
        if foreground_failed and not _reads_after_foreground(session):
            return self._foreground_report(kvm, session)

        try:
            perception = self._read(kvm)
            if foreground_failed and not perception.calendar_visible:
                return self._foreground_report(kvm, session)
            if perception.obstructed:
                self._trace_output(
                    f"{kvm}  | The calendar is covered"
                    f"{f' by {perception.obstruction}' if perception.obstruction else ''}; "
                    "dismissing with Escape before trusting the read."
                )
                dismissal = self._dismiss(kvm, controller_lock)
                if dismissal.status is AgentFinalStatus.SUCCESS:
                    perception = self._read(kvm)
                else:
                    self._trace_output(
                        f"{kvm}  | Overlay could not be dismissed: {dismissal.summary}"
                    )

            refusal = self._refusal(kvm, perception)
            if refusal is not None:
                return refusal

            earlier_truncated = perception.earlier_truncated
            merged: dict[tuple[str, str, str], VisibleMeeting] = {
                meeting_key(meeting): meeting for meeting in perception.meetings
            }
            scrolled = False
            for _ in range(_MAX_SCROLL_ROUNDS):
                if not perception.later_truncated:
                    break
                self._trace_output(
                    f"{kvm}  | The day is clipped; scrolling once to look for later meetings."
                )
                scroll = self._scroll(kvm, controller_lock)
                if scroll.status is not AgentFinalStatus.SUCCESS:
                    self._trace_output(f"{kvm}  | Scrolling stopped: {scroll.summary}")
                    break
                scrolled = True
                perception = self._read(kvm)
                refusal = self._refusal(kvm, perception)
                if refusal is not None:
                    return refusal
                added = [
                    meeting for meeting in perception.meetings if meeting_key(meeting) not in merged
                ]
                if not added:
                    break
                for meeting in added:
                    merged[meeting_key(meeting)] = meeting
        except (PiKVMError, VisionError) as exc:
            return AgendaReport(kvm=kvm, success=False, error=str(exc))
        except (OSError, ValueError):
            return AgendaReport(
                kvm=kvm,
                success=False,
                error="Today's calendar could not be read locally.",
            )

        complete = perception.model_copy(update={"meetings": list(merged.values())})
        items = build_items(complete)
        if perception.obstructed and not items:
            # Never let a blocked read masquerade as a free afternoon.
            return AgendaReport(
                kvm=kvm,
                success=False,
                surface=perception.surface,
                obstructed=True,
                confidence=perception.confidence,
                error=(
                    "Something is still covering the calendar"
                    f"{f' ({perception.obstruction})' if perception.obstruction else ''}, so an "
                    "empty day cannot be trusted. Nothing was reported."
                ),
            )

        return AgendaReport(
            kvm=kvm,
            success=True,
            surface=perception.surface,
            date_text=perception.date_text,
            current_time_text=perception.current_time_text,
            items=items,
            later_truncated=perception.later_truncated,
            earlier_truncated=earlier_truncated,
            obstructed=perception.obstructed,
            scrolled=scrolled,
            confidence=perception.confidence,
        )

    def _foreground_report(self, kvm: str, session: AgentSessionResult) -> AgendaReport:
        return AgendaReport(
            kvm=kvm,
            success=False,
            error=_foreground_failure(session),
            stop_code=session.stop_code.value,
            warnings=self._last_warnings,
        )

    def _refusal(self, kvm: str, perception: AgendaPerception) -> AgendaReport | None:
        """Reject a read that cannot honestly describe today, rather than reporting it."""

        if not perception.safe_to_read:
            return AgendaReport(
                kvm=kvm,
                success=False,
                error=perception.stop_reason or "The visible calendar could not be trusted.",
            )
        if not perception.calendar_visible:
            return AgendaReport(
                kvm=kvm,
                success=False,
                error=(
                    "No calendar was visible. Open the Teams calendar or a calendar tab on the "
                    "remote machine; this workflow never opens one itself."
                ),
            )
        if not perception.showing_today:
            shown = f" It is showing {perception.date_text}." if perception.date_text else ""
            return AgendaReport(
                kvm=kvm,
                success=False,
                surface=perception.surface,
                date_text=perception.date_text,
                error=f"The visible calendar is not on today.{shown}",
            )
        return None

    @staticmethod
    def _controller_lock_for_profile(kvm: str) -> ControllerLock:
        settings = PiKVMSettings.from_env(kvm)
        return ControllerLock.for_endpoint(settings.base_url)

    def _dismiss(self, kvm: str, controller_lock: ControllerLock) -> AgentSessionResult:
        return self._controller(kvm, DISMISS_OBJECTIVE, controller_lock)

    def _foreground(self, kvm: str, controller_lock: ControllerLock) -> AgentSessionResult:
        return self._controller(kvm, FOREGROUND_OBJECTIVE, controller_lock)

    def _scroll(self, kvm: str, controller_lock: ControllerLock) -> AgentSessionResult:
        return self._controller(
            kvm, SCROLL_OBJECTIVE, controller_lock, policy_engine=AgendaScrollPolicyEngine()
        )

    def _controller(
        self,
        kvm: str,
        objective: str,
        controller_lock: ControllerLock,
        *,
        policy_engine: AgendaPolicyEngine | None = None,
    ) -> AgentSessionResult:
        args = argparse.Namespace(
            command="agent-run",
            profile=kvm,
            objective=objective,
            timeout=None,
            approval_mode=ApprovalMode.SAFE,
            debug_dir=None,
            vision_model=None,
            planner_model=None,
            max_steps=None,
            step=False,
            dry_run=False,
        )
        return self._executor(
            args,
            controller_lock=controller_lock,
            output=lambda message: self._trace_output(f"{kvm}  | {message}"),
            approval_provider=NonInteractiveApprovalProvider(),
            policy_engine=policy_engine or AgendaPolicyEngine(),
            vision_detail=ImageDetail.HIGH,
            analyzer_transform=lambda analyzer, _settings: AgendaScreenAnalyzer(
                analyzer,
                event_sink=lambda message: self._trace_output(f"{kvm}  | {message}"),
                warning_sink=self._note_warnings,
            ),
        )

    def _note_warnings(self, warnings: tuple[str, ...]) -> None:
        # Category names only, so a screen_unsafe stop can be diagnosed from the log without
        # persisting anything the screen showed.
        self._last_warnings = warnings

    def _read(self, kvm: str) -> AgendaPerception:
        settings = PiKVMSettings.from_env(kvm)
        vision_settings = VisionSettings.from_env()
        analyzer = OpenAIScreenAnalyzer(vision_settings)
        self._trace_output(f"{kvm}  | Reading today's meetings; no action will be sent.")
        with PiKVMSession(settings, totp_provider=build_totp_provider(settings)) as session:
            screenshot = session.get_screenshot()
        perception, telemetry = analyzer.perceive(
            screenshot.content,
            schema=AgendaPerception,
            instructions=AGENDA_PROMPT,
            context=AGENDA_CONTEXT,
            width=screenshot.size.width,
            height=screenshot.size.height,
            options=AnalysisOptions(image_detail=ImageDetail.HIGH),
        )
        self._trace_output(
            f"{kvm}  | Calendar read: {len(perception.meetings)} meeting(s) visible "
            f"(confidence {perception.confidence:.2f}, {telemetry.usage.total_tokens} tokens)"
        )
        return perception
