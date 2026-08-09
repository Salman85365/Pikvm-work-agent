from __future__ import annotations

import pytest
from pydantic import ValidationError

from work_agent.vision import (
    BoundingBox,
    NormalizedPoint,
    ScreenPerception,
    ScreenState,
)


def test_screen_perception_parses_strict_structured_output() -> None:
    perception = ScreenPerception.model_validate(
        {
            "application": "Slack",
            "screen_state": "application",
            "summary": "Slack is visible.",
            "target_found": False,
            "target": None,
            "relevant_elements": [],
            "warnings": [],
            "safe_to_continue": True,
            "stop_reason": None,
            "confidence": 0.94,
        }
    )

    assert perception.application == "Slack"
    assert perception.screen_state is ScreenState.APPLICATION
    assert perception.confidence == 0.94


@pytest.mark.parametrize(
    "point",
    [
        {"x": -1, "y": 500},
        {"x": 1001, "y": 500},
        {"x": 500, "y": -1},
        {"x": 500, "y": 1001},
    ],
)
def test_out_of_range_normalized_point_is_rejected(point: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        NormalizedPoint.model_validate(point)


def test_reversed_bounding_box_is_rejected() -> None:
    with pytest.raises(ValidationError, match="maximums"):
        BoundingBox(x1=800, y1=100, x2=200, y2=300)


def test_inconsistent_target_and_safety_fields_are_rejected() -> None:
    with pytest.raises(ValidationError, match="target_found"):
        ScreenPerception(
            application="Desktop",
            screen_state=ScreenState.DESKTOP,
            summary="Desktop is visible.",
            target_found=True,
            target=None,
            relevant_elements=[],
            warnings=[],
            safe_to_continue=True,
            stop_reason=None,
            confidence=0.9,
        )
