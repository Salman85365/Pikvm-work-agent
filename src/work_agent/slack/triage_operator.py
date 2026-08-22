from __future__ import annotations

import argparse
from collections.abc import Callable

from work_agent.agent.approval import NonInteractiveApprovalProvider
from work_agent.agent.cli import execute_agent_command
from work_agent.agent.errors import AgentError, ControllerLockError
from work_agent.agent.lock import DEFAULT_LOCK_WAIT_SECONDS, ControllerLock
from work_agent.agent.models import AgentFinalStatus, AgentSessionResult, ApprovalMode
from work_agent.agent.pikvm_session import PiKVMSession
from work_agent.pikvm import PiKVMError, PiKVMSettings, build_totp_provider
from work_agent.slack.triage import (
    DISMISS_OBJECTIVE,
    FOREGROUND_OBJECTIVE,
    TRIAGE_CONTEXT,
    TRIAGE_PROMPT,
    build_items,
)
from work_agent.slack.triage_models import SlackTriagePerception, TriageReport
from work_agent.slack.triage_policy import SlackTriagePolicyEngine
from work_agent.vision import (
    AnalysisOptions,
    ImageDetail,
    OpenAIScreenAnalyzer,
    VisionError,
    VisionSettings,
)


class SlackTriageOperator:
    """Bring Slack forward, then read its unread sidebar without opening anything.

    Foregrounding and reading are deliberately separate phases: the read runs on a plain
    screenshot with no executor attached, so it is structurally incapable of sending HID.
    """

    def __init__(
        self,
        *,
        executor: Callable[..., AgentSessionResult] = execute_agent_command,
        trace_output: Callable[[str], None] | None = None,
        controller_lock_factory: Callable[[str], ControllerLock] | None = None,
        controller_lock_wait_seconds: float = DEFAULT_LOCK_WAIT_SECONDS,
    ) -> None:
        self._executor = executor
        self._trace_output = trace_output or (lambda _: None)
        self._controller_lock_factory = controller_lock_factory or _controller_lock_for_profile
        self._controller_lock_wait_seconds = controller_lock_wait_seconds

    def execute(self, kvm: str) -> TriageReport:
        # Hold the endpoint for foreground, read, and dismiss together so a scheduled Slack run
        # cannot slip in between phases and change the screen under the read.
        try:
            controller_lock = self._controller_lock_factory(kvm)
            controller_lock.acquire(
                timeout_seconds=self._controller_lock_wait_seconds,
                on_wait=lambda: self._trace_output(
                    f"{kvm}  | Another local workflow is using this PiKVM; waiting for it."
                ),
            )
        except ControllerLockError:
            return TriageReport(
                kvm=kvm,
                success=False,
                error=(
                    "Another local workflow kept using this PiKVM for the whole wait, so this "
                    "triage was skipped."
                ),
                stop_code="lock_busy",
            )
        except (PiKVMError, OSError, ValueError) as exc:
            return TriageReport(kvm=kvm, success=False, error=str(exc))
        try:
            return self._execute_locked(kvm, controller_lock)
        finally:
            controller_lock.release()

    def _execute_locked(self, kvm: str, controller_lock: ControllerLock) -> TriageReport:
        try:
            session = self._foreground(kvm, controller_lock)
        except (AgentError, PiKVMError, VisionError) as exc:
            return TriageReport(kvm=kvm, success=False, error=str(exc))
        except (OSError, ValueError):
            return TriageReport(
                kvm=kvm,
                success=False,
                error="The local Slack triage controller could not be started.",
            )

        if session.status is not AgentFinalStatus.SUCCESS:
            return TriageReport(
                kvm=kvm,
                success=False,
                error=f"Slack could not be brought to the foreground: {session.summary}",
                stop_code=session.stop_code.value,
            )

        try:
            perception = self._read(kvm)
            # A popover left open over the direct-message list makes an obstructed read look
            # empty. Reporting "nothing unread" then would be a false negative, so dismiss the
            # overlay with Escape and read once more before believing an empty sidebar.
            if perception.sidebar_obstructed:
                self._trace_output(
                    f"{kvm}  | Sidebar is covered"
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
        except (PiKVMError, VisionError) as exc:
            return TriageReport(kvm=kvm, success=False, error=str(exc))
        except (OSError, ValueError):
            return TriageReport(
                kvm=kvm,
                success=False,
                error="The Slack unread sidebar could not be read locally.",
            )

        if not perception.safe_to_read:
            return TriageReport(
                kvm=kvm,
                success=False,
                error=perception.stop_reason or "The visible Slack state could not be trusted.",
            )
        if not perception.slack_foreground or not perception.sidebar_visible:
            return TriageReport(
                kvm=kvm,
                success=False,
                error="Slack's conversation sidebar was not visible, so nothing was read.",
            )

        items = build_items(perception)
        if perception.sidebar_obstructed and not items:
            # Never let a blocked read masquerade as a quiet inbox.
            return TriageReport(
                kvm=kvm,
                success=False,
                sidebar_obstructed=True,
                confidence=perception.confidence,
                error=(
                    "Something is still covering Slack's conversation list"
                    f"{f' ({perception.obstruction})' if perception.obstruction else ''}, so an "
                    "empty result cannot be trusted. Nothing was reported."
                ),
            )

        return TriageReport(
            kvm=kvm,
            success=True,
            items=items,
            total_unread_badge=perception.total_unread_badge,
            sidebar_truncated=perception.sidebar_truncated,
            sidebar_obstructed=perception.sidebar_obstructed,
            confidence=perception.confidence,
        )

    def _dismiss(self, kvm: str, controller_lock: ControllerLock) -> AgentSessionResult:
        return self._controller(kvm, DISMISS_OBJECTIVE, controller_lock)

    def _foreground(self, kvm: str, controller_lock: ControllerLock) -> AgentSessionResult:
        return self._controller(kvm, FOREGROUND_OBJECTIVE, controller_lock)

    def _controller(
        self, kvm: str, objective: str, controller_lock: ControllerLock
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
            output=lambda message: self._trace_output(f"{kvm}  | {message}"),
            approval_provider=NonInteractiveApprovalProvider(),
            controller_lock=controller_lock,
            policy_engine=SlackTriagePolicyEngine(),
            vision_detail=ImageDetail.HIGH,
        )

    def _read(self, kvm: str) -> SlackTriagePerception:
        settings = PiKVMSettings.from_env(kvm)
        vision_settings = VisionSettings.from_env()
        analyzer = OpenAIScreenAnalyzer(vision_settings)
        self._trace_output(f"{kvm}  | Reading the unread sidebar; no action will be sent.")
        with PiKVMSession(settings, totp_provider=build_totp_provider(settings)) as session:
            screenshot = session.get_screenshot()
        perception, telemetry = analyzer.perceive(
            screenshot.content,
            schema=SlackTriagePerception,
            instructions=TRIAGE_PROMPT,
            context=TRIAGE_CONTEXT,
            width=screenshot.size.width,
            height=screenshot.size.height,
            options=AnalysisOptions(image_detail=ImageDetail.HIGH),
        )
        self._trace_output(
            f"{kvm}  | Sidebar read: {len(perception.conversations)} unread entries "
            f"(confidence {perception.confidence:.2f}, {telemetry.usage.total_tokens} tokens)"
        )
        return perception


def _controller_lock_for_profile(kvm: str) -> ControllerLock:
    return ControllerLock.for_endpoint(PiKVMSettings.from_env(kvm).base_url)
