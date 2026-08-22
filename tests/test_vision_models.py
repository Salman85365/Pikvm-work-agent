from __future__ import annotations

import pytest

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
def test_out_of_range_normalized_point_is_clamped(point: dict[str, int]) -> None:
    clamped = NormalizedPoint.model_validate(point)
    assert 0 <= clamped.x <= 1000 and 0 <= clamped.y <= 1000


def test_reversed_bounding_box_from_the_model_is_reordered() -> None:
    box = BoundingBox.model_validate({"x1": 800, "y1": 100, "x2": 200, "y2": 300})
    assert (box.x1, box.x2) == (200, 800)


def test_reversed_bounding_box_built_locally_is_still_rejected() -> None:
    with pytest.raises(ValueError, match="maximums"):
        BoundingBox.model_construct(x1=800, y1=100, x2=200, y2=300)._validate_order()


def test_inconsistent_target_and_safety_fields_are_normalized_not_rejected() -> None:
    """A screen read is an observation; a slipped flag must not discard the whole session."""
    perception = ScreenPerception.model_validate(
        {
            "application": "",
            "screen_state": "desktop",
            "summary": "Desktop is visible.",
            "target_found": True,
            "target": None,
            "relevant_elements": [
                {
                    "id": "dock_slack",
                    "label": "",
                    "role": "icon",
                    "visible_text": "Slack",
                    "bounding_box": None,
                    "click_point": {"x": 1002, "y": 990},
                    "confidence": 0.9,
                }
            ],
            "warnings": [],
            "safe_to_continue": True,
            "stop_reason": "leftover text",
            "confidence": 0.9,
        }
    )

    assert perception.target_found is False
    assert perception.stop_reason is None
    assert perception.application == "unknown"
    assert perception.relevant_elements[0].label == "Slack"
    assert perception.relevant_elements[0].click_point is not None
    assert perception.relevant_elements[0].click_point.x == 1000

    unsafe = ScreenPerception.model_validate(
        {
            "application": "Finder",
            "screen_state": "dialog",
            "summary": "A dialog.",
            "target_found": False,
            "target": None,
            "relevant_elements": [],
            "warnings": ["unexpected_dialog"],
            "safe_to_continue": False,
            "stop_reason": "",
            "confidence": 0.9,
        }
    )
    assert unsafe.stop_reason is not None
