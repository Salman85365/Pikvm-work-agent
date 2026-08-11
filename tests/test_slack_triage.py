from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from work_agent.agent.models import (
    ActionProposal,
    AgentFinalStatus,
    ClickElementAction,
    DoubleClickElementAction,
    FinishAction,
    HotkeyAction,
    PolicyDecisionKind,
    PressKeyAction,
    RiskCategory,
    ScrollAction,
    ScrollDirection,
    StopCode,
    TextPurpose,
    TypeTextAction,
)
from work_agent.slack.cli import execute_slack_triage_command, format_triage_batch
from work_agent.slack.triage import FOREGROUND_OBJECTIVE, TRIAGE_PROMPT, build_items, classify
from work_agent.slack.triage_models import (
    AttentionLevel,
    ConversationKind,
    SlackTriagePerception,
    TriageBatchResult,
    TriageReport,
    UnreadConversation,
)
from work_agent.slack.triage_operator import SlackTriageOperator
from work_agent.slack.triage_policy import SlackTriagePolicyEngine
from work_agent.slack.triage_service import JsonlTriageLogger, SlackTriageService
from work_agent.vision import (
    AnalysisUsage,
    ImageDetail,
    ReasoningEffort,
    ScreenAnalysis,
    ScreenState,
    ServiceTier,
    UIElement,
    UIElementRole,
)


def _conversation(
    name: str,
    *,
    kind: ConversationKind = ConversationKind.CHANNEL,
    unread: int = 1,
    mention: bool = False,
    muted: bool = False,
) -> UnreadConversation:
    return UnreadConversation(
        name=name,
        kind=kind,
        unread_count=unread,
        has_mention=mention,
        muted=muted,
    )


def _perception(
    conversations: list[UnreadConversation],
    *,
    foreground: bool = True,
    sidebar: bool = True,
    safe: bool = True,
    truncated: bool = False,
) -> SlackTriagePerception:
    return SlackTriagePerception(
        slack_foreground=foreground,
        sidebar_visible=sidebar,
        conversations=conversations,
        total_unread_badge=sum(item.unread_count for item in conversations) or None,
        sidebar_truncated=truncated,
        summary="Slack sidebar is visible.",
        safe_to_read=safe,
        stop_reason=None if safe else "The remote feed is blank.",
        confidence=0.94,
    )


def _screen(elements: list[UIElement], *, application: str = "Finder") -> ScreenAnalysis:
    return ScreenAnalysis(
        objective=FOREGROUND_OBJECTIVE,
        application=application,
        screen_state=ScreenState.APPLICATION,
        summary="Desktop with a dock.",
        target_found=False,
        target=None,
        relevant_elements=elements,
        warnings=[],
        safe_to_continue=True,
        stop_reason=None,
        confidence=0.96,
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
    confidence: float = 0.95,
) -> UIElement:
    return UIElement(
        id=element_id,
        label=label,
        role=role,
        visible_text=visible_text,
        bounding_box=None,
        click_point={"x": 100, "y": 900},  # type: ignore[arg-type]
        confidence=confidence,
    )


def _proposal(action: object, *, risk: RiskCategory = RiskCategory.NAVIGATION) -> ActionProposal:
    return ActionProposal(
        action=action,  # type: ignore[arg-type]
        expected_outcome="Slack becomes foreground.",
        confidence=0.97,
        risk=risk,
        reason_summary="Bring Slack forward.",
    )


# ---------------- perception schema ----------------


def test_perception_rejects_conversations_without_a_visible_sidebar() -> None:
    with pytest.raises(ValueError, match="sidebar is not visible"):
        SlackTriagePerception(
            slack_foreground=True,
            sidebar_visible=False,
            conversations=[_conversation("#general")],
            total_unread_badge=1,
            sidebar_truncated=False,
            summary="Sidebar hidden.",
            safe_to_read=True,
            stop_reason=None,
            confidence=0.9,
        )


def test_perception_requires_a_reason_when_not_readable() -> None:
    with pytest.raises(ValueError, match="must explain why"):
        SlackTriagePerception(
            slack_foreground=True,
            sidebar_visible=True,
            conversations=[],
            total_unread_badge=None,
            sidebar_truncated=False,
            summary="Blank.",
            safe_to_read=False,
            stop_reason=None,
            confidence=0.4,
        )


def test_perception_schema_carries_no_message_text_field() -> None:
    """Triage must be structurally incapable of returning message bodies."""
    forbidden = {"message", "text", "preview", "body", "content", "snippet"}
    for field in UnreadConversation.model_fields:
        assert not any(word in field for word in forbidden), field


def test_prompt_forbids_reporting_message_contents() -> None:
    lowered = TRIAGE_PROMPT.casefold()
    assert "do not report message text" in lowered
    assert "do not infer anything about what a message says" in lowered
    assert "do not open" in FOREGROUND_OBJECTIVE.casefold()
    assert "marks it read" in FOREGROUND_OBJECTIVE.casefold()


# ---------------- classification ----------------


@pytest.mark.parametrize(
    ("kind", "mention", "expected"),
    [
        (ConversationKind.CHANNEL, True, AttentionLevel.MENTIONED),
        (ConversationKind.DIRECT_MESSAGE, False, AttentionLevel.DIRECT),
        (ConversationKind.GROUP_MESSAGE, False, AttentionLevel.DIRECT),
        (ConversationKind.CHANNEL, False, AttentionLevel.UNREAD),
        (ConversationKind.THREAD, False, AttentionLevel.UNREAD),
    ],
)
def test_attention_is_ranked_from_visible_state_only(
    kind: ConversationKind,
    mention: bool,
    expected: AttentionLevel,
) -> None:
    assert classify(kind, has_mention=mention) is expected


def test_items_are_ordered_by_attention_then_volume_and_drop_muted() -> None:
    perception = _perception(
        [
            _conversation("#noise", unread=40),
            _conversation("patrick", kind=ConversationKind.DIRECT_MESSAGE, unread=2),
            _conversation("#deploys", unread=3, mention=True),
            _conversation("#muted-firehose", unread=99, muted=True),
            _conversation("   ", unread=1),
        ]
    )

    items = build_items(perception)

    assert [item.name for item in items] == ["#deploys", "patrick", "#noise"]
    assert items[0].attention is AttentionLevel.MENTIONED


# ---------------- policy: the non-destructive guarantee ----------------


def test_policy_allows_clicking_the_slack_launcher() -> None:
    screen = _screen([_element("dock-slack", "Slack")])

    decision = SlackTriagePolicyEngine().evaluate(
        _proposal(ClickElementAction(type="click_element", element_id="dock-slack", button="left")),
        screen,
    )

    assert decision.decision is PolicyDecisionKind.ALLOW


@pytest.mark.parametrize(
    ("element_id", "label", "role", "visible_text"),
    [
        ("dm-patrick", "Patrick", UIElementRole.LIST_ITEM, "2"),
        ("channel-general", "#general", UIElementRole.LIST_ITEM, "5"),
        ("sidebar-slack", "Slack", UIElementRole.LIST_ITEM, "3 unread messages"),
        ("thread-row", "Thread with Anthony", UIElementRole.LIST_ITEM, ""),
    ],
)
def test_policy_denies_opening_any_conversation(
    element_id: str,
    label: str,
    role: UIElementRole,
    visible_text: str,
) -> None:
    """The read-preserving boundary must hold even when the planner asks nicely."""
    screen = _screen([_element(element_id, label, role=role, visible_text=visible_text)])

    decision = SlackTriagePolicyEngine().evaluate(
        _proposal(ClickElementAction(type="click_element", element_id=element_id, button="left")),
        screen,
    )

    assert decision.decision is PolicyDecisionKind.DENY
    assert "mark it read" in decision.reason


def test_policy_denies_double_click_and_scroll() -> None:
    screen = _screen([_element("dock-slack", "Slack")])
    engine = SlackTriagePolicyEngine()

    double = engine.evaluate(
        _proposal(DoubleClickElementAction(type="double_click_element", element_id="dock-slack")),
        screen,
    )
    scroll = engine.evaluate(
        _proposal(ScrollAction(type="scroll", direction=ScrollDirection.DOWN, amount=2)),
        screen,
    )

    assert double.decision is PolicyDecisionKind.DENY
    assert scroll.decision is PolicyDecisionKind.DENY


def test_policy_allows_only_the_documented_launch_shortcuts() -> None:
    # Spotlight is open, so the generic engine sees a real search field to type into.
    screen = _screen(
        [
            _element(
                "spotlight",
                "Spotlight Search",
                role=UIElementRole.TEXT_FIELD,
                visible_text="Search",
            )
        ]
    )
    engine = SlackTriagePolicyEngine()

    spotlight = engine.evaluate(
        _proposal(HotkeyAction(type="hotkey", keys=["MetaLeft", "Space"])),
        screen,
    )
    search = engine.evaluate(
        _proposal(
            TypeTextAction(
                type="type_text",
                text="slack",
                purpose=TextPurpose.NAVIGATION_SEARCH,
            )
        ),
        screen,
    )
    other_app = engine.evaluate(
        _proposal(
            TypeTextAction(
                type="type_text",
                text="terminal",
                purpose=TextPurpose.NAVIGATION_SEARCH,
            )
        ),
        screen,
    )
    other_hotkey = engine.evaluate(
        _proposal(HotkeyAction(type="hotkey", keys=["ControlLeft", "KeyF"])),
        screen,
    )

    assert spotlight.decision is PolicyDecisionKind.ALLOW
    assert search.decision is PolicyDecisionKind.ALLOW
    assert other_app.decision is PolicyDecisionKind.DENY
    assert other_hotkey.decision is PolicyDecisionKind.DENY


def test_policy_still_defers_to_the_generic_engine_on_unsafe_screens() -> None:
    screen = _screen([_element("dock-slack", "Slack")]).model_copy(
        update={"screen_state": ScreenState.LOCK_SCREEN}
    )

    decision = SlackTriagePolicyEngine().evaluate(
        _proposal(ClickElementAction(type="click_element", element_id="dock-slack", button="left")),
        screen,
    )

    assert decision.decision is PolicyDecisionKind.DENY
    assert decision.inferred_risk is RiskCategory.AUTHENTICATION


def test_policy_allows_finish_without_hid() -> None:
    decision = SlackTriagePolicyEngine().evaluate(
        _proposal(FinishAction(type="finish", summary="Slack is foreground.")),
        _screen([], application="Slack"),
    )

    assert decision.decision is PolicyDecisionKind.ALLOW


# ---------------- operator ----------------


class _Operator(SlackTriageOperator):
    def __init__(self, perception: SlackTriagePerception | None, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._perception = perception

    def _read(self, kvm: str) -> SlackTriagePerception:
        assert self._perception is not None
        return self._perception


def test_operator_uses_the_triage_policy_and_never_approves_interactively() -> None:
    received: dict[str, object] = {}

    def executor(args: object, **kwargs: object) -> object:
        received["args"] = args
        received.update(kwargs)
        return SimpleNamespace(status=AgentFinalStatus.SUCCESS, stop_code=StopCode.COMPLETED)

    report = _Operator(
        _perception([_conversation("patrick", kind=ConversationKind.DIRECT_MESSAGE, unread=2)]),
        executor=executor,
    ).execute("work-kvm")

    assert report.success is True
    assert [item.name for item in report.needs_attention] == ["patrick"]
    assert isinstance(received["policy_engine"], SlackTriagePolicyEngine)
    assert received["approval_provider"].__class__.__name__ == "NonInteractiveApprovalProvider"
    assert received["vision_detail"] is ImageDetail.HIGH
    # Triage never supplies an executor-side completion validator: no action is verified.
    assert "completion_validator" not in received


def test_operator_reports_the_stop_code_when_foregrounding_fails() -> None:
    def executor(args: object, **kwargs: object) -> object:
        return SimpleNamespace(
            status=AgentFinalStatus.FAILED,
            stop_code=StopCode.POLICY_DENIED,
            summary="Policy denied the proposed action: opening a conversation.",
        )

    report = _Operator(None, executor=executor).execute("work-kvm")

    assert report.success is False
    assert report.stop_code == "policy_denied"
    assert report.items == ()


def test_operator_refuses_to_report_when_the_sidebar_is_not_visible() -> None:
    def executor(args: object, **kwargs: object) -> object:
        return SimpleNamespace(status=AgentFinalStatus.SUCCESS, stop_code=StopCode.COMPLETED)

    report = _Operator(
        _perception([], sidebar=False),
        executor=executor,
    ).execute("work-kvm")

    assert report.success is False
    assert "sidebar was not visible" in (report.error or "")


def test_operator_stops_on_an_untrusted_screen() -> None:
    def executor(args: object, **kwargs: object) -> object:
        return SimpleNamespace(status=AgentFinalStatus.SUCCESS, stop_code=StopCode.COMPLETED)

    report = _Operator(_perception([], safe=False), executor=executor).execute("work-kvm")

    assert report.success is False
    assert report.error == "The remote feed is blank."


# ---------------- service, logging, CLI ----------------


class _FakeOperator:
    def __init__(self, reports: dict[str, TriageReport]) -> None:
        self._reports = reports
        self.calls: list[str] = []

    def execute(self, kvm: str) -> TriageReport:
        self.calls.append(kvm)
        return self._reports[kvm]


def test_triage_log_records_counts_but_never_conversation_names(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "slack-triage.jsonl"
    path.parent.mkdir()
    logger = JsonlTriageLogger(path)
    report = TriageReport(
        kvm="work-kvm",
        success=True,
        items=build_items(
            _perception(
                [
                    _conversation("patrick", kind=ConversationKind.DIRECT_MESSAGE, unread=2),
                    _conversation("#deploys", unread=1, mention=True),
                    _conversation("#general", unread=7),
                ]
            )
        ),
        sidebar_truncated=True,
    )

    logger.record(report)

    entry = json.loads(path.read_text(encoding="utf-8"))
    assert set(entry) == {
        "timestamp",
        "kvm",
        "outcome",
        "unread_conversations",
        "mentions",
        "direct_messages",
        "sidebar_truncated",
        "stop_code",
        "error",
    }
    assert entry["unread_conversations"] == 3
    assert entry["mentions"] == 1
    assert entry["direct_messages"] == 1
    # The roadmap forbids persisting Slack content; names are content.
    serialized = path.read_text(encoding="utf-8")
    for name in ("patrick", "deploys", "general"):
        assert name not in serialized
    assert path.stat().st_mode & 0o777 == 0o600


def test_service_isolates_a_failing_kvm_and_keeps_going(tmp_path: Path) -> None:
    reports = {
        "one": TriageReport(kvm="one", success=False, error="boom"),
        "two": TriageReport(
            kvm="two",
            success=True,
            items=build_items(_perception([_conversation("#ops", unread=1)])),
        ),
    }
    operator = _FakeOperator(reports)
    service = SlackTriageService(operator, JsonlTriageLogger(tmp_path / "triage.jsonl"))

    result = service.run(("one", "two"))

    assert operator.calls == ["one", "two"]
    assert result.success is False
    assert [report.kvm for report in result.reports] == ["one", "two"]


def test_cli_triage_resolves_targets_and_formats_a_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from work_agent.slack import cli as slack_cli

    monkeypatch.setattr(slack_cli, "configured_pikvm_profiles", lambda: ("heidrick", "nbc_kvm"))
    reports = {
        "heidrick": TriageReport(
            kvm="heidrick",
            success=True,
            items=build_items(
                _perception(
                    [
                        _conversation("patrick", kind=ConversationKind.DIRECT_MESSAGE, unread=2),
                        _conversation("#deploys", unread=1, mention=True),
                        _conversation("#general", unread=7),
                    ]
                )
            ),
            sidebar_truncated=True,
        ),
        "nbc_kvm": TriageReport(kvm="nbc_kvm", success=False, error="Slack was not visible."),
    }
    service = SlackTriageService(
        _FakeOperator(reports),
        JsonlTriageLogger(tmp_path / "triage.jsonl"),
    )
    args = argparse.Namespace(slack_command="triage", kvm=None, all_kvms=True, trace=False)

    result = execute_slack_triage_command(args, service=service)
    output = format_triage_batch(result)

    assert result.success is False
    assert "heidrick  ✓ needs attention: 2" in output
    assert "@ #deploys" in output
    assert "· patrick (2)" in output
    assert "FYI: 1" in output
    assert "sidebar was clipped" in output
    assert "nbc_kvm  ✗ Slack was not visible." in output


def test_cli_triage_rejects_an_unknown_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    from work_agent.slack import cli as slack_cli
    from work_agent.slack.errors import SlackAvailabilityError

    monkeypatch.setattr(slack_cli, "configured_pikvm_profiles", lambda: ("heidrick",))
    args = argparse.Namespace(slack_command="triage", kvm="nope", all_kvms=False, trace=False)

    with pytest.raises(SlackAvailabilityError, match="Unknown PiKVM profile"):
        execute_slack_triage_command(args)


def test_empty_batch_is_not_reported_as_success() -> None:
    assert TriageBatchResult(reports=()).success is False


def test_press_key_outside_the_allowlist_is_denied() -> None:
    decision = SlackTriagePolicyEngine().evaluate(
        _proposal(PressKeyAction(type="press_key", key="KeyK")),
        _screen([], application="Slack"),
    )

    assert decision.decision is PolicyDecisionKind.DENY
