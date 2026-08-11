from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from work_agent.agent.approval import ApprovalProvider, TerminalApprovalProvider
from work_agent.agent.config import HARD_MAX_RUNTIME_SECONDS, HARD_MAX_STEPS, AgentSettings
from work_agent.agent.controller import AgentController, ControllerOptions
from work_agent.agent.debug import DebugArtifacts
from work_agent.agent.executor import ActionExecutor
from work_agent.agent.lock import ControllerLock
from work_agent.agent.models import AgentFinalStatus, AgentSessionResult, ApprovalMode
from work_agent.agent.openai_planner import OpenAIActionPlanner
from work_agent.agent.pikvm_session import PiKVMSession
from work_agent.agent.policy import PolicyEngine
from work_agent.agent.screen_change import PreActionGuard, ScreenSettleDetector
from work_agent.pikvm import PiKVMSettings, TotpProvider, build_totp_provider
from work_agent.vision import (
    ActionVerification,
    ImageDetail,
    OpenAIScreenAnalyzer,
    ScreenAnalyzer,
    VisionSettings,
)
from work_agent.vision.models import ScreenAnalysis


def _objective(raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        raise argparse.ArgumentTypeError("objective must not be empty")
    return value


def _model_name(raw_value: str) -> str:
    value = raw_value.strip()
    if not value or any(character.isspace() for character in value):
        raise argparse.ArgumentTypeError("model must be a non-empty identifier without whitespace")
    return value


def _max_steps(raw_value: str) -> int:
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("max steps must be an integer") from exc
    if not 1 <= value <= HARD_MAX_STEPS:
        raise argparse.ArgumentTypeError(
            f"max steps must be between 1 and the hard cap of {HARD_MAX_STEPS}"
        )
    return value


def _timeout(raw_value: str) -> float:
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a number") from exc
    if not 0 < value <= HARD_MAX_RUNTIME_SECONDS:
        raise argparse.ArgumentTypeError(
            "timeout must be greater than zero and no more than "
            f"{HARD_MAX_RUNTIME_SECONDS:g} seconds"
        )
    return value


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--objective", type=_objective, required=True)
    parser.add_argument("--timeout", type=_timeout, default=None)
    parser.add_argument(
        "--approval-mode",
        type=ApprovalMode,
        choices=list(ApprovalMode),
        default=ApprovalMode.SAFE,
    )
    parser.add_argument(
        "--debug-dir",
        type=Path,
        default=None,
        help="Explicitly save sensitive local screenshots and structured debug artifacts.",
    )
    parser.add_argument("--vision-model", type=_model_name, default=None)
    parser.add_argument("--planner-model", type=_model_name, default=None)


def add_agent_parsers(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    agent_run = subparsers.add_parser(
        "agent-run",
        help="Run the bounded observe-plan-policy-act-verify controller.",
    )
    _add_common_arguments(agent_run)
    agent_run.add_argument("--max-steps", type=_max_steps, default=None)
    agent_run.add_argument(
        "--step",
        action="store_true",
        help="Pause for local confirmation before each executable action.",
    )
    agent_run.add_argument(
        "--dry-run",
        action="store_true",
        help="Observe, plan, and apply policy, but issue no HID action.",
    )

    agent_step = subparsers.add_parser(
        "agent-step",
        help="Inspect exactly one proposal; use --execute for at most one verified action.",
    )
    _add_common_arguments(agent_step)
    agent_step.add_argument(
        "--execute",
        action="store_true",
        help="After confirmation, execute at most one action and verify it.",
    )


def execute_agent_command(
    args: argparse.Namespace,
    *,
    totp_provider: TotpProvider | None = None,
    output: Callable[[str], None] = print,
    approval_provider: ApprovalProvider | None = None,
    observation_sink: Callable[[ScreenAnalysis], None] | None = None,
    completion_validator: Callable[[ScreenAnalysis], str | None] | None = None,
    policy_engine: PolicyEngine | None = None,
    vision_detail: ImageDetail | None = None,
    verification_override: (
        Callable[[ScreenAnalysis, ActionVerification], ActionVerification | None] | None
    ) = None,
    analyzer_transform: Callable[[ScreenAnalyzer, VisionSettings], ScreenAnalyzer] | None = None,
) -> AgentSessionResult:
    agent_settings = AgentSettings.from_env()
    if args.planner_model is not None:
        agent_settings = replace(agent_settings, planner_model=args.planner_model)
    vision_settings = VisionSettings.from_env()
    pikvm_settings = PiKVMSettings.from_env(getattr(args, "profile", None))
    selected_totp_provider = totp_provider or build_totp_provider(pikvm_settings)

    is_step_command = args.command == "agent-step"
    options = ControllerOptions.from_settings(
        agent_settings,
        max_steps=1 if is_step_command else args.max_steps,
        timeout_seconds=args.timeout,
        approval_mode=args.approval_mode,
        step_mode=(args.execute if is_step_command else args.step),
        dry_run=(not args.execute if is_step_command else args.dry_run),
        vision_model=args.vision_model,
        vision_detail=vision_detail,
    )
    debug = DebugArtifacts(args.debug_dir)
    if debug.enabled:
        output(
            "Warning: debug artifacts may contain private work information; "
            "keep the directory local."
        )

    with (
        ControllerLock.for_endpoint(pikvm_settings.base_url),
        PiKVMSession(
            pikvm_settings,
            totp_provider=selected_totp_provider,
        ) as session,
    ):
        base_analyzer: ScreenAnalyzer = OpenAIScreenAnalyzer(vision_settings)
        analyzer = (
            analyzer_transform(base_analyzer, vision_settings)
            if analyzer_transform is not None
            else base_analyzer
        )
        planner = OpenAIActionPlanner(agent_settings)
        controller = AgentController(
            capture=session.get_screenshot,
            analyzer=analyzer,
            planner=planner,
            policy=policy_engine or PolicyEngine(),
            executor=ActionExecutor(session),
            guard=PreActionGuard(material_change_threshold=agent_settings.stale_screen_threshold),
            settle_detector=ScreenSettleDetector(
                poll_interval_seconds=agent_settings.screen_poll_interval_seconds,
                timeout_seconds=agent_settings.screen_change_timeout_seconds,
                stable_frames=agent_settings.screen_stable_frames,
                stable_threshold=agent_settings.screen_stable_threshold,
                localized_change_threshold=(agent_settings.screen_localized_change_threshold),
            ),
            approval_provider=approval_provider or TerminalApprovalProvider(output=output),
            settings=agent_settings,
            options=options,
            debug_artifacts=debug,
            event_sink=output,
            observation_sink=observation_sink,
            completion_validator=completion_validator,
            verification_override=verification_override,
        )
        return controller.run(args.objective)


def format_session_result(result: AgentSessionResult) -> str:
    telemetry = result.telemetry
    input_tokens = telemetry.vision_usage.input_tokens + telemetry.planner_usage.input_tokens
    output_tokens = telemetry.vision_usage.output_tokens + telemetry.planner_usage.output_tokens
    return "\n".join(
        [
            f"Result: {result.status.value}",
            f"Summary: {result.summary}",
            f"Steps: {telemetry.steps}",
            f"HID actions: {telemetry.hid_actions}",
            f"Vision calls: {telemetry.vision_calls}",
            f"Planner calls: {telemetry.planner_calls}",
            f"Approvals: {telemetry.approval_requests}",
            f"Stale actions cancelled: {telemetry.stale_action_cancellations}",
            f"Runtime: {telemetry.runtime_seconds:.2f}s",
            f"Input tokens: {input_tokens}",
            f"Output tokens: {output_tokens}",
        ]
    )


def result_exit_code(result: AgentSessionResult) -> int:
    if result.status in {AgentFinalStatus.SUCCESS, AgentFinalStatus.DRY_RUN}:
        return 0
    if result.status is AgentFinalStatus.FAILED:
        return 1
    return 2
