from __future__ import annotations

import argparse

import pytest

from work_agent.slack import cli
from work_agent.slack.errors import SlackAvailabilityError
from work_agent.slack.models import Availability, AvailabilityBatchResult, AvailabilityResult


class _Service:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], Availability | None]] = []

    def run(
        self,
        kvms: tuple[str, ...],
        desired: Availability | None,
    ) -> AvailabilityBatchResult:
        self.calls.append((kvms, desired))
        return AvailabilityBatchResult(
            results=tuple(
                AvailabilityResult(
                    kvm=kvm,
                    desired=desired,
                    observed=desired or Availability.ACTIVE,
                    changed=False,
                    success=True,
                )
                for kvm in kvms
            )
        )


def test_all_kvms_preserves_declared_profile_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "configured_pikvm_profiles", lambda: ("heidrick", "lutron-3"))
    service = _Service()
    args = argparse.Namespace(
        slack_command="availability",
        availability_action="set",
        availability=Availability.AWAY,
        all_kvms=True,
        kvm=None,
        trace=False,
    )

    result = cli.execute_slack_command(args, service=service)

    assert result.success is True
    assert service.calls == [(("heidrick", "lutron-3"), Availability.AWAY)]


def test_get_targets_one_declared_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "configured_pikvm_profiles", lambda: ("heidrick", "lutron-3"))
    service = _Service()
    args = argparse.Namespace(
        slack_command="availability",
        availability_action="get",
        all_kvms=False,
        kvm="LUTRON-3",
        trace=False,
    )

    cli.execute_slack_command(args, service=service)

    assert service.calls == [(("lutron-3",), None)]


def test_availability_requires_a_declared_named_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "configured_pikvm_profiles", lambda: ())
    args = argparse.Namespace(
        slack_command="availability",
        availability_action="get",
        all_kvms=False,
        kvm="work-kvm",
        trace=False,
    )

    with pytest.raises(SlackAvailabilityError, match="PIKVM_PROFILES"):
        cli.execute_slack_command(args, service=_Service())
