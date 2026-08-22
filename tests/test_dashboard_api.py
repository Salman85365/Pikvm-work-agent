from __future__ import annotations

import argparse
import json
import socket
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
from work_agent.dashboard import cli as dashboard_cli
from work_agent.dashboard import fleet, operations
from work_agent.dashboard.app import TOKEN_HEADER, create_app
from work_agent.dashboard.cli import execute_dashboard_command
from work_agent.dashboard.errors import DashboardError
from work_agent.dashboard.jobs import JobConflictError, JobManager, JobOutcome
from work_agent.dashboard.models import JobKind, JobResultLine, JobStatus, KvmProfile
from work_agent.pikvm import PiKVMTotpError
from work_agent.schedule.cli import RetryWaitInterrupted
from work_agent.schedule.state import ReconciliationStateStore
from work_agent.slack.models import Availability, AvailabilityBatchResult, AvailabilityResult
from work_agent.slack.triage_models import TriageBatchResult, TriageReport

TOKEN = "test-dashboard-token"
_BASE = "http://127.0.0.1"


def _profile(
    name: str,
    *,
    configured: bool = True,
    interactive_totp: bool = False,
) -> KvmProfile:
    return KvmProfile(
        name=name,
        endpoint=f"https://{name}.example.local" if configured else None,
        totp_required=True,
        verify_ssl=False,
        interactive_totp=interactive_totp,
        configured=configured,
        problem=None if configured else "missing password",
    )


@pytest.fixture
def profiles(monkeypatch: pytest.MonkeyPatch) -> tuple[KvmProfile, ...]:
    values = (_profile("heidrick"), _profile("nbc_kvm"))
    monkeypatch.setattr(operations, "profile_snapshots", lambda: list(values))
    monkeypatch.setattr(operations, "ensure_runnable", lambda targets: None)
    monkeypatch.setattr(operations, "default_profile", lambda items: "heidrick")
    return values


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = create_app(token=TOKEN)
    app.state.log_path = tmp_path / "slack-availability.jsonl"
    app.state.state_path = tmp_path / "state.json"
    return TestClient(app, base_url=_BASE, headers={TOKEN_HEADER: TOKEN})


def _write_log(path: Path, entries: list[dict[str, object]]) -> None:
    path.write_text(
        "\n".join(json.dumps(entry) for entry in entries) + "\n",
        encoding="utf-8",
    )


def _stream_done(client: TestClient, job_id: str) -> dict[str, object]:
    with client.stream("GET", f"/api/jobs/{job_id}/events") as stream:
        event = "message"
        for line in stream.iter_lines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and event == "done":
                return json.loads(line.split(":", 1)[1].strip())
    raise AssertionError("job stream ended without a final result")


def _entry(
    *,
    minutes_ago: int,
    kvm: str = "heidrick",
    outcome: str = "success",
    desired: str | None = "active",
    observed: str = "active",
    changed: bool | None = True,
    error: str | None = None,
) -> dict[str, object]:
    moment = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return {
        "timestamp": moment.isoformat(),
        "kvm": kvm,
        "desired_availability": desired,
        "observed_availability": observed,
        "changed": changed,
        "outcome": outcome,
        "error": error,
    }


def test_requests_without_a_token_are_refused(client: TestClient) -> None:
    response = client.get("/api/config", headers={TOKEN_HEADER: ""})

    assert response.status_code == 403
    assert "token" in response.json()["detail"].lower()


def test_non_loopback_host_headers_are_refused(client: TestClient) -> None:
    response = client.get("/api/config", headers={"Host": "work-agent.example.com"})

    assert response.status_code == 421


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "127.0.0.1:8787", "localhost:8787", "[::1]:8787", "::1", "127.0.0.5:8788"],
)
def test_loopback_host_headers_are_accepted(
    client: TestClient,
    profiles: tuple[KvmProfile, ...],
    host: str,
) -> None:
    assert client.get("/api/config", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1.evil.example", "localhost.evil.example", "[::1", "10.0.0.1:8787", "[fe80::1]:8787"],
)
def test_lookalike_host_headers_are_refused(client: TestClient, host: str) -> None:
    assert client.get("/api/config", headers={"Host": host}).status_code == 421


def test_index_embeds_the_session_token_and_is_not_cached(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert TOKEN in response.text
    assert "__DASHBOARD_TOKEN__" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_static_dashboard_assets_are_not_cached(client: TestClient) -> None:
    response = client.get("/static/app.js")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def test_config_exposes_profiles_without_secrets(
    client: TestClient,
    profiles: tuple[KvmProfile, ...],
) -> None:
    response = client.get("/api/config")
    payload = response.json()

    assert response.headers["cache-control"] == "no-store"
    assert [item["name"] for item in payload["kvms"]] == ["heidrick", "nbc_kvm"]
    assert payload["default_kvm"] == "heidrick"
    assert payload["timezone"] == "Asia/Karachi"
    assert "password" not in json.dumps(payload).lower()


def test_history_scopes_by_kvm_and_range_and_tolerates_bad_lines(client: TestClient) -> None:
    path = Path(client.app.state.log_path)
    path.write_text(
        "\n".join(
            [
                json.dumps(_entry(minutes_ago=5)),
                "{ truncated line",
                json.dumps(
                    _entry(minutes_ago=30, kvm="nbc_kvm", outcome="failure", error="stopped")
                ),
                json.dumps(_entry(minutes_ago=60 * 24 * 9, changed=False)),
                json.dumps({"timestamp": "nope", "kvm": "heidrick", "outcome": "success"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    everything = client.get("/api/history", params={"days": 0}).json()
    assert everything["summary"]["total"] == 3
    assert everything["unreadable_lines"] == 2
    assert everything["log_present"] is True

    recent = client.get("/api/history", params={"days": 7}).json()
    assert recent["summary"]["total"] == 2

    scoped = client.get("/api/history", params={"days": 7, "kvm": "nbc_kvm"}).json()
    assert scoped["summary"]["total"] == 1
    assert scoped["summary"]["failure"] == 1
    assert scoped["summary"]["failure_reasons"] == [{"reason": "stopped", "count": 1}]
    assert [item["kvm"] for item in scoped["summary"]["per_kvm"]] == ["nbc_kvm"]


def test_history_summary_covers_the_scope_while_records_are_paged(client: TestClient) -> None:
    path = Path(client.app.state.log_path)
    _write_log(path, [_entry(minutes_ago=index + 1) for index in range(12)])

    payload = client.get("/api/history", params={"days": 0, "limit": 5}).json()

    assert len(payload["records"]) == 5
    assert payload["summary"]["total"] == 12
    assert payload["summary"]["success_rate"] == 1.0
    assert payload["summary"]["changes_applied"] == 12


def test_history_groups_stop_reasons_into_reliability_categories(client: TestClient) -> None:
    path = Path(client.app.state.log_path)
    _write_log(
        path,
        [
            _entry(
                minutes_ago=1,
                outcome="failure",
                error=(
                    "The last action's expected result could not be visually verified; the action "
                    "was not repeated."
                ),
            ),
            _entry(
                minutes_ago=2,
                outcome="failure",
                error=("Read-only final-state verification found no manual availability evidence."),
            ),
            _entry(
                minutes_ago=3,
                outcome="failure",
                error=(
                    "Local policy required interactive approval, which unattended Slack workflows "
                    "deny."
                ),
            ),
            _entry(
                minutes_ago=4,
                outcome="failure",
                error="The next-action confidence was below the safety threshold.",
            ),
            _entry(
                minutes_ago=5,
                outcome="failure",
                error=(
                    "The controller stopped before repeating the same action on an unchanged "
                    "screen."
                ),
            ),
            _entry(
                minutes_ago=6,
                outcome="failure",
                error="The verified controller stopped with status failed.",
            ),
            _entry(
                minutes_ago=7,
                outcome="failure",
                error="The bounded controller paused before Slack availability could be verified.",
            ),
            _entry(minutes_ago=8, outcome="failure", error="Something else entirely."),
        ],
    )

    summary = client.get("/api/history", params={"days": 0}).json()["summary"]
    grouped = {item["category"]: item["count"] for item in summary["failure_categories"]}

    assert grouped == {
        "verification_failed": 1,
        "completion_unverified": 1,
        "approval_denied": 1,
        "screen_low_confidence": 1,
        "stuck_repeated_action": 1,
        "legacy_unclassified": 2,
        "other": 1,
    }
    # Most frequent first, so the chart never has to re-sort.
    assert summary["failure_categories"][0]["category"] == "legacy_unclassified"


def test_history_categorizes_the_real_world_stop_reasons(client: TestClient) -> None:
    """The most common real reason must not fall into the uninformative 'other' bucket."""

    path = Path(client.app.state.log_path)
    _write_log(
        path,
        [
            _entry(
                minutes_ago=index + 1,
                outcome="failure",
                error="The verified controller stopped with status failed.",
            )
            for index in range(3)
        ]
        + [
            _entry(
                minutes_ago=10,
                outcome="failure",
                error=(
                    "Read-only final-state verification did not complete: The verified controller "
                    "stopped with status failed."
                ),
            ),
        ],
    )

    summary = client.get("/api/history", params={"days": 0}).json()["summary"]
    grouped = {item["category"]: item["count"] for item in summary["failure_categories"]}

    assert grouped == {"legacy_unclassified": 3, "verification_failed": 1}
    assert "other" not in grouped


def test_history_separates_changes_no_ops_and_reads(client: TestClient) -> None:
    path = Path(client.app.state.log_path)
    _write_log(
        path,
        [
            _entry(minutes_ago=1, changed=True),
            _entry(minutes_ago=2, changed=False),
            _entry(minutes_ago=3, changed=False),
            _entry(minutes_ago=4, desired=None, changed=False, observed="away"),
            _entry(minutes_ago=5, outcome="failure", changed=None, error="stopped"),
        ],
    )

    summary = client.get("/api/history", params={"days": 0}).json()["summary"]

    assert summary["changes_applied"] == 1
    assert summary["no_ops"] == 2
    assert summary["reads"] == 1
    assert summary["success"] == 4


def test_history_without_a_log_reports_absence_rather_than_failing(client: TestClient) -> None:
    payload = client.get("/api/history").json()

    assert payload["log_present"] is False
    assert payload["records"] == []
    assert payload["summary"]["total"] == 0


def test_availability_rejects_an_unknown_profile(
    client: TestClient,
    profiles: tuple[KvmProfile, ...],
) -> None:
    response = client.post("/api/availability", json={"kvm": "nope"})

    assert response.status_code == 404


def test_availability_rejects_a_profile_needing_a_terminal_totp(
    client: TestClient,
    profiles: tuple[KvmProfile, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(targets: tuple[str, ...]) -> None:
        raise PiKVMTotpError("Profile heidrick is configured for interactive TOTP entry.")

    monkeypatch.setattr(operations, "ensure_runnable", refuse)

    response = client.post("/api/availability", json={"kvm": "heidrick"})

    assert response.status_code == 400
    assert "interactive TOTP" in response.json()["detail"]


def test_availability_streams_trace_then_a_final_result(
    client: TestClient,
    profiles: tuple[KvmProfile, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_work(targets: tuple[str, ...], desired: object) -> object:
        captured["targets"] = targets
        captured["desired"] = desired

        def run(emit: object) -> JobOutcome:
            assert callable(emit)
            emit("heidrick  | State: observing")
            emit("heidrick  | Policy: allow")
            return JobOutcome(
                ok=True,
                summary="Applied active to 1 of 1 KVM(s).",
                results=(JobResultLine(kvm="heidrick", ok=True, text="changed to active"),),
            )

        return run

    monkeypatch.setattr(operations, "availability_work", fake_work)

    started = client.post("/api/availability", json={"kvm": "heidrick", "availability": "active"})
    assert started.status_code == 202
    job_id = started.json()["id"]
    assert started.json()["kind"] == JobKind.AVAILABILITY_SET.value

    frames: list[tuple[str, dict[str, object]]] = []
    with client.stream("GET", f"/api/jobs/{job_id}/events") as stream:
        assert stream.headers["content-type"].startswith("text/event-stream")
        event = "message"
        for line in stream.iter_lines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                frames.append((event, json.loads(line.split(":", 1)[1].strip())))
                if event == "done":
                    break

    texts = [payload["text"] for name, payload in frames if name == "event"]
    assert texts == ["heidrick  | State: observing", "heidrick  | Policy: allow"]

    done = next(payload for name, payload in frames if name == "done")
    assert done["status"] == JobStatus.SUCCEEDED.value
    assert done["results"] == [{"kvm": "heidrick", "ok": True, "text": "changed to active"}]
    assert captured["targets"] == ("heidrick",)


def test_availability_for_all_kvms_targets_every_profile(
    client: TestClient,
    profiles: tuple[KvmProfile, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_work(targets: tuple[str, ...], desired: object) -> object:
        seen["targets"] = targets
        return lambda emit: JobOutcome(ok=True, summary="done")

    monkeypatch.setattr(operations, "availability_work", fake_work)

    response = client.post("/api/availability", json={"kvm": "__all__"})

    assert response.status_code == 202
    assert response.json()["target"] == "all KVMs"
    assert response.json()["targets"] == ["heidrick", "nbc_kvm"]
    assert seen["targets"] == ("heidrick", "nbc_kvm")


def test_all_kvm_availability_skips_unready_profiles_and_reports_each_failure(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots = [
        _profile("ready"),
        _profile("unconfigured", configured=False),
        _profile("terminal_totp", interactive_totp=True),
    ]
    checked: list[tuple[str, ...]] = []
    service_targets: list[tuple[str, ...]] = []
    monkeypatch.setattr(operations, "profile_snapshots", lambda: snapshots)
    monkeypatch.setattr(operations, "ensure_runnable", checked.append)

    class _Service:
        def run(
            self,
            targets: tuple[str, ...],
            desired: Availability | None,
        ) -> AvailabilityBatchResult:
            service_targets.append(targets)
            return AvailabilityBatchResult(
                results=(
                    AvailabilityResult(
                        kvm="ready",
                        desired=desired,
                        observed=Availability.ACTIVE,
                        changed=True,
                        success=True,
                    ),
                )
            )

    monkeypatch.setattr(
        operations,
        "default_slack_availability_service",
        lambda *, trace_output=None: _Service(),
    )

    started = client.post(
        "/api/availability",
        json={"kvm": "__all__", "availability": "active"},
    )
    done = _stream_done(client, started.json()["id"])

    assert started.status_code == 202
    assert started.json()["targets"] == ["ready"]
    assert checked == [("ready",)]
    assert service_targets == [("ready",)]
    assert done["status"] == JobStatus.PARTIAL.value
    assert done["summary"] == "Applied active to 1 of 3 KVM(s)."
    assert done["results"] == [
        {"kvm": "ready", "ok": True, "text": "changed to active"},
        {"kvm": "unconfigured", "ok": False, "text": "missing password"},
        {
            "kvm": "terminal_totp",
            "ok": False,
            "text": (
                "Interactive TOTP cannot run from the dashboard. Enroll this profile in "
                "Keychain first."
            ),
        },
    ]


def test_all_kvm_triage_skips_unready_profiles_and_reports_each_failure(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots = [
        _profile("ready"),
        _profile("unconfigured", configured=False),
        _profile("terminal_totp", interactive_totp=True),
    ]
    checked: list[tuple[str, ...]] = []
    service_targets: list[tuple[str, ...]] = []
    monkeypatch.setattr(operations, "profile_snapshots", lambda: snapshots)
    monkeypatch.setattr(operations, "ensure_runnable", checked.append)

    class _Service:
        def run(self, targets: tuple[str, ...]) -> TriageBatchResult:
            service_targets.append(targets)
            return TriageBatchResult(
                reports=(TriageReport(kvm="ready", success=True, confidence=0.99),)
            )

    monkeypatch.setattr(
        operations,
        "default_slack_triage_service",
        lambda *, trace_output=None: _Service(),
    )

    started = client.post("/api/triage", json={"kvm": "__all__"})
    done = _stream_done(client, started.json()["id"])

    assert started.status_code == 202
    assert checked == [("ready",)]
    assert service_targets == [("ready",)]
    assert done["status"] == JobStatus.PARTIAL.value
    assert done["summary"] == "0 conversation(s) need attention across 3 KVM(s)."
    assert [item["kvm"] for item in done["results"]] == [
        "ready",
        "unconfigured",
        "terminal_totp",
    ]
    assert [item["ok"] for item in done["results"]] == [True, False, False]
    assert done["payload"]["reports"] == [
        {
            "kvm": "ready",
            "success": True,
            "error": None,
            "sidebar_truncated": False,
            "sidebar_obstructed": False,
            "confidence": 0.99,
            "total_unread_badge": None,
            "items": [],
        }
    ]


def test_agenda_reports_todays_meetings_and_skips_unready_profiles(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots = [_profile("ready"), _profile("unconfigured", configured=False)]
    service_targets: list[tuple[str, ...]] = []
    monkeypatch.setattr(operations, "profile_snapshots", lambda: snapshots)
    monkeypatch.setattr(operations, "ensure_runnable", lambda targets: None)

    item = MeetingItem(
        title="Sprint review",
        start_text="10:30 AM",
        end_text="11:00 AM",
        start_minutes=630,
        status=MeetingStatus.UPCOMING,
        all_day=False,
        location="Room 4",
        organizer=None,
        is_online=True,
        declined=False,
    )

    class _Service:
        def run(self, targets: tuple[str, ...]) -> AgendaBatchResult:
            service_targets.append(targets)
            return AgendaBatchResult(
                reports=(
                    AgendaReport(
                        kvm="ready",
                        success=True,
                        surface=CalendarSurface.TEAMS,
                        date_text="Tuesday, 12 August",
                        current_time_text="10:00 AM",
                        items=(item,),
                        confidence=0.93,
                    ),
                )
            )

    monkeypatch.setattr(
        operations,
        "default_agenda_service",
        lambda *, trace_output=None: _Service(),
    )

    started = client.post("/api/agenda", json={"kvm": "__all__"})
    done = _stream_done(client, started.json()["id"])

    assert started.status_code == 202
    assert service_targets == [("ready",)]
    assert done["status"] == JobStatus.PARTIAL.value
    assert done["summary"] == "1 meeting still ahead from 1 of 2 environments; 1 stopped."
    assert [line["ok"] for line in done["results"]] == [True, False]
    report = done["payload"]["reports"][0]
    assert report["kvm"] == "ready"
    assert report["current_time_text"] == "10:00 AM"
    assert report["clock_read"] is True
    assert report["items"] == [
        {
            "title": "Sprint review",
            "start_text": "10:30 AM",
            "end_text": "11:00 AM",
            "status": "upcoming",
            "all_day": False,
            "location": "Room 4",
            "organizer": None,
            "is_online": True,
            "declined": False,
        }
    ]


def test_agenda_rejects_an_unknown_profile(
    client: TestClient,
    profiles: tuple[KvmProfile, ...],
) -> None:
    response = client.post("/api/agenda", json={"kvm": "nope"})

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("snapshot", "expected_detail"),
    [
        (_profile("unconfigured", configured=False), "missing password"),
        (_profile("terminal_totp", interactive_totp=True), "Keychain first"),
    ],
)
def test_single_unready_target_returns_a_clear_bad_request(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    snapshot: KvmProfile,
    expected_detail: str,
) -> None:
    monkeypatch.setattr(operations, "profile_snapshots", lambda: [snapshot])

    def unexpected_check(targets: tuple[str, ...]) -> None:
        raise AssertionError(f"unready profile reached runtime preflight: {targets}")

    monkeypatch.setattr(operations, "ensure_runnable", unexpected_check)

    response = client.post("/api/availability", json={"kvm": snapshot.name})

    assert response.status_code == 400
    assert expected_detail in response.json()["detail"]


def test_mixed_per_kvm_results_complete_with_issues(
    client: TestClient,
    profiles: tuple[KvmProfile, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_work(targets: tuple[str, ...], desired: object) -> object:
        return lambda emit: JobOutcome(
            ok=False,
            summary="Applied active to 1 of 2 KVM(s).",
            results=(
                JobResultLine(kvm="heidrick", ok=True, text="changed to active"),
                JobResultLine(kvm="nbc_kvm", ok=False, text="verification stopped"),
            ),
        )

    monkeypatch.setattr(operations, "availability_work", fake_work)

    started = client.post(
        "/api/availability",
        json={"kvm": "__all__", "availability": "active"},
    ).json()

    with client.stream("GET", f"/api/jobs/{started['id']}/events") as stream:
        done = next(
            json.loads(line.split(":", 1)[1].strip())
            for line in stream.iter_lines()
            if line.startswith("data:")
        )

    assert done["status"] == JobStatus.PARTIAL.value
    assert done["targets"] == ["heidrick", "nbc_kvm"]


def test_schedule_run_now_targets_only_ready_profiles_and_reports_skips(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots = [
        _profile("ready"),
        _profile("unconfigured", configured=False),
        _profile("terminal_totp", interactive_totp=True),
    ]
    checked: list[tuple[str, ...]] = []
    captured: dict[str, object] = {}
    monkeypatch.setattr(operations, "profile_snapshots", lambda: snapshots)
    monkeypatch.setattr(operations, "ensure_runnable", checked.append)
    monkeypatch.setattr(
        operations,
        "default_slack_availability_service",
        lambda *, trace_output=None: object(),
    )

    def fake_execute(args: object, **kwargs: object) -> tuple[str, int]:
        captured["profiles"] = kwargs["profiles"]
        return "Desired Slack availability: away (Asia/Karachi)\nready  ✓ away", 0

    monkeypatch.setattr(operations, "execute_schedule_command", fake_execute)

    started = client.post(
        "/api/schedule/actions",
        json={"action": "run-now", "availability": "away"},
    )
    done = _stream_done(client, started.json()["id"])

    assert started.status_code == 202
    assert started.json()["targets"] == ["ready"]
    assert checked == [("ready",)]
    assert captured["profiles"] == ("ready",)
    assert done["status"] == JobStatus.PARTIAL.value
    assert done["results"] == [
        {"kvm": "ready", "ok": True, "text": "away"},
        {"kvm": "unconfigured", "ok": False, "text": "missing password"},
        {
            "kvm": "terminal_totp",
            "ok": False,
            "text": (
                "Interactive TOTP cannot run from the dashboard. Enroll this profile in "
                "Keychain first."
            ),
        },
    ]


def test_a_second_run_against_a_busy_kvm_is_refused(
    client: TestClient,
    profiles: tuple[KvmProfile, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()

    def fake_work(targets: tuple[str, ...], desired: object) -> object:
        def run(emit: object) -> JobOutcome:
            release.wait(timeout=5)
            return JobOutcome(ok=True, summary="done")

        return run

    monkeypatch.setattr(operations, "availability_work", fake_work)

    first = client.post("/api/availability", json={"kvm": "heidrick"})
    assert first.status_code == 202
    try:
        second = client.post("/api/availability", json={"kvm": "heidrick"})
        assert second.status_code == 409
        assert "already running" in second.json()["detail"]

        # A different KVM is independent and still accepted.
        assert client.post("/api/availability", json={"kvm": "nbc_kvm"}).status_code == 202
    finally:
        release.set()


def test_screenshot_returns_an_uncached_jpeg(
    client: TestClient,
    profiles: tuple[KvmProfile, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        operations,
        "capture_screenshot",
        lambda name: (b"\xff\xd8\xff-fake-jpeg", 1920, 1080),
    )

    response = client.get("/api/kvms/heidrick/screenshot")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-screen-width"] == "1920"
    assert response.content == b"\xff\xd8\xff-fake-jpeg"


def test_screenshot_surfaces_a_sanitized_transport_failure(
    client: TestClient,
    profiles: tuple[KvmProfile, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(name: str) -> tuple[bytes, int, int]:
        raise PiKVMTotpError("No TOTP credential is configured in macOS Keychain.")

    monkeypatch.setattr(operations, "capture_screenshot", explode)

    response = client.get("/api/kvms/heidrick/screenshot")

    assert response.status_code == 502
    assert "Keychain" in response.json()["detail"]


def test_screenshot_rejects_an_unknown_profile(
    client: TestClient,
    profiles: tuple[KvmProfile, ...],
) -> None:
    assert client.get("/api/kvms/other/screenshot").status_code == 404


def test_unknown_jobs_are_not_found(client: TestClient) -> None:
    assert client.get("/api/jobs/missing").status_code == 404
    assert client.get("/api/jobs/missing/events").status_code == 404


def test_jobs_lists_newest_first_and_honors_its_bound(client: TestClient) -> None:
    manager: JobManager = client.app.state.jobs
    first = manager.start(
        kind=JobKind.AVAILABILITY_GET,
        target="heidrick",
        keys=("heidrick",),
        work=lambda emit: JobOutcome(ok=True, summary="first"),
    )
    second = manager.start(
        kind=JobKind.AVAILABILITY_GET,
        target="nbc_kvm",
        keys=("nbc_kvm",),
        work=lambda emit: JobOutcome(ok=True, summary="second"),
    )

    response = client.get("/api/jobs", params={"limit": 1})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert [job["id"] for job in response.json()] == [second.id]
    assert response.json()[0]["targets"] == ["nbc_kvm"]
    assert client.get("/api/jobs", params={"limit": 51}).status_code == 422
    assert first.id != second.id


def test_job_event_cursor_does_not_replay_earlier_trace_lines(client: TestClient) -> None:
    manager: JobManager = client.app.state.jobs

    def work(emit: object) -> JobOutcome:
        assert callable(emit)
        emit("first")
        emit("second")
        return JobOutcome(ok=True, summary="done")

    started = manager.start(
        kind=JobKind.AVAILABILITY_GET,
        target="heidrick",
        keys=("heidrick",),
        work=work,
    )
    for _ in range(200):
        if manager.snapshot(started.id).status is not JobStatus.RUNNING:
            break
        threading.Event().wait(0.01)

    seen: list[str] = []
    with client.stream("GET", f"/api/jobs/{started.id}/events?after=1") as stream:
        event = "message"
        for line in stream.iter_lines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and event == "event":
                seen.append(json.loads(line.split(":", 1)[1].strip())["text"])

    assert seen == ["second"]


def test_dashboard_refuses_a_non_loopback_bind() -> None:
    args = argparse.Namespace(host="0.0.0.0", port=8787, no_browser=True)

    with pytest.raises(DashboardError, match="loopback"):
        execute_dashboard_command(args)


def test_job_manager_records_a_workflow_exception_as_a_failed_job() -> None:
    manager = JobManager()

    def explode(emit: object) -> JobOutcome:
        raise RuntimeError("controller blew up")

    snapshot = manager.start(
        kind=JobKind.AVAILABILITY_GET,
        target="heidrick",
        keys=("heidrick",),
        work=explode,
    )
    for _ in range(200):
        current = manager.snapshot(snapshot.id)
        if current.status is not JobStatus.RUNNING:
            break
        threading.Event().wait(0.02)

    current = manager.snapshot(snapshot.id)
    assert current.status is JobStatus.FAILED
    assert current.error == "controller blew up"
    # The reservation is released even when the workflow raised.
    assert manager.busy_targets() == {}


def test_job_manager_does_not_succeed_when_every_result_failed() -> None:
    manager = JobManager()
    snapshot = manager.start(
        kind=JobKind.AVAILABILITY_GET,
        target="all KVMs",
        keys=(),
        work=lambda emit: JobOutcome(
            ok=True,
            summary="contradictory worker result",
            results=(JobResultLine(kvm="one", ok=False, text="stopped"),),
        ),
    )
    for _ in range(200):
        current = manager.snapshot(snapshot.id)
        if current.status is not JobStatus.RUNNING:
            break
        threading.Event().wait(0.01)

    assert manager.snapshot(snapshot.id).status is JobStatus.FAILED


def test_job_manager_reserves_each_target_once() -> None:
    manager = JobManager()
    release = threading.Event()

    manager.start(
        kind=JobKind.AVAILABILITY_GET,
        target="all KVMs",
        keys=("one", "two"),
        work=lambda emit: (release.wait(timeout=5), JobOutcome(ok=True, summary="done"))[1],
    )
    try:
        with pytest.raises(JobConflictError, match="two"):
            manager.start(
                kind=JobKind.AVAILABILITY_GET,
                target="two",
                keys=("two",),
                work=lambda emit: JobOutcome(ok=True, summary="done"),
            )
    finally:
        release.set()


# --------------------------------------------------------------------------------------------
# Cancellation, pruned-job streams, thread-start failures, fleet status, host binding
# --------------------------------------------------------------------------------------------


def _wait_until_finished(manager: JobManager, job_id: str) -> JobStatus:
    for _ in range(300):
        current = manager.snapshot(job_id)
        if current.status is not JobStatus.RUNNING:
            return current.status
        threading.Event().wait(0.02)
    return manager.snapshot(job_id).status


def test_cancel_stops_a_schedule_job_during_its_retry_wait(
    client: TestClient,
    profiles: tuple[KvmProfile, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_execute(args: object, **kwargs: object) -> tuple[str, int]:
        sleeper = kwargs["sleeper"]
        assert callable(sleeper)
        try:
            sleeper(5.0)  # raises RetryWaitInterrupted when cancelled
        except RetryWaitInterrupted:
            return "Desired Slack availability: away (Asia/Karachi)\nheidrick  ✗ unreachable", 1
        return "Desired Slack availability: away (Asia/Karachi)\nheidrick  ✗ unreachable", 1

    monkeypatch.setattr(operations, "execute_schedule_command", fake_execute)
    monkeypatch.setattr(
        operations,
        "default_slack_availability_service",
        lambda *, trace_output=None: object(),
    )

    started = client.post("/api/schedule/actions", json={"action": "reconcile"}).json()
    assert started["cancellable"] is True
    assert started["cancel_requested"] is False

    cancelled = client.post(f"/api/jobs/{started['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["cancel_requested"] is True

    manager: JobManager = client.app.state.jobs
    assert _wait_until_finished(manager, started["id"]) is JobStatus.CANCELLED
    final = client.get(f"/api/jobs/{started['id']}").json()
    assert final["status"] == "cancelled"
    assert "Cancelled" in final["summary"]
    assert any(line == "Cancelled; skipping the retry." for line in final["events"])
    assert manager.busy_targets() == {}


def test_cancel_is_refused_for_jobs_that_cannot_be_interrupted(
    client: TestClient,
    profiles: tuple[KvmProfile, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()

    def fake_work(targets: tuple[str, ...], desired: object) -> object:
        return lambda emit: (release.wait(timeout=5), JobOutcome(ok=True, summary="done"))[1]

    monkeypatch.setattr(operations, "availability_work", fake_work)
    started = client.post("/api/availability", json={"kvm": "heidrick"}).json()
    try:
        assert started["cancellable"] is False
        refused = client.post(f"/api/jobs/{started['id']}/cancel")
        assert refused.status_code == 409
        assert client.post("/api/jobs/nope/cancel").status_code == 404
    finally:
        release.set()


def test_pruned_job_stream_ends_with_a_gone_event(client: TestClient) -> None:
    manager: JobManager = client.app.state.jobs
    job_id: dict[str, str] = {}

    def vanish(emit: object) -> JobOutcome:
        # Wait until the stream is polling, then simulate retention pruning of this job.
        threading.Event().wait(0.5)
        with manager._lock:
            del manager._jobs[job_id["id"]]
        threading.Event().wait(0.6)
        return JobOutcome(ok=True, summary="done")

    snapshot = manager.start(
        kind=JobKind.AVAILABILITY_GET, target="heidrick", keys=("heidrick",), work=vanish
    )
    job_id["id"] = snapshot.id

    with client.stream("GET", f"/api/jobs/{snapshot.id}/events") as stream:
        assert stream.status_code == 200
        frames: list[str] = []
        for line in stream.iter_lines():
            if line.startswith("event:"):
                frames.append(line.split(":", 1)[1].strip())
            elif line.startswith("data:"):
                frames.append(json.loads(line.split(":", 1)[1].strip())["text"])
    assert frames == ["gone", "This job is no longer available."]


def test_stream_for_an_already_pruned_job_is_not_found(client: TestClient) -> None:
    manager: JobManager = client.app.state.jobs
    release = threading.Event()
    snapshot = manager.start(
        kind=JobKind.AVAILABILITY_GET,
        target="heidrick",
        keys=("heidrick",),
        work=lambda emit: (release.wait(timeout=5), JobOutcome(ok=True, summary="done"))[1],
    )
    with manager._lock:
        del manager._jobs[snapshot.id]
    try:
        # A job unknown at stream start is a plain 404; one that vanishes mid-stream is "gone".
        assert client.get(f"/api/jobs/{snapshot.id}/events").status_code == 404
    finally:
        release.set()


def test_job_manager_releases_targets_when_the_worker_thread_cannot_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = JobManager()

    def refuse(self: threading.Thread) -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", refuse)
    snapshot = manager.start(
        kind=JobKind.AVAILABILITY_GET,
        target="heidrick",
        keys=("heidrick",),
        work=lambda emit: JobOutcome(ok=True, summary="done"),
    )
    assert snapshot.status is JobStatus.FAILED
    assert snapshot.error is not None and "worker thread" in snapshot.error
    assert manager.busy_targets() == {}
    monkeypatch.undo()

    # The same KVM is immediately usable again.
    retry = manager.start(
        kind=JobKind.AVAILABILITY_GET,
        target="heidrick",
        keys=("heidrick",),
        work=lambda emit: JobOutcome(ok=True, summary="done"),
    )
    assert _wait_until_finished(manager, retry.id) is JobStatus.SUCCEEDED


def test_schedule_endpoint_reports_per_kvm_reachability_without_credentials(
    client: TestClient,
    profiles: tuple[KvmProfile, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probed: list[str] = []

    def prober(base_url: str, verify_ssl: bool) -> fleet.ProbeResult:
        probed.append(base_url)
        return fleet.ProbeResult(
            reachable=base_url.startswith("https://nbc_kvm"),
            detail="HTTP 401" if base_url.startswith("https://nbc_kvm") else "connect timeout",
            checked_at=datetime.now(UTC),
        )

    client.app.state.reachability = fleet.ReachabilityCache(prober=prober)
    _write_log(
        Path(client.app.state.log_path),
        [
            _entry(minutes_ago=90, outcome="failure", observed="unknown", changed=None)
            | {"stop_code": "pikvm_unreachable", "telemetry": {"steps": 0, "total_tokens": 0}},
            _entry(minutes_ago=30, outcome="failure", observed="unknown", changed=None)
            | {"stop_code": "pikvm_unreachable"},
            _entry(minutes_ago=5, kvm="nbc_kvm"),
        ],
    )
    build = operations.schedule_snapshot
    monkeypatch.setattr(
        operations,
        "schedule_snapshot",
        lambda **kwargs: build(
            manager=_StubManager(),  # type: ignore[arg-type]
            state_store=ReconciliationStateStore(Path(client.app.state.state_path)),
            **kwargs,
        ),
    )

    payload = client.get("/api/schedule").json()

    assert sorted(probed) == ["https://heidrick.example.local", "https://nbc_kvm.example.local"]
    text = json.dumps(payload)
    assert "password" not in text.lower() and "token" not in text.lower()
    statuses = {item["name"]: item for item in payload["kvms"]}
    assert statuses["heidrick"]["reachable"] is False
    assert statuses["heidrick"]["reachability_detail"] == "connect timeout"
    assert statuses["heidrick"]["consecutive_failures"] == 2
    assert statuses["heidrick"]["alert"]["label"] == "PiKVM unreachable"
    assert statuses["heidrick"]["unreachable_since"] is not None
    assert statuses["nbc_kvm"]["reachable"] is True
    assert statuses["nbc_kvm"]["alert"] is None
    assert payload["last_run"]["kvm"] == "nbc_kvm"

    # A second read within the cache window does not probe again.
    client.get("/api/schedule")
    assert len(probed) == 2


class _StubManager:
    def status(self) -> tuple[object, ...]:
        return ()

    def health(self) -> object:
        from work_agent.schedule.launchd import ScheduleHealth

        return ScheduleHealth(
            interpreter=None,
            working_directory=None,
            interpreter_can_run=False,
            timezone_name="Asia/Karachi",
            timezone_ok=True,
            problems=(),
        )


def test_history_reports_telemetry_and_does_not_count_lock_busy_as_failure(
    client: TestClient,
) -> None:
    _write_log(
        Path(client.app.state.log_path),
        [
            _entry(minutes_ago=40)
            | {
                "telemetry": {
                    "sessions": 1,
                    "steps": 4,
                    "hid_actions": 2,
                    "vision_calls": 3,
                    "planner_calls": 3,
                    "total_tokens": 4321,
                    "runtime_seconds": 41.2,
                }
            },
            _entry(minutes_ago=30, outcome="failure", observed="unknown", changed=None)
            | {"stop_code": "lock_busy", "error": "another workflow held the PiKVM"},
            _entry(minutes_ago=20, outcome="failure", observed="unknown", changed=None)
            | {"stop_code": "stuck_no_screen_change", "error": "no change"},
            _entry(minutes_ago=10, outcome="failure", observed="unknown", changed=None)
            | {"telemetry": "not-an-object"},
        ],
    )

    payload = client.get("/api/history", params={"days": 0}).json()

    summary = payload["summary"]
    assert summary["total"] == 4
    assert summary["skipped"] == 1
    assert summary["failure"] == 2
    assert summary["success_rate"] == pytest.approx(1 / 3)
    kvm = summary["per_kvm"][0]
    assert kvm["skipped"] == 1
    assert kvm["consecutive_failures"] == 2
    assert kvm["success_rate"] == pytest.approx(1 / 3)
    by_minute = {record["error"]: record for record in payload["records"]}
    telemetry = payload["records"][-1]["telemetry"]
    assert telemetry == {
        "sessions": 1,
        "steps": 4,
        "hid_actions": 2,
        "vision_calls": 3,
        "planner_calls": 3,
        "total_tokens": 4321,
        "runtime_seconds": 41.2,
    }
    assert payload["records"][0]["telemetry"] is None
    assert by_minute["another workflow held the PiKVM"]["stop_code"] == "lock_busy"


def test_dashboard_binds_before_opening_the_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    served: dict[str, object] = {}

    class _FakeServer:
        def __init__(self, config: uvicorn.Config) -> None:
            served["host"] = config.host
            served["port"] = config.port

        def run(self, sockets: list[socket.socket] | None = None) -> None:
            assert sockets is not None and len(sockets) == 1
            served["bound"] = sockets[0].getsockname()[:2]

    monkeypatch.setattr(uvicorn, "Server", _FakeServer)
    opened: list[str] = []

    class _ImmediateTimer:
        def __init__(self, delay: float, function: object) -> None:
            self._function = function

        def start(self) -> None:
            self._function()  # type: ignore[operator]

    monkeypatch.setattr(dashboard_cli.threading, "Timer", _ImmediateTimer)

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    args = argparse.Namespace(host="localhost", port=port, no_browser=False)
    assert execute_dashboard_command(args, open_browser=opened.append) == 0
    assert opened == [f"http://127.0.0.1:{port}/"]
    assert served["bound"] == ("127.0.0.1", port)


def test_dashboard_reports_a_taken_port_without_opening_the_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    opened: list[str] = []
    try:
        args = argparse.Namespace(host="127.0.0.1", port=port, no_browser=False)
        with pytest.raises(DashboardError, match="could not bind"):
            execute_dashboard_command(args, open_browser=opened.append)
    finally:
        holder.close()
    assert opened == []


@pytest.mark.parametrize(
    ("host", "expected"),
    [("localhost", "127.0.0.1"), ("127.0.0.7", "127.0.0.7"), ("::1", "::1"), ("[::1]", "::1")],
)
def test_dashboard_accepts_any_loopback_host(host: str, expected: str) -> None:
    assert dashboard_cli._validate_host(host) == expected


def test_dashboard_url_brackets_ipv6() -> None:
    assert dashboard_cli.dashboard_url("::1", 8787) == "http://[::1]:8787/"
    assert dashboard_cli.dashboard_url("127.0.0.1", 8787) == "http://127.0.0.1:8787/"


class _FakeProfileService:
    """Stands in for ProfileService: same surface, no Keychain, no PiKVM."""

    def __init__(self) -> None:
        from work_agent.pikvm.profile_service import ConnectionTest, ProfileView

        self._view = ProfileView
        self._test = ConnectionTest
        self.views: dict[str, ProfileView] = {
            "heidrick": ProfileView(
                name="heidrick",
                host="100.94.8.25",
                url="https://100.94.8.25",
                username="admin",
                source="env",
                enabled=True,
                totp_required=True,
                totp_enrolled=False,
                verify_ssl=False,
                removable=False,
            )
        }
        self.enrolled: list[tuple[str, bytes, bool]] = []

    def list_profiles(self) -> list[object]:
        return list(self.views.values())

    def get(self, name: str) -> object:
        return self.views[name]

    def add(self, **fields: object) -> object:
        from work_agent.pikvm.errors import PiKVMConfigurationError

        if fields["name"] in self.views:
            raise PiKVMConfigurationError("Managed profile already exists.")
        view = self._view(
            name=str(fields["name"]),
            host="lab.example",
            url=str(fields["url"]),
            username=str(fields["username"]),
            source="managed",
            enabled=True,
            totp_required=bool(fields["totp_required"]),
            totp_enrolled=False if fields["totp_required"] else None,
            verify_ssl=bool(fields["verify_ssl"]),
            removable=True,
        )
        self.views[view.name] = view
        return view

    def remove(self, name: str) -> list[str]:
        from work_agent.pikvm.errors import PiKVMConfigurationError

        if name not in self.views:
            raise PiKVMConfigurationError(f"Unknown PiKVM profile {name!r}.")
        del self.views[name]
        return [f"Removed managed profile {name!r}.", "Removed its password from macOS Keychain."]

    def set_enabled(self, name: str, enabled: bool) -> object:
        from dataclasses import replace

        self.views[name] = replace(self.views[name], enabled=enabled)
        return self.views[name]

    def test_connection(self, name: str) -> object:
        return self._test(ok=True, message="Captured a 1920x1080 screen.", screen_width=1920)

    def enroll_totp_from_image(
        self, name: str, image: bytes, *, replace_existing: bool
    ) -> list[str]:
        from dataclasses import replace

        self.enrolled.append((name, image, replace_existing))
        self.views[name] = replace(self.views[name], totp_enrolled=True)
        return ["TOTP QR decoded locally.", "Verified."]


def test_profile_endpoints_manage_profiles_without_exposing_secrets(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeProfileService()
    monkeypatch.setattr(operations, "profile_service", lambda: fake)

    listed = client.get("/api/profiles")
    assert listed.status_code == 200
    assert [item["name"] for item in listed.json()["profiles"]] == ["heidrick"]
    assert "password" not in listed.text

    created = client.post(
        "/api/profiles",
        json={
            "name": "lab",
            "url": "https://lab.example",
            "username": "op",
            "password": "typed-secret",
            "totp_required": True,
            "verify_ssl": False,
        },
    )
    assert created.status_code == 201
    assert created.json()["profile"]["removable"] is True
    assert "typed-secret" not in created.text
    assert "enroll" in created.json()["message"]

    disabled = client.post("/api/profiles/heidrick/disable")
    assert disabled.status_code == 200 and disabled.json()["profile"]["enabled"] is False
    assert client.post("/api/profiles/heidrick/enable").json()["profile"]["enabled"] is True

    tested = client.post("/api/profiles/lab/test")
    assert tested.status_code == 200 and tested.json()["ok"] is True

    enrolled = client.post(
        "/api/profiles/lab/totp?replace=true",
        content=b"fake-png-bytes",
        headers={"Content-Type": "image/png"},
    )
    assert enrolled.status_code == 200
    assert enrolled.json()["profile"]["totp_enrolled"] is True
    assert fake.enrolled == [("lab", b"fake-png-bytes", True)]
    assert client.post("/api/profiles/lab/totp", content=b"").status_code == 400

    removed = client.delete("/api/profiles/lab")
    assert removed.status_code == 200 and "Removed managed profile" in removed.json()["message"]
    assert client.delete("/api/profiles/lab").status_code == 404
    assert client.post("/api/profiles/lab/bogus").status_code == 404


def test_profile_endpoints_require_the_token(tmp_path: Path) -> None:
    app = create_app(token=TOKEN)
    anonymous = TestClient(app, base_url=_BASE)
    assert anonymous.get("/api/profiles").status_code == 403
    assert anonymous.post("/api/profiles/heidrick/disable").status_code == 403
