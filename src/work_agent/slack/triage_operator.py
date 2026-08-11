from __future__ import annotations

import argparse
from collections.abc import Callable

from work_agent.agent.approval import NonInteractiveApprovalProvider
from work_agent.agent.cli import execute_agent_command
from work_agent.agent.errors import AgentError
from work_agent.agent.models import AgentFinalStatus, AgentSessionResult, ApprovalMode
from work_agent.agent.pikvm_session import PiKVMSession
from work_agent.pikvm import PiKVMError, PiKVMSettings, build_totp_provider
from work_agent.slack.triage import (
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
    ) -> None:
        self._executor = executor
        self._trace_output = trace_output or (lambda _: None)

    def execute(self, kvm: str) -> TriageReport:
        try:
            session = self._foreground(kvm)
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

        return TriageReport(
            kvm=kvm,
            success=True,
            items=build_items(perception),
            total_unread_badge=perception.total_unread_badge,
            sidebar_truncated=perception.sidebar_truncated,
            confidence=perception.confidence,
        )

    def _foreground(self, kvm: str) -> AgentSessionResult:
        args = argparse.Namespace(
            command="agent-run",
            profile=kvm,
            objective=FOREGROUND_OBJECTIVE,
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
