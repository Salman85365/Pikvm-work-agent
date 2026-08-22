from __future__ import annotations

import argparse
import fcntl
import json
import os
import plistlib
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from work_agent.schedule import cli as schedule_cli
from work_agent.schedule.errors import ScheduleError
from work_agent.schedule.launchd import SlackAvailabilityLaunchdManager
from work_agent.schedule.reconcile import (
    KARACHI_TIMEZONE,
    desired_availability,
    last_transition,
    next_transition,
)
from work_agent.schedule.runlog import failure_streaks, read_failure_streaks, read_outcomes
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


def test_scheduler_retry_re_reads_the_desired_state_after_a_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry that sleeps across 02:00 must apply the state that is now due, not the stale one.

    Before this, a reconcile fired at 01:57 with desired=active that failed once would wake at
    02:02 and set *active* right after the away boundary, leaving the user visibly online for up
    to an hour.
    """
    monkeypatch.setattr(schedule_cli, "configured_pikvm_profiles", lambda: ("one", "two"))
    service = _SequenceService([frozenset({"one"}), frozenset()])
    store = ReconciliationStateStore(tmp_path / "state.json")
    # Monday 01:57 (Tuesday 02:00 boundary is 3 minutes ahead), then after the 5-minute wait.
    moments = iter(
        [
            datetime(2026, 8, 11, 1, 57, tzinfo=KARACHI_TIMEZONE),
            datetime(2026, 8, 11, 2, 2, tzinfo=KARACHI_TIMEZONE),
        ]
    )
    args = argparse.Namespace(
        schedule_workflow="slack-availability",
        schedule_action="reconcile",
        if_due=False,
    )

    output, exit_code = schedule_cli.execute_schedule_command(
        args,
        service=service,
        state_store=store,
        now=lambda: next(moments),
        sleeper=lambda _: None,
    )

    assert exit_code == 0
    assert service.calls == [
        (("one", "two"), Availability.ACTIVE),
        (("one", "two"), Availability.AWAY),
    ]
    assert "Desired state changed to away during the wait" in output
    assert output.startswith("Desired Slack availability: away")


def test_forced_run_now_never_re_reads_the_clock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(schedule_cli, "configured_pikvm_profiles", lambda: ("one",))
    service = _SequenceService([frozenset({"one"}), frozenset()])
    store = ReconciliationStateStore(tmp_path / "state.json")
    args = argparse.Namespace(
        schedule_workflow="slack-availability",
        schedule_action="run-now",
        availability=Availability.ACTIVE,
    )

    _, exit_code = schedule_cli.execute_schedule_command(
        args,
        service=service,
        state_store=store,
        now=lambda: datetime(2026, 8, 11, 2, 2, tzinfo=KARACHI_TIMEZONE),
        sleeper=lambda _: None,
    )

    assert exit_code == 0
    assert [desired for _, desired in service.calls] == [Availability.ACTIVE, Availability.ACTIVE]


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


class _FakeProbe:
    def __init__(self, *, importable: bool = True) -> None:
        self.importable = importable
        self.calls: list[tuple[Path, Path]] = []

    def can_import_work_agent(self, python: Path, working_directory: Path) -> bool:
        self.calls.append((python, working_directory))
        return self.importable


def _manager(
    tmp_path: Path,
    *,
    runner: _LaunchctlRunner | None = None,
    probe: _FakeProbe | None = None,
    timezone_name: str = "Asia/Karachi",
) -> SlackAvailabilityLaunchdManager:
    return SlackAvailabilityLaunchdManager(
        launch_agents_dir=tmp_path / "LaunchAgents",
        log_dir=tmp_path / "Logs",
        python_executable=Path("/usr/bin/python3"),
        working_directory=Path("/private/tmp/pikvm-work-agent"),
        uid=501,
        timezone_name=timezone_name,
        runner=runner or _LaunchctlRunner(),
        interpreter_probe=probe or _FakeProbe(),
    )


def test_launchd_install_status_and_uninstall_are_deterministic(tmp_path: Path) -> None:
    agents = tmp_path / "LaunchAgents"
    runner = _LaunchctlRunner()
    manager = _manager(tmp_path, runner=runner)

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
    # Calendar agents only supply timing; a replayed-late trigger must not force a stale state.
    assert active["ProgramArguments"][-2:] == ["reconcile", "--if-due"]
    assert away["ProgramArguments"][-2:] == ["reconcile", "--if-due"]
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


def test_launchd_install_preserves_virtualenv_interpreter_symlink(tmp_path: Path) -> None:
    base_python = tmp_path / "python3.14"
    base_python.touch()
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(base_python)
    working_directory = tmp_path / "project"
    working_directory.mkdir()
    probe = _FakeProbe()
    manager = SlackAvailabilityLaunchdManager(
        launch_agents_dir=tmp_path / "LaunchAgents",
        log_dir=tmp_path / "Logs",
        python_executable=venv_python,
        working_directory=working_directory,
        uid=501,
        timezone_name="Asia/Karachi",
        runner=_LaunchctlRunner(),
        interpreter_probe=probe,
    )

    manager.install()

    assert probe.calls == [(venv_python, working_directory)]
    payloads = [
        plistlib.loads(path.read_bytes()) for path in (tmp_path / "LaunchAgents").glob("*.plist")
    ]
    assert len(payloads) == 3
    assert all(payload["ProgramArguments"][0] == str(venv_python) for payload in payloads)
    assert all(payload["ProgramArguments"][0] != str(base_python) for payload in payloads)


def test_launchd_install_rejects_a_different_mac_timezone(tmp_path: Path) -> None:
    manager = _manager(tmp_path, timezone_name="America/New_York")

    with pytest.raises(ScheduleError, match="Asia/Karachi"):
        manager.install()


def test_launchd_install_rejects_an_interpreter_without_the_project(tmp_path: Path) -> None:
    probe = _FakeProbe(importable=False)
    manager = _manager(tmp_path, probe=probe)

    with pytest.raises(ScheduleError, match="cannot import work_agent"):
        manager.install()

    assert probe.calls == [(Path("/usr/bin/python3"), Path("/private/tmp/pikvm-work-agent"))]
    assert list((tmp_path / "LaunchAgents").glob("*.plist")) == []


def test_launchd_health_reports_an_installed_but_unrunnable_interpreter(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.install()

    healthy = manager.health()
    assert healthy.healthy
    assert healthy.interpreter == Path("/usr/bin/python3")
    assert healthy.interpreter_can_run

    broken = _manager(tmp_path, probe=_FakeProbe(importable=False)).health()
    assert not broken.healthy
    assert not broken.interpreter_can_run
    assert any("cannot import work_agent" in problem for problem in broken.problems)


def test_launchd_health_without_installed_agents_reports_nothing_recorded(tmp_path: Path) -> None:
    health = _manager(tmp_path).health()

    assert health.interpreter is None
    assert not health.interpreter_can_run
    assert health.timezone_ok


def test_schedule_status_exit_code_follows_health(tmp_path: Path) -> None:
    runner = _LaunchctlRunner()
    _manager(tmp_path, runner=runner).install()
    args = argparse.Namespace(
        schedule_workflow="slack-availability",
        schedule_action="status",
    )

    output, exit_code = schedule_cli.execute_schedule_command(
        args,
        manager=_manager(tmp_path, runner=runner, probe=_FakeProbe(importable=False)),
    )

    assert exit_code == 1
    assert "runnable=no" in output
    assert "! " in output


@pytest.mark.parametrize(
    ("moment", "expected_at", "expected_state"),
    [
        (
            datetime(2026, 8, 11, 9, 0, tzinfo=KARACHI_TIMEZONE),
            datetime(2026, 8, 11, 18, 0, tzinfo=KARACHI_TIMEZONE),
            Availability.ACTIVE,
        ),
        (
            datetime(2026, 8, 11, 19, 0, tzinfo=KARACHI_TIMEZONE),
            datetime(2026, 8, 12, 2, 0, tzinfo=KARACHI_TIMEZONE),
            Availability.AWAY,
        ),
        (
            # Saturday 03:00 — next boundary is Monday 18:00, not Sunday.
            datetime(2026, 8, 15, 3, 0, tzinfo=KARACHI_TIMEZONE),
            datetime(2026, 8, 17, 18, 0, tzinfo=KARACHI_TIMEZONE),
            Availability.ACTIVE,
        ),
    ],
)
def test_next_transition_matches_the_desired_state_function(
    moment: datetime,
    expected_at: datetime,
    expected_state: Availability,
) -> None:
    boundary, state = next_transition(moment)

    assert boundary == expected_at
    assert state is expected_state
    assert desired_availability(boundary) is expected_state
    assert desired_availability(boundary - timedelta(minutes=1)) is not expected_state


def test_next_transition_rejects_naive_time() -> None:
    with pytest.raises(ScheduleError, match="timezone-aware"):
        next_transition(datetime(2026, 8, 11, 9, 0))


def test_applied_state_round_trips_without_exposing_partial_writes(tmp_path: Path) -> None:
    store = ReconciliationStateStore(tmp_path / "state.json")

    assert store.applied_state() == ({}, None)

    store.record_successes(_batch(("one", "two"), Availability.ACTIVE))
    applied, updated_at = store.applied_state()

    assert applied == {"one": "active", "two": "active"}
    assert updated_at is not None

    store.path.write_text("{not json", encoding="utf-8")
    assert store.applied_state() == ({}, None)


def test_profile_states_keep_independent_verification_timestamps(tmp_path: Path) -> None:
    first_verification = datetime(2026, 8, 11, 8, 0, tzinfo=UTC)
    second_verification = datetime(2026, 8, 11, 9, 30, tzinfo=UTC)
    moments = iter((first_verification, second_verification))
    store = ReconciliationStateStore(tmp_path / "state.json", now=lambda: next(moments))

    store.record_successes(_batch(("one", "two"), Availability.ACTIVE))
    store.record_successes(_batch(("one", "two"), Availability.AWAY, failing=frozenset({"two"})))

    profiles = store.profile_states()
    assert profiles["one"].availability == "away"
    assert profiles["one"].verified_at == second_verification
    assert profiles["two"].availability == "active"
    assert profiles["two"].verified_at == first_verification
    assert store.applied_state() == (
        {"one": "away", "two": "active"},
        second_verification,
    )


def test_profile_states_read_legacy_state_and_migrate_without_losing_profiles(
    tmp_path: Path,
) -> None:
    legacy_verification = datetime(2026, 8, 10, 20, 0, tzinfo=UTC)
    next_verification = datetime(2026, 8, 11, 7, 0, tzinfo=UTC)
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "updated_at": legacy_verification.isoformat(),
                "applied": {"one": "active", "two": "away"},
            }
        ),
        encoding="utf-8",
    )
    store = ReconciliationStateStore(path, now=lambda: next_verification)

    legacy_profiles = store.profile_states()
    assert legacy_profiles["one"].verified_at == legacy_verification
    assert legacy_profiles["two"].verified_at == legacy_verification

    store.record_successes(_batch(("one",), Availability.AWAY))

    assert store.applied_state() == (
        {"one": "away", "two": "away"},
        next_verification,
    )
    migrated_profiles = store.profile_states()
    assert migrated_profiles["one"].verified_at == next_verification
    assert migrated_profiles["two"].verified_at == legacy_verification
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["verified_at"] == {
        "one": next_verification.isoformat(),
        "two": legacy_verification.isoformat(),
    }


@pytest.mark.parametrize(
    "result",
    [
        AvailabilityBatchResult(results=()),
        AvailabilityBatchResult(
            results=(
                AvailabilityResult(
                    kvm="one",
                    desired=Availability.ACTIVE,
                    observed=None,
                    changed=None,
                    success=False,
                    error="verification failed",
                ),
            )
        ),
        AvailabilityBatchResult(
            results=(
                AvailabilityResult(
                    kvm="one",
                    desired=Availability.ACTIVE,
                    observed=Availability.AWAY,
                    changed=True,
                    success=True,
                ),
            )
        ),
    ],
)
def test_record_successes_does_not_create_state_without_verified_match(
    tmp_path: Path,
    result: AvailabilityBatchResult,
) -> None:
    def unexpected_clock_call() -> datetime:
        raise AssertionError("an unsuccessful batch must not obtain a new timestamp")

    store = ReconciliationStateStore(tmp_path / "state.json", now=unexpected_clock_call)

    store.record_successes(result)

    assert not store.path.exists()
    assert store.applied_state() == ({}, None)
    assert store.profile_states() == {}


def test_failed_batch_does_not_advance_or_rewrite_existing_state(tmp_path: Path) -> None:
    verified_at = datetime(2026, 8, 11, 8, 0, tzinfo=UTC)
    path = tmp_path / "state.json"
    store = ReconciliationStateStore(path, now=lambda: verified_at)
    store.record_successes(_batch(("one",), Availability.ACTIVE))
    original_payload = path.read_bytes()
    original_modified_at = path.stat().st_mtime_ns

    failed_store = ReconciliationStateStore(
        path,
        now=lambda: (_ for _ in ()).throw(
            AssertionError("an unsuccessful batch must not obtain a new timestamp")
        ),
    )
    failed_store.record_successes(_batch(("one",), Availability.AWAY, failing=frozenset({"one"})))

    assert path.read_bytes() == original_payload
    assert path.stat().st_mtime_ns == original_modified_at
    assert failed_store.applied_state() == ({"one": "active"}, verified_at)
    assert failed_store.profile_states()["one"].verified_at == verified_at


def test_profile_states_tolerate_invalid_new_timestamp_data(tmp_path: Path) -> None:
    legacy_verification = datetime(2026, 8, 10, 20, 0, tzinfo=UTC)
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "updated_at": legacy_verification.isoformat(),
                "applied": {"one": "active", "two": "away"},
                "verified_at": {"one": "not-a-time", "two": 42},
            }
        ),
        encoding="utf-8",
    )

    profiles = ReconciliationStateStore(path).profile_states()

    assert profiles["one"].verified_at == legacy_verification
    assert profiles["two"].verified_at == legacy_verification


def test_record_successes_waits_for_cross_process_lock(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = ReconciliationStateStore(path)
    store.record_successes(_batch(("one",), Availability.ACTIVE))
    lock_path = path.with_name(f"{path.name}.lock")
    lock_descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
    script = """
import sys
from datetime import UTC, datetime
from pathlib import Path

from work_agent.schedule.state import ReconciliationStateStore
from work_agent.slack.models import Availability, AvailabilityBatchResult, AvailabilityResult

result = AvailabilityBatchResult(
    results=(
        AvailabilityResult(
            kvm="two",
            desired=Availability.AWAY,
            observed=Availability.AWAY,
            changed=False,
            success=True,
        ),
    )
)
print("ready", flush=True)
ReconciliationStateStore(
    Path(sys.argv[1]),
    now=lambda: datetime(2026, 8, 11, 10, 0, tzinfo=UTC),
).record_successes(result)
print("done", flush=True)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    ready = process.stdout.readline()
    blocked = False
    state_while_locked: tuple[dict[str, str], datetime | None] | None = None
    try:
        try:
            process.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            blocked = True
        state_while_locked = store.applied_state()
    finally:
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)

    stdout, stderr = process.communicate(timeout=5)

    assert ready == "ready\n"
    assert blocked
    assert state_while_locked is not None
    assert state_while_locked[0] == {"one": "active"}
    assert process.returncode == 0, stderr
    assert stdout == "done\n"
    assert store.applied_state()[0] == {"one": "active", "two": "away"}
    assert lock_path.stat().st_mode & 0o777 == 0o600


def test_failed_state_replace_cleans_unique_private_temporary_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "state.json"
    temporary_files: list[tuple[Path, int]] = []

    def fail_replace(source: os.PathLike[str], destination: os.PathLike[str]) -> None:
        temporary = Path(source)
        assert Path(destination) == path
        temporary_files.append((temporary, temporary.stat().st_mode & 0o777))
        raise OSError("simulated atomic replace failure")

    monkeypatch.setattr("work_agent.schedule.state.os.replace", fail_replace)
    store = ReconciliationStateStore(path)

    for desired in (Availability.ACTIVE, Availability.AWAY):
        with pytest.raises(ScheduleError, match="could not be written"):
            store.record_successes(_batch(("one",), desired))

    temporary_paths = [temporary for temporary, _mode in temporary_files]
    assert len(temporary_paths) == 2
    assert len(set(temporary_paths)) == 2
    assert all(temporary.parent == tmp_path for temporary in temporary_paths)
    assert all(mode == 0o600 for _temporary, mode in temporary_files)
    assert all(not temporary.exists() for temporary in temporary_paths)
    assert list(tmp_path.glob(f".{path.name}.*.tmp")) == []
    assert not path.exists()


# --------------------------------------------------------------------------------------------
# Boundary expiry, profile pruning, and lock timeouts in the reconciliation state
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("moment", "expected_at", "expected_state"),
    [
        (
            datetime(2026, 8, 11, 9, 0, tzinfo=KARACHI_TIMEZONE),
            datetime(2026, 8, 11, 2, 0, tzinfo=KARACHI_TIMEZONE),
            Availability.AWAY,
        ),
        (
            datetime(2026, 8, 11, 19, 0, tzinfo=KARACHI_TIMEZONE),
            datetime(2026, 8, 11, 18, 0, tzinfo=KARACHI_TIMEZONE),
            Availability.ACTIVE,
        ),
        (
            # Sunday: the last boundary was Saturday 02:00 (away); no Sunday boundaries exist.
            datetime(2026, 8, 16, 20, 0, tzinfo=KARACHI_TIMEZONE),
            datetime(2026, 8, 15, 2, 0, tzinfo=KARACHI_TIMEZONE),
            Availability.AWAY,
        ),
        (
            # Exactly on the boundary counts as that boundary.
            datetime(2026, 8, 10, 18, 0, tzinfo=KARACHI_TIMEZONE),
            datetime(2026, 8, 10, 18, 0, tzinfo=KARACHI_TIMEZONE),
            Availability.ACTIVE,
        ),
    ],
)
def test_last_transition_matches_the_desired_state_function(
    moment: datetime,
    expected_at: datetime,
    expected_state: Availability,
) -> None:
    boundary, state = last_transition(moment)

    assert boundary == expected_at
    assert state is expected_state
    assert desired_availability(moment) is state


def test_applied_state_verified_before_the_boundary_is_due_again(tmp_path: Path) -> None:
    """A run-now at 17:00 that set active must not satisfy the 18:00 active boundary."""

    verified = datetime(2026, 8, 10, 17, 0, tzinfo=KARACHI_TIMEZONE)
    store = ReconciliationStateStore(tmp_path / "state.json", now=lambda: verified)
    store.record_successes(_batch(("one", "two"), Availability.ACTIVE))
    boundary = datetime(2026, 8, 10, 18, 0, tzinfo=KARACHI_TIMEZONE)

    assert store.profiles_requiring_reconciliation(("one", "two"), Availability.ACTIVE) == ()
    assert store.profiles_requiring_reconciliation(
        ("one", "two"), Availability.ACTIVE, verified_after=boundary
    ) == ("one", "two")

    later = ReconciliationStateStore(
        tmp_path / "state.json",
        now=lambda: boundary + timedelta(minutes=5),
    )
    later.record_successes(_batch(("one",), Availability.ACTIVE))
    assert later.profiles_requiring_reconciliation(
        ("one", "two"), Availability.ACTIVE, verified_after=boundary
    ) == ("two",)


def test_if_due_reconcile_re_verifies_state_recorded_before_the_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(schedule_cli, "configured_pikvm_profiles", lambda: ("one",))
    verified = datetime(2026, 8, 10, 17, 0, tzinfo=KARACHI_TIMEZONE)
    store = ReconciliationStateStore(tmp_path / "state.json", now=lambda: verified)
    store.record_successes(_batch(("one",), Availability.ACTIVE))
    service = _FakeService()
    args = argparse.Namespace(
        schedule_workflow="slack-availability",
        schedule_action="reconcile",
        if_due=True,
    )

    _, exit_code = schedule_cli.execute_schedule_command(
        args,
        service=service,
        state_store=store,
        now=lambda: datetime(2026, 8, 10, 18, 30, tzinfo=KARACHI_TIMEZONE),
        notifier=lambda title, text: None,
        run_log_path=tmp_path / "missing.jsonl",
    )

    assert exit_code == 0
    assert service.calls == [(("one",), Availability.ACTIVE)]


def test_record_successes_drops_profiles_no_longer_configured(tmp_path: Path) -> None:
    store = ReconciliationStateStore(tmp_path / "state.json")
    store.record_successes(_batch(("old", "kept"), Availability.AWAY))

    store.record_successes(_batch(("kept",), Availability.ACTIVE), known_profiles=("kept", "new"))

    assert store.applied_state()[0] == {"kept": "active"}
    assert set(store.profile_states()) == {"kept"}

    # Pruning alone (no successes) still rewrites the file.
    store.record_successes(
        _batch(("kept",), Availability.ACTIVE, failing=frozenset({"kept"})),
        known_profiles=("new",),
    )
    assert store.applied_state()[0] == {}


def test_state_lock_wait_gives_up_instead_of_hanging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("work_agent.schedule.state._LOCK_WAIT_SECONDS", 0.2)
    path = tmp_path / "state.json"
    store = ReconciliationStateStore(path)
    lock_path = path.with_name(f"{path.name}.lock")
    tmp_path.chmod(0o700)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    try:
        with pytest.raises(ScheduleError, match="locked by another process"):
            store.record_successes(_batch(("one",), Availability.ACTIVE))
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    assert not path.exists()


# --------------------------------------------------------------------------------------------
# launchd: subprocess timeouts and non-destructive reinstall
# --------------------------------------------------------------------------------------------


def test_launchctl_runner_reports_a_hung_launchctl(monkeypatch: pytest.MonkeyPatch) -> None:
    from work_agent.schedule import launchd

    def hang(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="launchctl", timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(launchd.subprocess, "run", hang)

    with pytest.raises(ScheduleError, match="did not finish"):
        launchd.SubprocessCommandRunner().run(("launchctl", "print", "gui/501/x"))
    assert (
        launchd.SubprocessInterpreterProbe().can_import_work_agent(Path(sys.executable), Path.cwd())
        is False
    )


def test_reinstall_leaves_unchanged_loaded_agents_alone(tmp_path: Path) -> None:
    runner = _LaunchctlRunner()
    manager = _manager(tmp_path, runner=runner)
    manager.install()
    first_notes = manager.last_install_notes
    assert any("RunAtLoad" in note for note in first_notes)
    runner.calls.clear()

    manager.install()

    actions = {call[1] for call in runner.calls}
    assert "bootout" not in actions
    assert "bootstrap" not in actions
    assert len(manager.last_install_notes) == 3
    assert all("left running" in note for note in manager.last_install_notes)


def test_reinstall_reloads_only_agents_whose_plist_changed(tmp_path: Path) -> None:
    runner = _LaunchctlRunner()
    _manager(tmp_path, runner=runner).install()
    runner.calls.clear()

    changed = _manager(tmp_path, runner=runner)
    # Simulate an edited (stale) reconcile plist on disk.
    reconcile_plist = (
        tmp_path / "LaunchAgents" / ("com.pikvm-work-agent.slack-availability.reconcile.plist")
    )
    payload = plistlib.loads(reconcile_plist.read_bytes())
    payload["StartInterval"] = 60
    reconcile_plist.write_bytes(plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True))

    changed.install()

    reloaded = [call for call in runner.calls if call[1] in {"bootout", "bootstrap"}]
    assert all(call[-1].endswith("reconcile.plist") for call in reloaded)
    assert {call[1] for call in reloaded} == {"bootout", "bootstrap"}
    assert plistlib.loads(reconcile_plist.read_bytes())["StartInterval"] == 3600
    assert any("RunAtLoad" in note for note in changed.last_install_notes)

    args = argparse.Namespace(schedule_workflow="slack-availability", schedule_action="install")
    output, _ = schedule_cli.execute_schedule_command(args, manager=changed)
    assert "left running" in output


# --------------------------------------------------------------------------------------------
# Failure streaks from the run log, retry cancellation, and macOS notifications
# --------------------------------------------------------------------------------------------


def _log_entry(
    kvm: str,
    *,
    minutes_ago: int,
    outcome: str,
    stop_code: str | None = None,
    error: str | None = None,
) -> str:
    moment = datetime(2026, 8, 19, 12, 0, tzinfo=UTC) - timedelta(minutes=minutes_ago)
    return json.dumps(
        {
            "timestamp": moment.isoformat(),
            "kvm": kvm,
            "desired_availability": "away",
            "observed_availability": "unknown" if outcome == "failure" else "away",
            "changed": None,
            "outcome": outcome,
            "stop_code": stop_code,
            "error": error,
        }
    )


def test_failure_streaks_skip_lock_busy_and_find_the_first_unreachable_run(
    tmp_path: Path,
) -> None:
    log = tmp_path / "log.jsonl"
    log.write_text(
        "\n".join(
            [
                _log_entry("heidrick", minutes_ago=300, outcome="success"),
                _log_entry("heidrick", minutes_ago=240, outcome="failure", stop_code="stuck"),
                _log_entry(
                    "heidrick",
                    minutes_ago=180,
                    outcome="failure",
                    stop_code="pikvm_unreachable",
                    error="connect timeout",
                ),
                _log_entry("heidrick", minutes_ago=120, outcome="failure", stop_code="lock_busy"),
                _log_entry(
                    "heidrick",
                    minutes_ago=60,
                    outcome="failure",
                    stop_code="pikvm_unreachable",
                    error="connect timeout",
                ),
                _log_entry("nbc_kvm", minutes_ago=90, outcome="failure", stop_code="lock_busy"),
                _log_entry("nbc_kvm", minutes_ago=30, outcome="success"),
                "{ broken",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    streaks = read_failure_streaks(log)

    heidrick = streaks["heidrick"]
    assert heidrick.count == 3
    assert heidrick.last_stop_code == "pikvm_unreachable"
    assert heidrick.unreachable_since == datetime(2026, 8, 19, 9, 0, tzinfo=UTC)
    assert heidrick.first_at == datetime(2026, 8, 19, 8, 0, tzinfo=UTC)
    assert heidrick.latest_success is False
    nbc = streaks["nbc_kvm"]
    assert nbc.count == 0
    assert nbc.latest_success is True
    assert len(read_outcomes(log)) == 7
    assert failure_streaks([]) == {}
    assert read_failure_streaks(tmp_path / "absent.jsonl") == {}


def test_notification_names_kvm_and_reason_category_only(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    log.write_text(
        "\n".join(
            _log_entry("nbc_kvm", minutes_ago=m, outcome="failure", stop_code="stuck")
            for m in (180, 120, 60)
        )
        + "\n",
        encoding="utf-8",
    )
    batch = AvailabilityBatchResult(
        results=(
            AvailabilityResult(
                kvm="heidrick",
                desired=Availability.AWAY,
                observed=None,
                changed=None,
                success=False,
                error="secret-looking transport detail https://user:pw@host",
                stop_code="pikvm_unreachable",
            ),
            AvailabilityResult(
                kvm="nbc_kvm",
                desired=Availability.AWAY,
                observed=None,
                changed=None,
                success=False,
                error="stuck",
                stop_code="stuck_no_screen_change",
            ),
        )
    )

    text = schedule_cli.failure_notification_text(
        batch, profiles=("heidrick", "nbc_kvm"), run_log_path=log
    )

    assert text == ("heidrick: PiKVM unreachable; nbc_kvm: failed 3 scheduled runs in a row")
    assert "https://" not in text

    healthy = AvailabilityBatchResult(results=(_batch(("heidrick",), Availability.AWAY).results))
    assert (
        schedule_cli.failure_notification_text(
            healthy, profiles=("heidrick",), run_log_path=tmp_path / "none.jsonl"
        )
        is None
    )


def test_scheduled_reconcile_posts_a_notification_only_when_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(schedule_cli, "configured_pikvm_profiles", lambda: ("one",))
    monkeypatch.delenv(schedule_cli.NOTIFICATIONS_ENV, raising=False)

    class _UnreachableService:
        def run(
            self,
            kvms: tuple[str, ...],
            desired: Availability | None,
        ) -> AvailabilityBatchResult:
            assert desired is not None
            return AvailabilityBatchResult(
                results=tuple(
                    AvailabilityResult(
                        kvm=kvm,
                        desired=desired,
                        observed=None,
                        changed=None,
                        success=False,
                        error="connect timeout",
                        stop_code="pikvm_unreachable",
                    )
                    for kvm in kvms
                )
            )

    posted: list[tuple[str, str]] = []

    def run(if_due: bool, *, no_notify: bool = False) -> int:
        args = argparse.Namespace(
            schedule_workflow="slack-availability",
            schedule_action="reconcile",
            if_due=if_due,
            no_notify=no_notify,
        )
        _, code = schedule_cli.execute_schedule_command(
            args,
            service=_UnreachableService(),
            state_store=ReconciliationStateStore(tmp_path / "state.json"),
            now=lambda: datetime(2026, 8, 10, 20, 0, tzinfo=KARACHI_TIMEZONE),
            sleeper=lambda _: None,
            notifier=lambda title, text: posted.append((title, text)),
            run_log_path=tmp_path / "log.jsonl",
        )
        return code

    assert run(True) == 1
    assert posted == [("PiKVM Work Agent", "one: PiKVM unreachable")]

    posted.clear()
    assert run(False) == 1
    assert posted == []
    assert run(True, no_notify=True) == 1
    assert posted == []
    monkeypatch.setenv(schedule_cli.NOTIFICATIONS_ENV, "off")
    assert run(True) == 1
    assert posted == []


def test_notification_failures_never_fail_the_scheduled_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(schedule_cli, "configured_pikvm_profiles", lambda: ("one",))
    monkeypatch.delenv(schedule_cli.NOTIFICATIONS_ENV, raising=False)
    service = _SequenceService([frozenset({"one"})] * 3)
    args = argparse.Namespace(
        schedule_workflow="slack-availability",
        schedule_action="reconcile",
        if_due=True,
    )
    log = tmp_path / "log.jsonl"
    log.write_text(
        "\n".join(
            _log_entry("one", minutes_ago=m, outcome="failure", stop_code="stuck")
            for m in (3, 2, 1)
        )
        + "\n",
        encoding="utf-8",
    )
    calls: list[str] = []

    def explode(title: str, text: str) -> None:
        calls.append(text)
        raise subprocess.TimeoutExpired(cmd="osascript", timeout=5)

    _, code = schedule_cli.execute_schedule_command(
        args,
        service=service,
        state_store=ReconciliationStateStore(tmp_path / "state.json"),
        now=lambda: datetime(2026, 8, 10, 20, 0, tzinfo=KARACHI_TIMEZONE),
        sleeper=lambda _: None,
        notifier=explode,
        run_log_path=log,
    )

    assert code == 1
    assert calls == ["one: failed 3 scheduled runs in a row"]


def test_macos_notification_command_is_quoted_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(arguments: tuple[str, ...], **kwargs: object) -> None:
        captured["arguments"] = arguments
        captured["timeout"] = kwargs.get("timeout")

    monkeypatch.setattr(schedule_cli.subprocess, "run", fake_run)

    schedule_cli._display_macos_notification("Title", 'kvm "x": bad \\ thing')

    arguments = captured["arguments"]
    assert isinstance(arguments, tuple)
    assert arguments[:2] == ("osascript", "-e")
    assert arguments[2] == ('display notification "kvm \\"x\\": bad \\\\ thing" with title "Title"')
    assert captured["timeout"] == 5.0


def test_a_cancelled_retry_wait_keeps_the_recorded_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(schedule_cli, "configured_pikvm_profiles", lambda: ("one", "two"))
    service = _SequenceService([frozenset({"two"}), frozenset()])
    store = ReconciliationStateStore(tmp_path / "state.json")
    args = argparse.Namespace(
        schedule_workflow="slack-availability",
        schedule_action="run-now",
        availability=Availability.ACTIVE,
    )

    def interrupted(_: float) -> None:
        raise schedule_cli.RetryWaitInterrupted

    output, exit_code = schedule_cli.execute_schedule_command(
        args,
        service=service,
        state_store=store,
        now=lambda: datetime(2026, 8, 10, 20, 0, tzinfo=KARACHI_TIMEZONE),
        sleeper=interrupted,
    )

    assert exit_code == 1
    assert service.calls == [(("one", "two"), Availability.ACTIVE)]
    assert "Cancelled during the retry wait" in output
    assert "Final result:\none  ✓ active" in output
    assert "two  ✗ verification failed" in output
    assert store.profiles_requiring_reconciliation(("one", "two"), Availability.ACTIVE) == ("two",)
