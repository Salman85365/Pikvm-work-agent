from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from work_agent.agent.models import Action, ActionProposal


def test_action_proposal_parses_exactly_one_discriminated_action() -> None:
    proposal = ActionProposal.model_validate(
        {
            "action": {"type": "hotkey", "keys": ["ControlLeft", "KeyL"]},
            "expected_outcome": "Address bar becomes focused.",
            "confidence": 0.95,
            "risk": "navigation",
            "reason_summary": "Use deterministic browser navigation.",
        }
    )

    assert proposal.action.type == "hotkey"


def test_action_proposal_schema_uses_supported_union_keywords() -> None:
    schema = ActionProposal.model_json_schema()

    def keys(value: object) -> set[str]:
        if isinstance(value, dict):
            return set(value).union(*(keys(item) for item in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(item) for item in value))
        return set()

    schema_keys = keys(schema)
    assert schema["type"] == "object"
    assert "anyOf" in schema_keys
    assert "oneOf" not in schema_keys
    assert "discriminator" not in schema_keys


@pytest.mark.parametrize(
    "action",
    [
        {"type": "click", "x": 100, "y": 200},
        {"type": "shell", "command": "echo no"},
        {"type": "scroll", "direction": "down", "amount": 100},
        {"type": "wait", "seconds": 30},
        {"type": "hotkey", "keys": ["A", "B", "C", "D", "E"]},
        {"type": "type_text", "text": "x" * 501, "purpose": "local_input"},
    ],
)
def test_unsupported_or_unbounded_actions_are_rejected(action: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(Action).validate_python(action)


def test_action_proposal_rejects_out_of_range_confidence_and_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ActionProposal.model_validate(
            {
                "action": {"type": "press_key", "key": "Escape"},
                "expected_outcome": "Menu closes.",
                "confidence": 1.1,
                "risk": "navigation",
                "reason_summary": "Close menu.",
                "second_action": {"type": "press_key", "key": "Enter"},
            }
        )
