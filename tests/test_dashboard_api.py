from __future__ import annotations

import argparse
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from work_agent.dashboard import operations
from work_agent.dashboard.app import TOKEN_HEADER, create_app
from work_agent.dashboard.cli import execute_dashboard_command
from work_agent.dashboard.errors import DashboardError
from work_agent.dashboard.jobs import JobConflictError, JobManager, JobOutcome
from work_agent.dashboard.models import JobKind, JobResultLine, JobStatus, KvmProfile
from work_agent.pikvm import PiKVMTotpError

TOKEN = "test-dashboard-token"
_BASE = "http://127.0.0.1"


def _profile(name: str, *, configured: bool = True) -> KvmProfile:
    return KvmProfile(
        name=name,
        endpoint=f"https://{name}.example.local" if configured else None,
        totp_required=True,
        verify_ssl=False,
        interactive_totp=False,
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


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.1:8787", "localhost:8787", "[::1]:8787"])
def test_loopback_host_headers_are_accepted(
    client: TestClient,
    profiles: tuple[KvmProfile, ...],
    host: str,
) -> None:
    assert client.get("/api/config", headers={"Host": host}).status_code == 200


def test_index_embeds_the_session_token_and_is_not_cached(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert TOKEN in response.text
    assert "__DASHBOARD_TOKEN__" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_config_exposes_profiles_without_secrets(
    client: TestClient,
    profiles: tuple[KvmProfile, ...],
) -> None:
    payload = client.get("/api/config").json()

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
    assert seen["targets"] == ("heidrick", "nbc_kvm")


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
