from __future__ import annotations

import argparse
import plistlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from work_agent.schedule import cli as schedule_cli
from work_agent.schedule.errors import ScheduleError
from work_agent.schedule.launchd import SlackAvailabilityLaunchdManager
from work_agent.schedule.reconcile import KARACHI_TIMEZONE, desired_availability
from work_agent.schedule.state import ReconciliationStateStore
from work_agent.slack.models import Availability, AvailabilityBatchResult, AvailabilityResult


@pytest.mark.parametrize(
    ("local_time", "expected"),
    [
        (datetime(2026, 8, 10, 17, 59, tzinfo=KARACHI_TIMEZONE), Availability.AWAY),
        (datetime(2026, 8, 10, 18, 0, tzinfo=KARACHI_TIMEZONE), Availability.ACTIVE),
        (datetime(2026, 8, 11, 1, 59, tzinfo=KARACHI_TIMEZONE), Availability.ACTIVE),
        (datetime(2026, 8, 11, 2, 0, tzinfo=KARACHI_TIMEZONE), Availability.AWAY),
        (datetime(2026, 8, 14, 23, 0, tzinfo=KARACHI_TIMEZONE), Availability.ACTIVE),
        (datetime(2026, 8, 15, 1, 59, tzinfo=KARACHI_TIMEZONE), Availability.ACTIVE),
        (datetime(2026, 8, 15, 2, 0, tzinfo=KARACHI_TIMEZONE), Availability.AWAY),
        (datetime(2026, 8, 16, 20, 0, tzinfo=KARACHI_TIMEZONE), Availability.AWAY),
    ],
)
def test_schedule_boundaries(local_time: datetime, expected: Availability) -> None:
    assert desired_availability(local_time) is expected
    assert desired_availability(local_time.astimezone(UTC)) is expected


def test_schedule_rejects_naive_time() -> None:
    with pytest.raises(ScheduleError, match="timezone-aware"):
        desired_availability(datetime(2026, 8, 10, 18, 0))


def _batch(
    profiles: tuple[str, ...],
    desired: Availability,
    *,
    failing: frozenset[str] = frozenset(),
) -> AvailabilityBatchResult:
    return AvailabilityBatchResult(
        results=tuple(
            AvailabilityResult(
                kvm=profile,
                desired=desired,
                observed=None if profile in failing else desired,
                changed=False if profile not in failing else None,
                success=profile not in failing,
                error="verification failed" if profile in failing else None,
            )
            for profile in profiles
        )
    )


def test_reconciliation_state_tracks_only_verified_successes(tmp_path: Path) -> None:
    store = ReconciliationStateStore(tmp_path / "state.json")
    profiles = ("first", "second")

    assert store.profiles_requiring_reconciliation(profiles, Availability.ACTIVE) == profiles
    store.record_successes(_batch(profiles, Availability.ACTIVE, failing=frozenset({"second"})))

    assert store.profiles_requiring_reconciliation(profiles, Availability.ACTIVE) == ("second",)
    assert store.profiles_requiring_reconciliation(profiles, Availability.AWAY) == profiles
    assert store.path.stat().st_mode & 0o777 == 0o600


class _FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], Availability | None]] = []

    def run(
        self,
        kvms: tuple[str, ...],
        desired: Availability | None,
    ) -> AvailabilityBatchResult:
        self.calls.append((kvms, desired))
        assert desired is not None
        return _batch(kvms, desired)


class _SequenceService:
    def __init__(self, failures: list[frozenset[str]]) -> None:
        self._failures = failures
        self.calls: list[tuple[tuple[str, ...], Availability | None]] = []

    def run(
        self,
        kvms: tuple[str, ...],
        desired: Availability | None,
    ) -> AvailabilityBatchResult:
        self.calls.append((kvms, desired))
        assert desired is not None
        failing = self._failures.pop(0)
        return _batch(kvms, desired, failing=failing)


def test_periodic_reconcile_recovers_a_missed_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(schedule_cli, "configured_pikvm_profiles", lambda: ("one", "two"))
    store = ReconciliationStateStore(tmp_path / "state.json")
    store.record_successes(_batch(("one", "two"), Availability.AWAY))
    service = _FakeService()
    args = argparse.Namespace(
        schedule_workflow="slack-availability",
        schedule_action="reconcile",
        if_due=True,
    )

    output, exit_code = schedule_cli.execute_schedule_command(
        args,
        service=service,
        state_store=store,
        now=lambda: datetime(2026, 8, 10, 20, 0, tzinfo=KARACHI_TIMEZONE),
    )

    assert exit_code == 0
    assert service.calls == [(("one", "two"), Availability.ACTIVE)]
    assert "Desired Slack availability: active" in output

    second_output, second_exit = schedule_cli.execute_schedule_command(
        args,
        service=service,
        state_store=store,
        now=lambda: datetime(2026, 8, 10, 21, 0, tzinfo=KARACHI_TIMEZONE),
    )
    assert second_exit == 0
    assert "no PiKVM call required" in second_output
    assert len(service.calls) == 1


def test_scheduler_retries_only_failed_profiles_after_five_minutes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(schedule_cli, "configured_pikvm_profiles", lambda: ("one", "two"))
    service = _SequenceService([frozenset({"two"}), frozenset()])
    store = ReconciliationStateStore(tmp_path / "state.json")
    delays: list[float] = []
    args = argparse.Namespace(
        schedule_workflow="slack-availability",
        schedule_action="run-now",
        availability=Availability.ACTIVE,
    )

    output, exit_code = schedule_cli.execute_schedule_command(
        args,
        service=service,
        state_store=store,
        now=lambda: datetime(2026, 8, 10, 20, 0, tzinfo=KARACHI_TIMEZONE),
        sleeper=delays.append,
    )

    assert exit_code == 0
    assert service.calls == [
        (("one", "two"), Availability.ACTIVE),
        (("two",), Availability.ACTIVE),
    ]
    assert delays == [300.0]
    assert "Retry 1 of 2 after 5 minutes:" in output
    assert "Final result:\none  ✓ active" in output
    assert store.profiles_requiring_reconciliation(("one", "two"), Availability.ACTIVE) == ()


def test_scheduler_stops_after_two_failed_retry_rounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(schedule_cli, "configured_pikvm_profiles", lambda: ("one", "two"))
    service = _SequenceService(
        [
            frozenset({"one", "two"}),
            frozenset({"one", "two"}),
            frozenset({"one"}),
        ]
    )
    store = ReconciliationStateStore(tmp_path / "state.json")
    delays: list[float] = []
    args = argparse.Namespace(
        schedule_workflow="slack-availability",
        schedule_action="reconcile",
        if_due=False,
    )

    output, exit_code = schedule_cli.execute_schedule_command(
        args,
        service=service,
        state_store=store,
        now=lambda: datetime(2026, 8, 10, 20, 0, tzinfo=KARACHI_TIMEZONE),
        sleeper=delays.append,
    )

    assert exit_code == 1
    assert service.calls == [
        (("one", "two"), Availability.ACTIVE),
        (("one", "two"), Availability.ACTIVE),
        (("one", "two"), Availability.ACTIVE),
    ]
    assert delays == [300.0, 300.0]
    assert "Retry 2 of 2 after 5 minutes:" in output
    assert "Final result:\none  ✗ verification failed" in output
    assert "two  ✓ active" in output
    assert store.profiles_requiring_reconciliation(("one", "two"), Availability.ACTIVE) == ("one",)


class _LaunchctlRunner:
    def __init__(self) -> None:
        self.loaded: set[str] = set()
        self.calls: list[tuple[str, ...]] = []

    def run(self, arguments: tuple[str, ...]) -> int:
        self.calls.append(arguments)
        action = arguments[1]
        if action == "bootstrap":
            payload = plistlib.loads(Path(arguments[-1]).read_bytes())
            self.loaded.add(payload["Label"])
            return 0
        if action == "bootout":
            path = Path(arguments[-1])
            if path.exists():
                payload = plistlib.loads(path.read_bytes())
                self.loaded.discard(payload["Label"])
            return 0
        if action == "print":
            label = arguments[-1].split("/", 2)[-1]
            return 0 if label in self.loaded else 1
        raise AssertionError(arguments)


def test_launchd_install_status_and_uninstall_are_deterministic(tmp_path: Path) -> None:
    agents = tmp_path / "LaunchAgents"
    logs = tmp_path / "Logs"
    runner = _LaunchctlRunner()
    manager = SlackAvailabilityLaunchdManager(
        launch_agents_dir=agents,
        log_dir=logs,
        python_executable=Path("/usr/bin/python3"),
        working_directory=Path("/private/tmp/pikvm-work-agent"),
        uid=501,
        timezone_name="Asia/Karachi",
        runner=runner,
    )

    installed = manager.install()

    assert len(installed) == 3
    assert all(item.installed and item.loaded for item in installed)
    payloads = {
        plistlib.loads(path.read_bytes())["Label"]: plistlib.loads(path.read_bytes())
        for path in agents.glob("*.plist")
    }
    active = payloads["com.pikvm-work-agent.slack-availability.active"]
    away = payloads["com.pikvm-work-agent.slack-availability.away"]
    reconcile = payloads["com.pikvm-work-agent.slack-availability.reconcile"]
    assert active["StartCalendarInterval"] == [
        {"Weekday": weekday, "Hour": 18, "Minute": 0} for weekday in range(1, 6)
    ]
    assert away["StartCalendarInterval"] == [
        {"Weekday": weekday, "Hour": 2, "Minute": 0} for weekday in range(2, 7)
    ]
    assert active["ProgramArguments"][-2:] == ["--availability", "active"]
    assert away["ProgramArguments"][-2:] == ["--availability", "away"]
    assert reconcile["RunAtLoad"] is True
    assert reconcile["StartInterval"] == 3600
    assert reconcile["ProgramArguments"][-2:] == ["reconcile", "--if-due"]
    assert all(
        payload["EnvironmentVariables"] == {"TZ": "Asia/Karachi"} for payload in payloads.values()
    )
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in agents.glob("*.plist"))

    removed = manager.uninstall()

    assert all(not item.installed and not item.loaded for item in removed)
    assert list(agents.glob("*.plist")) == []


def test_launchd_install_rejects_a_different_mac_timezone(tmp_path: Path) -> None:
    manager = SlackAvailabilityLaunchdManager(
        launch_agents_dir=tmp_path / "LaunchAgents",
        log_dir=tmp_path / "Logs",
        timezone_name="America/New_York",
        runner=_LaunchctlRunner(),
    )

    with pytest.raises(ScheduleError, match="Asia/Karachi"):
        manager.install()
