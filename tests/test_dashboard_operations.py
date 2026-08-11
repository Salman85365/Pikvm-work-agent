from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from work_agent.dashboard import operations
from work_agent.dashboard.app import TOKEN_HEADER, create_app
from work_agent.dashboard.jobs import JobOutcome
from work_agent.dashboard.models import JobKind, JobStatus, ScheduleAction
from work_agent.schedule.launchd import LaunchAgentStatus, ScheduleHealth
from work_agent.schedule.state import ReconciliationStateStore
from work_agent.slack.models import Availability, AvailabilityBatchResult, AvailabilityResult

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
        lambda: build(
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
