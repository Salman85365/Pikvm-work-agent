from __future__ import annotations

import fcntl
import json
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from work_agent.agenda.models import (
    AgendaBatchResult,
    AgendaReport,
    CalendarSurface,
    MeetingItem,
    MeetingStatus,
)
from work_agent.agent.lock import ControllerLock
from work_agent.dashboard import fleet, operations
from work_agent.dashboard.app import TOKEN_HEADER, create_app
from work_agent.dashboard.jobs import JobOutcome
from work_agent.dashboard.models import (
    JobKind,
    JobResultLine,
    JobStatus,
    KvmProfile,
    ScheduleAction,
)
from work_agent.schedule.cli import RetryWaitInterrupted
from work_agent.schedule.launchd import LaunchAgentStatus, ScheduleHealth
from work_agent.schedule.state import ReconciliationStateStore
from work_agent.slack.models import Availability, AvailabilityBatchResult, AvailabilityResult
from work_agent.slack.triage_models import TriageBatchResult, TriageReport

TOKEN = "test-dashboard-token"
_LABEL = "com.pikvm-work-agent.slack-availability."


class _FakeManager:
    def __init__(
        self,
        *,
        installed: bool = True,
        loaded: bool = True,
        interpreter: Path | None = Path("/venv/bin/python"),
        can_run: bool = True,
        timezone_name: str = "Asia/Karachi",
    ) -> None:
        self._installed = installed
        self._loaded = loaded
        self._interpreter = interpreter
        self._can_run = can_run
        self._timezone = timezone_name
        self.calls: list[str] = []

    def status(self) -> tuple[LaunchAgentStatus, ...]:
        self.calls.append("status")
        return tuple(
            LaunchAgentStatus(
                label=f"{_LABEL}{name}",
                path=Path(f"/agents/{name}.plist"),
                installed=self._installed,
                loaded=self._loaded,
            )
            for name in ("active", "away", "reconcile")
        )

    def health(self) -> ScheduleHealth:
        self.calls.append("health")
        problems: tuple[str, ...] = ()
        if self._interpreter is not None and not self._can_run:
            problems = (f"The installed agents run {self._interpreter}, which cannot import.",)
        return ScheduleHealth(
            interpreter=self._interpreter,
            working_directory=Path("/repo"),
            interpreter_can_run=self._can_run,
            timezone_name=self._timezone,
            timezone_ok=self._timezone == "Asia/Karachi",
            problems=problems,
        )


def _snapshot(manager: _FakeManager, store: ReconciliationStateStore) -> object:
    return operations.schedule_snapshot(
        manager=manager,  # type: ignore[arg-type]
        state_store=store,
        now=datetime(2026, 8, 11, 9, 0, tzinfo=UTC),
    )


def test_schedule_snapshot_reports_a_healthy_installation(tmp_path: Path) -> None:
    store = ReconciliationStateStore(tmp_path / "state.json")
    store.record_successes(
        AvailabilityBatchResult(
            results=(
                AvailabilityResult(
                    kvm="heidrick",
                    desired=Availability.AWAY,
                    observed=Availability.AWAY,
                    changed=False,
                    success=True,
                ),
            )
        )
    )

    snapshot = _snapshot(_FakeManager(), store)

    assert snapshot.healthy is True
    assert snapshot.installed is True
    assert snapshot.problems == []
    assert [agent.short_label for agent in snapshot.agents] == ["active", "away", "reconcile"]
    assert snapshot.applied == {"heidrick": "away"}
    assert set(snapshot.applied_verified_at) == {"heidrick"}
    assert snapshot.applied_updated_at is not None
    # 09:00 UTC on a Tuesday is 14:00 Karachi: away until the 18:00 boundary.
    assert snapshot.desired_now is Availability.AWAY
    assert snapshot.next_transition_to is Availability.ACTIVE
    assert snapshot.next_transition_at.hour == 18


def test_schedule_snapshot_flags_a_wrong_interpreter(tmp_path: Path) -> None:
    snapshot = _snapshot(
        _FakeManager(interpreter=Path("/usr/bin/python3.14"), can_run=False),
        ReconciliationStateStore(tmp_path / "state.json"),
    )

    assert snapshot.healthy is False
    assert snapshot.interpreter_can_run is False
    assert any("cannot import" in problem for problem in snapshot.problems)


def test_schedule_snapshot_flags_a_missing_installation(tmp_path: Path) -> None:
    snapshot = _snapshot(
        _FakeManager(installed=False, loaded=False, interpreter=None),
        ReconciliationStateStore(tmp_path / "state.json"),
    )

    assert snapshot.installed is False
    assert snapshot.healthy is False
    assert any("not installed" in problem for problem in snapshot.problems)


def test_schedule_snapshot_flags_an_unloaded_agent(tmp_path: Path) -> None:
    snapshot = _snapshot(
        _FakeManager(loaded=False),
        ReconciliationStateStore(tmp_path / "state.json"),
    )

    assert snapshot.installed is True
    assert snapshot.healthy is False
    assert any("not loaded" in problem for problem in snapshot.problems)


def test_availability_work_streams_per_kvm_text(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Service:
        def run(
            self,
            kvms: tuple[str, ...],
            desired: Availability | None,
        ) -> AvailabilityBatchResult:
            return AvailabilityBatchResult(
                results=(
                    AvailabilityResult(
                        kvm="heidrick",
                        desired=desired,
                        observed=Availability.ACTIVE,
                        changed=True,
                        success=True,
                    ),
                    AvailabilityResult(
                        kvm="nbc_kvm",
                        desired=desired,
                        observed=None,
                        changed=None,
                        success=False,
                        error="verification failed",
                        log_error="log not writable",
                    ),
                )
            )

    monkeypatch.setattr(
        operations,
        "default_slack_availability_service",
        lambda *, trace_output=None: _Service(),
    )

    events: list[str] = []
    outcome = operations.availability_work(("heidrick", "nbc_kvm"), Availability.ACTIVE)(
        events.append
    )

    assert outcome.ok is False
    assert outcome.summary == "Applied active to 1 of 2 KVM(s)."
    assert [(item.kvm, item.ok, item.text) for item in outcome.results] == [
        ("heidrick", True, "changed to active"),
        ("nbc_kvm", False, "verification failed"),
    ]
    assert events == ["nbc_kvm  ! log not writable"]


def test_availability_work_reports_a_no_op_distinctly(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Service:
        def run(
            self,
            kvms: tuple[str, ...],
            desired: Availability | None,
        ) -> AvailabilityBatchResult:
            return AvailabilityBatchResult(
                results=(
                    AvailabilityResult(
                        kvm="heidrick",
                        desired=desired,
                        observed=Availability.AWAY,
                        changed=False,
                        success=True,
                    ),
                )
            )

    monkeypatch.setattr(
        operations,
        "default_slack_availability_service",
        lambda *, trace_output=None: _Service(),
    )

    outcome = operations.availability_work(("heidrick",), Availability.AWAY)(lambda _: None)

    assert outcome.ok is True
    assert outcome.results[0].text == "already away; no click sent"


def test_availability_work_includes_preflight_failures_in_its_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Service:
        def run(
            self,
            kvms: tuple[str, ...],
            desired: Availability | None,
        ) -> AvailabilityBatchResult:
            return AvailabilityBatchResult(
                results=(
                    AvailabilityResult(
                        kvm="ready",
                        desired=desired,
                        observed=Availability.ACTIVE,
                        changed=False,
                        success=True,
                    ),
                )
            )

    monkeypatch.setattr(
        operations,
        "default_slack_availability_service",
        lambda *, trace_output=None: _Service(),
    )
    skipped = JobResultLine(kvm="unconfigured", ok=False, text="missing password")

    outcome = operations.availability_work(
        ("ready",),
        Availability.ACTIVE,
        preflight_results=(skipped,),
    )(lambda _: None)

    assert outcome.ok is False
    assert outcome.summary == "Applied active to 1 of 2 KVM(s)."
    assert outcome.results == (
        JobResultLine(kvm="ready", ok=True, text="already active; no click sent"),
        skipped,
    )


def test_availability_work_does_not_build_a_service_when_every_profile_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        operations,
        "default_slack_availability_service",
        lambda **kwargs: pytest.fail("empty batch should not build a service"),
    )
    skipped = JobResultLine(kvm="unconfigured", ok=False, text="missing password")

    outcome = operations.availability_work(
        (),
        Availability.ACTIVE,
        preflight_results=(skipped,),
    )(lambda _: None)

    assert outcome.summary == "Applied active to 0 of 1 KVM(s)."
    assert outcome.results == (skipped,)


def test_triage_work_includes_preflight_failures_in_its_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Service:
        def run(self, kvms: tuple[str, ...]) -> TriageBatchResult:
            return TriageBatchResult(
                reports=(TriageReport(kvm="ready", success=True, confidence=0.95),)
            )

    monkeypatch.setattr(
        operations,
        "default_slack_triage_service",
        lambda *, trace_output=None: _Service(),
    )
    skipped = JobResultLine(kvm="terminal_totp", ok=False, text="Keychain enrollment required")

    outcome = operations.triage_work(
        ("ready",),
        preflight_results=(skipped,),
    )(lambda _: None)

    assert outcome.ok is False
    assert outcome.summary == "0 conversation(s) need attention across 2 KVM(s)."
    assert outcome.results == (
        JobResultLine(kvm="ready", ok=True, text="0 need attention, 0 FYI"),
        skipped,
    )


def test_triage_work_does_not_build_a_service_when_every_profile_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        operations,
        "default_slack_triage_service",
        lambda **kwargs: pytest.fail("empty batch should not build a service"),
    )
    skipped = JobResultLine(kvm="terminal_totp", ok=False, text="Keychain required")

    outcome = operations.triage_work((), preflight_results=(skipped,))(lambda _: None)

    assert outcome.summary == "0 conversation(s) need attention across 1 KVM(s)."
    assert outcome.payload == {"reports": []}


def test_agenda_work_does_not_describe_a_failed_read_as_zero_meetings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Service:
        def run(self, kvms: tuple[str, ...]) -> AgendaBatchResult:
            assert kvms == ("heidrick",)
            return AgendaBatchResult(
                reports=(
                    AgendaReport(
                        kvm="heidrick",
                        success=False,
                        error="Another local agent controller is already using this PiKVM.",
                    ),
                )
            )

    monkeypatch.setattr(
        operations,
        "default_agenda_service",
        lambda *, trace_output=None: _Service(),
    )

    outcome = operations.agenda_work(("heidrick",))(lambda _: None)

    assert outcome.ok is False
    assert outcome.summary == "Calendar read stopped on 1 environment."
    assert "0 meeting" not in outcome.summary
    assert outcome.results == (
        JobResultLine(
            kvm="heidrick",
            ok=False,
            text="Another local agent controller is already using this PiKVM.",
        ),
    )


def test_agenda_work_distinguishes_meetings_read_from_profiles_that_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meeting = MeetingItem(
        title="Review",
        start_text="3:00 PM",
        end_text="3:30 PM",
        start_minutes=900,
        status=MeetingStatus.UPCOMING,
        all_day=False,
        location=None,
        organizer=None,
        is_online=True,
        declined=False,
    )

    class _Service:
        def run(self, kvms: tuple[str, ...]) -> AgendaBatchResult:
            assert kvms == ("ready",)
            return AgendaBatchResult(
                reports=(
                    AgendaReport(
                        kvm="ready",
                        success=True,
                        surface=CalendarSurface.TEAMS,
                        current_time_text="2:00 PM",
                        items=(meeting,),
                    ),
                )
            )

    monkeypatch.setattr(
        operations,
        "default_agenda_service",
        lambda *, trace_output=None: _Service(),
    )
    skipped = JobResultLine(kvm="unready", ok=False, text="configuration incomplete")

    outcome = operations.agenda_work(
        ("ready",),
        preflight_results=(skipped,),
    )(lambda _: None)

    assert outcome.ok is False
    assert outcome.summary == "1 meeting still ahead from 1 of 2 environments; 1 stopped."
    assert outcome.payload is not None
    reports = outcome.payload["reports"]
    assert isinstance(reports, list)
    assert reports[-1] == {
        "kvm": "unready",
        "success": False,
        "error": "configuration incomplete",
    }


def test_agenda_work_surfaces_preflight_failures_in_the_agenda_panel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        operations,
        "default_agenda_service",
        lambda **kwargs: pytest.fail("empty batch should not build a service"),
    )
    skipped = JobResultLine(kvm="unready", ok=False, text="configuration incomplete")

    outcome = operations.agenda_work((), preflight_results=(skipped,))(lambda _: None)

    assert outcome.summary == "Calendar read stopped on 1 environment."
    assert outcome.payload == {
        "reports": [{"kvm": "unready", "success": False, "error": "configuration incomplete"}]
    }


def test_schedule_work_run_now_emits_the_command_output(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_execute(args: object, **kwargs: object) -> tuple[str, int]:
        captured["action"] = args.schedule_action  # type: ignore[attr-defined]
        captured["availability"] = args.availability  # type: ignore[attr-defined]
        return "Desired Slack availability: away (Asia/Karachi)\nheidrick  ✓ away", 0

    monkeypatch.setattr(operations, "execute_schedule_command", fake_execute)
    monkeypatch.setattr(
        operations,
        "default_slack_availability_service",
        lambda *, trace_output=None: object(),
    )

    events: list[str] = []
    outcome = operations.schedule_work(
        ScheduleAction.RUN_NOW,
        Availability.AWAY,
        manager=_FakeManager(),  # type: ignore[arg-type]
    )(events.append)

    assert outcome.ok is True
    assert captured["action"] == "run-now"
    assert captured["availability"] is Availability.AWAY
    assert events[0] == "Desired Slack availability: away (Asia/Karachi)"


def test_schedule_work_parses_per_profile_results_for_partial_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_execute(args: object, **kwargs: object) -> tuple[str, int]:
        return (
            "Desired Slack availability: away (Asia/Karachi)\n"
            "one  ✓ away\n"
            "two  ✗ verification failed",
            1,
        )

    monkeypatch.setattr(operations, "execute_schedule_command", fake_execute)
    monkeypatch.setattr(
        operations,
        "default_slack_availability_service",
        lambda *, trace_output=None: object(),
    )
    skipped = JobResultLine(kvm="three", ok=False, text="configuration incomplete")

    outcome = operations.schedule_work(
        ScheduleAction.RUN_NOW,
        Availability.AWAY,
        manager=_FakeManager(),  # type: ignore[arg-type]
        targets=("one", "two"),
        preflight_results=(skipped,),
    )(lambda _: None)

    assert outcome.ok is False
    assert outcome.results == (
        JobResultLine(kvm="one", ok=True, text="away"),
        JobResultLine(kvm="two", ok=False, text="verification failed"),
        skipped,
    )
    assert {item.ok for item in outcome.results} == {False, True}


def test_schedule_result_parser_ignores_a_later_logging_warning() -> None:
    results = operations._schedule_result_lines(
        [
            "one  ✓ away",
            "one  ! Availability changed, but the local log could not be written.",
        ],
        ("one",),
        True,
    )

    assert results == (JobResultLine(kvm="one", ok=True, text="away"),)


def test_schedule_work_reports_preflight_failures_when_no_profile_is_ready() -> None:
    skipped = (
        JobResultLine(kvm="one", ok=False, text="configuration incomplete"),
        JobResultLine(kvm="two", ok=False, text="Keychain enrollment required"),
    )

    outcome = operations.schedule_work(
        ScheduleAction.RECONCILE,
        None,
        targets=(),
        preflight_results=skipped,
    )(lambda _: None)

    assert outcome.ok is False
    assert outcome.summary == "No eligible environment; skipped 2 KVM(s)."
    assert outcome.results == skipped


def test_schedule_work_reconcile_does_not_use_the_if_due_shortcut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_execute(args: object, **kwargs: object) -> tuple[str, int]:
        captured["if_due"] = args.if_due  # type: ignore[attr-defined]
        return "checked", 0

    monkeypatch.setattr(operations, "execute_schedule_command", fake_execute)
    monkeypatch.setattr(
        operations,
        "default_slack_availability_service",
        lambda *, trace_output=None: object(),
    )

    operations.schedule_work(
        ScheduleAction.RECONCILE,
        None,
        manager=_FakeManager(),  # type: ignore[arg-type]
    )(lambda _: None)

    assert captured["if_due"] is False


def test_schedule_endpoint_serves_the_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ReconciliationStateStore(tmp_path / "state.json")
    manager = _FakeManager(interpreter=Path("/usr/bin/python3"), can_run=False)
    build = operations.schedule_snapshot
    monkeypatch.setattr(
        operations,
        "schedule_snapshot",
        lambda **kwargs: build(
            manager=manager,  # type: ignore[arg-type]
            state_store=store,
            now=datetime(2026, 8, 11, 9, 0, tzinfo=UTC),
        ),
    )
    app = create_app(token=TOKEN)
    app.state.log_path = tmp_path / "log.jsonl"
    client = TestClient(app, base_url="http://127.0.0.1", headers={TOKEN_HEADER: TOKEN})

    payload = client.get("/api/schedule").json()

    assert payload["healthy"] is False
    assert payload["interpreter"] == "/usr/bin/python3"
    assert payload["desired_now"] == "away"
    assert len(payload["agents"]) == 3


def test_schedule_action_endpoint_starts_an_install_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(operations, "profile_snapshots", lambda: [])

    def fake_work(action: ScheduleAction, availability: Availability | None) -> object:
        return lambda emit: JobOutcome(ok=True, summary=f"{action.value} ok")

    monkeypatch.setattr(operations, "schedule_work", fake_work)
    app = create_app(token=TOKEN)
    app.state.log_path = tmp_path / "log.jsonl"
    client = TestClient(app, base_url="http://127.0.0.1", headers={TOKEN_HEADER: TOKEN})

    response = client.post("/api/schedule/actions", json={"action": "install"})

    assert response.status_code == 202
    assert response.json()["kind"] == JobKind.SCHEDULE_INSTALL.value

    job_id = response.json()["id"]
    with client.stream("GET", f"/api/jobs/{job_id}/events") as stream:
        for line in stream.iter_lines():
            if line.startswith("data:") and "status" in line:
                final = json.loads(line.split(":", 1)[1].strip())
                assert final["status"] == JobStatus.SUCCEEDED.value
                break


def test_schedule_run_now_requires_configured_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(operations, "profile_snapshots", lambda: [])
    app = create_app(token=TOKEN)
    app.state.log_path = tmp_path / "log.jsonl"
    client = TestClient(app, base_url="http://127.0.0.1", headers={TOKEN_HEADER: TOKEN})

    response = client.post("/api/schedule/actions", json={"action": "run-now"})

    assert response.status_code == 400
    assert "PIKVM_PROFILES" in response.json()["detail"]


# --------------------------------------------------------------------------------------------
# Outcome-based schedule health, per-KVM liveness, and interruptible dashboard runs
# --------------------------------------------------------------------------------------------


def _kvm(name: str, *, configured: bool = True, endpoint: str | None = None) -> KvmProfile:
    return KvmProfile(
        name=name,
        endpoint=(endpoint or f"https://{name}.example.local") if configured else None,
        totp_required=True,
        verify_ssl=False,
        interactive_totp=False,
        configured=configured,
        problem=None if configured else "missing password",
    )


def _write_runs(path: Path, runs: list[tuple[str, int, str, str | None]]) -> None:
    base = datetime(2026, 8, 11, 9, 0, tzinfo=UTC)
    lines = []
    for kvm, minutes_ago, outcome, stop_code in runs:
        lines.append(
            json.dumps(
                {
                    "timestamp": (base - timedelta(minutes=minutes_ago)).isoformat(),
                    "kvm": kvm,
                    "desired_availability": "away",
                    "observed_availability": "unknown" if outcome == "failure" else "away",
                    "changed": None,
                    "outcome": outcome,
                    "stop_code": stop_code,
                    "error": "connect timeout" if stop_code == "pikvm_unreachable" else None,
                    "telemetry": {
                        "sessions": 1,
                        "steps": 2,
                        "hid_actions": 1,
                        "vision_calls": 2,
                        "planner_calls": 2,
                        "total_tokens": 1234,
                        "runtime_seconds": 12.5,
                    },
                }
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fresh_store(tmp_path: Path, profiles: tuple[str, ...]) -> ReconciliationStateStore:
    """State verified just after the last boundary (Tue 02:00 Karachi = Mon 21:00 UTC)."""

    verified = datetime(2026, 8, 10, 21, 5, tzinfo=UTC)
    store = ReconciliationStateStore(tmp_path / "state.json", now=lambda: verified)
    store.record_successes(
        AvailabilityBatchResult(
            results=tuple(
                AvailabilityResult(
                    kvm=name,
                    desired=Availability.AWAY,
                    observed=Availability.AWAY,
                    changed=False,
                    success=True,
                )
                for name in profiles
            )
        )
    )
    return store


def test_schedule_snapshot_flags_three_consecutive_failures_and_surfaces_the_last_run(
    tmp_path: Path,
) -> None:
    log = tmp_path / "log.jsonl"
    _write_runs(
        log,
        [
            ("heidrick", 400, "success", None),
            ("heidrick", 180, "failure", "pikvm_unreachable"),
            ("heidrick", 120, "failure", "lock_busy"),
            ("heidrick", 60, "failure", "pikvm_unreachable"),
            ("heidrick", 10, "failure", "pikvm_unreachable"),
            ("nbc_kvm", 30, "success", None),
        ],
    )
    profiles = [_kvm("heidrick"), _kvm("nbc_kvm")]

    snapshot = operations.schedule_snapshot(
        manager=_FakeManager(),  # type: ignore[arg-type]
        state_store=_fresh_store(tmp_path, ("heidrick", "nbc_kvm")),
        now=datetime(2026, 8, 11, 9, 0, tzinfo=UTC),
        profiles=profiles,
        log_path=log,
        lock_directory=tmp_path / "locks",
    )

    assert snapshot.healthy is False
    assert any(
        "heidrick has failed its last 3 runs (PiKVM unreachable" in problem
        for problem in snapshot.problems
    )
    assert snapshot.last_run is not None
    assert snapshot.last_run.kvm == "heidrick"
    assert snapshot.last_run.outcome == "failure"
    assert snapshot.last_run.stop_code == "pikvm_unreachable"
    assert snapshot.last_transition_at == datetime(2026, 8, 10, 21, 0, tzinfo=UTC)

    by_name = {status.name: status for status in snapshot.kvms}
    heidrick = by_name["heidrick"]
    assert heidrick.consecutive_failures == 3
    assert heidrick.unreachable_since == datetime(2026, 8, 11, 6, 0, tzinfo=UTC)
    assert heidrick.alert is not None
    assert heidrick.alert.label == "PiKVM unreachable"
    assert heidrick.alert.count == 3
    assert heidrick.reachable is None  # no probe configured
    assert heidrick.workflow_running is False
    assert by_name["nbc_kvm"].alert is None
    assert by_name["nbc_kvm"].consecutive_failures == 0


def test_schedule_snapshot_flags_a_desired_state_unapplied_long_after_the_boundary(
    tmp_path: Path,
) -> None:
    # Verified active before the Tue 02:00 (away) boundary and never since: 7 h overdue.
    verified = datetime(2026, 8, 10, 15, 0, tzinfo=UTC)
    store = ReconciliationStateStore(tmp_path / "state.json", now=lambda: verified)
    store.record_successes(
        AvailabilityBatchResult(
            results=(
                AvailabilityResult(
                    kvm="heidrick",
                    desired=Availability.ACTIVE,
                    observed=Availability.ACTIVE,
                    changed=True,
                    success=True,
                ),
            )
        )
    )

    stale = operations.schedule_snapshot(
        manager=_FakeManager(),  # type: ignore[arg-type]
        state_store=store,
        now=datetime(2026, 8, 11, 4, 0, tzinfo=UTC),
        profiles=[_kvm("heidrick"), _kvm("broken", configured=False)],
        log_path=tmp_path / "missing.jsonl",
        lock_directory=tmp_path / "locks",
    )
    assert stale.healthy is False
    assert [
        problem for problem in stale.problems if "heidrick has not been verified away" in problem
    ]
    assert not [problem for problem in stale.problems if problem.startswith("broken")]

    # Within the two-hour grace window after the boundary the same state is not a problem.
    recent = operations.schedule_snapshot(
        manager=_FakeManager(),  # type: ignore[arg-type]
        state_store=store,
        now=datetime(2026, 8, 10, 22, 0, tzinfo=UTC),
        profiles=[_kvm("heidrick")],
        log_path=tmp_path / "missing.jsonl",
        lock_directory=tmp_path / "locks",
    )
    assert recent.healthy is True

    # And a verification after the boundary satisfies it however long ago the boundary was.
    healthy = operations.schedule_snapshot(
        manager=_FakeManager(),  # type: ignore[arg-type]
        state_store=_fresh_store(tmp_path / "fresh", ("heidrick",)),
        now=datetime(2026, 8, 11, 9, 0, tzinfo=UTC),
        profiles=[_kvm("heidrick")],
        log_path=tmp_path / "missing.jsonl",
        lock_directory=tmp_path / "locks",
    )
    assert healthy.healthy is True
    assert healthy.kvms[0].alert is None


def test_reachability_cache_probes_once_per_minute_and_never_sends_credentials() -> None:
    calls: list[tuple[str, bool]] = []
    clock = {"now": 100.0}

    def prober(base_url: str, verify_ssl: bool) -> fleet.ProbeResult:
        calls.append((base_url, verify_ssl))
        return fleet.ProbeResult(
            reachable=base_url.endswith("up"),
            detail="HTTP 401" if base_url.endswith("up") else "connect timeout",
            checked_at=datetime(2026, 8, 11, 9, 0, tzinfo=UTC),
        )

    cache = fleet.ReachabilityCache(prober=prober, ttl_seconds=60.0, monotonic=lambda: clock["now"])
    results = cache.check_many(
        [("https://up", False), ("https://down", True), ("https://up", False)]
    )

    assert results["https://up"].reachable is True
    assert results["https://down"].reachable is False
    assert sorted(calls) == [("https://down", True), ("https://up", False)]

    clock["now"] = 150.0
    cache.check("https://up", verify_ssl=False)
    assert len(calls) == 2
    clock["now"] = 161.0
    cache.check("https://up", verify_ssl=False)
    assert len(calls) == 3


def test_reachability_probe_treats_any_http_answer_as_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        assert url == "https://kvm.example.local/api/info"
        assert "auth" not in kwargs and "headers" not in kwargs
        assert kwargs["timeout"] == fleet.PROBE_TIMEOUT_SECONDS
        return httpx.Response(401)

    monkeypatch.setattr(fleet.httpx, "get", fake_get)
    result = fleet.probe_endpoint("https://kvm.example.local/", False)
    assert result.reachable is True
    assert result.detail == "HTTP 401"

    def timeout(url: str, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(fleet.httpx, "get", timeout)
    down = fleet.probe_endpoint("https://kvm.example.local", False)
    assert down.reachable is False
    assert down.detail == "connect timeout"


def test_workflow_running_observes_a_held_endpoint_lock_without_taking_it(tmp_path: Path) -> None:
    locks = tmp_path / "locks"
    endpoint = "https://kvm.example.local"
    assert fleet.workflow_running(endpoint, directory=locks) is False

    lock = ControllerLock.for_endpoint(endpoint, directory=locks)
    lock.acquire()
    try:
        assert fleet.workflow_running(endpoint, directory=locks) is True
        # Probing must not have disturbed the real holder or its pid record.
        assert lock.held
    finally:
        lock.release()
    assert fleet.workflow_running(endpoint, directory=locks) is False

    # After a probe the file is still lockable by a real workflow.
    path = ControllerLock.for_endpoint(endpoint, directory=locks)._path
    descriptor = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def test_schedule_work_uses_short_interruptible_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_execute(args: object, **kwargs: object) -> tuple[str, int]:
        captured["retry_count"] = kwargs["retry_count"]
        captured["retry_delay_seconds"] = kwargs["retry_delay_seconds"]
        sleeper = kwargs["sleeper"]
        assert callable(sleeper)
        try:
            sleeper(0.05)
        except RetryWaitInterrupted:
            captured["interrupted"] = True
        return "Desired Slack availability: away (Asia/Karachi)\none  ✓ away", 0

    monkeypatch.setattr(operations, "execute_schedule_command", fake_execute)
    monkeypatch.setattr(
        operations,
        "default_slack_availability_service",
        lambda *, trace_output=None: object(),
    )
    cancel = threading.Event()
    cancel.set()

    outcome = operations.schedule_work(
        ScheduleAction.RECONCILE,
        None,
        manager=_FakeManager(),  # type: ignore[arg-type]
        targets=("one",),
        cancel=cancel,
    )(lambda _: None)

    assert captured["retry_count"] == operations.DASHBOARD_RETRY_COUNT == 1
    assert captured["retry_delay_seconds"] == operations.DASHBOARD_RETRY_DELAY_SECONDS == 30.0
    assert captured["interrupted"] is True
    assert outcome.cancelled is True
    assert "Cancelled" in outcome.summary
    assert outcome.results == (JobResultLine(kvm="one", ok=True, text="away"),)
