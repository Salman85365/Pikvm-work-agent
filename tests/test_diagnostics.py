from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ValidationError

from work_agent import diagnostics
from work_agent.agent.controller import classify_exception
from work_agent.agent.errors import PlannerNetworkError, PlannerStructuredOutputError
from work_agent.agent.models import StopCode
from work_agent.pikvm import PiKVMAuthenticationError, PiKVMConnectionError, PiKVMTimeoutError
from work_agent.slack.agent_operator import _controller_failure_reason
from work_agent.vision.errors import VisionRateLimitError, VisionStructuredOutputError


class _Label(BaseModel):
    label: str


def test_validation_error_description_omits_the_rejected_input() -> None:
    with pytest.raises(ValidationError) as caught:
        _Label.model_validate({"label": 42, "extra": "Do not log this DM name"})

    text = diagnostics.describe_exception(caught.value)

    assert text.startswith("ValidationError:")
    assert "label=" in text
    assert "DM name" not in text and "42" not in text


def test_cause_chain_contributes_class_names_only() -> None:
    try:
        try:
            raise ValueError("secret response body")
        except ValueError as inner:
            raise VisionStructuredOutputError("OpenAI returned invalid output.") from inner
    except VisionStructuredOutputError as outer:
        text = diagnostics.describe_exception(outer)
        trace = diagnostics.format_traceback(outer)

    assert text == (
        "VisionStructuredOutputError: OpenAI returned invalid output. (caused by ValueError)"
    )
    assert "secret response body" not in text
    assert "secret response body" not in trace


def test_configure_logging_creates_owner_only_rotating_file(tmp_path: Path) -> None:
    diagnostics._configured_path = None
    try:
        path = diagnostics.configure_logging(directory=tmp_path / "logs")
        assert path is not None
        logging.getLogger("work_agent.test").info("hello")
        for handler in logging.getLogger("work_agent").handlers:
            handler.flush()
        assert "hello" in path.read_text(encoding="utf-8")
        assert path.stat().st_mode & 0o777 == 0o600
        assert (tmp_path / "logs").stat().st_mode & 0o777 == 0o700
        assert diagnostics.configure_logging(directory=tmp_path / "other") == path
    finally:
        root = logging.getLogger("work_agent")
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        diagnostics._configured_path = None


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (PiKVMConnectionError("Could not reach PiKVM."), StopCode.PIKVM_UNREACHABLE),
        (PiKVMTimeoutError("PiKVM timed out."), StopCode.PIKVM_UNREACHABLE),
        (PiKVMAuthenticationError("Rejected."), StopCode.PIKVM_AUTH_FAILED),
        (VisionRateLimitError("Rate limited."), StopCode.MODEL_PROVIDER_ERROR),
        (PlannerNetworkError("Unreachable."), StopCode.MODEL_PROVIDER_ERROR),
        (VisionStructuredOutputError("Invalid."), StopCode.MODEL_OUTPUT_INVALID),
        (PlannerStructuredOutputError("Invalid."), StopCode.MODEL_OUTPUT_INVALID),
        (OSError("disk"), StopCode.INTERNAL_ERROR),
    ],
)
def test_controller_exceptions_are_classified_by_environment(
    exc: Exception, code: StopCode
) -> None:
    assert classify_exception(exc) is code


def test_environment_failure_reason_keeps_the_sanitized_detail() -> None:
    session = SimpleNamespace(
        stop_code=StopCode.PIKVM_UNREACHABLE,
        summary="Could not reach PiKVM during POST /api/auth/login.",
        status="failed",
    )
    reason = _controller_failure_reason(session)  # type: ignore[arg-type]
    assert reason == (
        "The PiKVM could not be reached. Could not reach PiKVM during POST /api/auth/login."
    )

    verification = SimpleNamespace(
        stop_code=StopCode.VERIFICATION_FAILED,
        summary="Model prose about the screen",
        status="failed",
    )
    assert "Model prose" not in _controller_failure_reason(verification)  # type: ignore[arg-type]
