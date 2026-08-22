from __future__ import annotations

import asyncio
import ipaddress
import json
import secrets
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from work_agent.dashboard import operations
from work_agent.dashboard.fleet import ReachabilityCache
from work_agent.dashboard.history import read_history
from work_agent.dashboard.jobs import JobConflictError, JobManager, Work
from work_agent.dashboard.models import (
    ALL_KVMS,
    AgendaRequest,
    AvailabilityRequest,
    DashboardConfig,
    HistoryResponse,
    JobKind,
    JobResultLine,
    JobSnapshot,
    JobStatus,
    MeetingAbandonRequest,
    MeetingSessionDetailCard,
    MeetingsSnapshot,
    MeetingStartRequest,
    ProfileActionResult,
    ProfileCard,
    ProfileCreateRequest,
    ScheduleAction,
    ScheduleActionRequest,
    ScheduleSnapshot,
    TriageRequest,
)
from work_agent.meeting.errors import MeetingError
from work_agent.pikvm import PiKVMError
from work_agent.schedule.errors import ScheduleError
from work_agent.schedule.state import ReconciliationStateStore
from work_agent.slack.logging import JsonlAvailabilityLogger

TOKEN_HEADER = "X-Dashboard-Token"
_STATIC_DIR = Path(__file__).parent / "static"
_SSE_POLL_SECONDS = 0.2
_SSE_IDLE_TIMEOUT_SECONDS = 900.0
_MAX_LISTED_JOBS = 50
_CAPABILITIES = [
    "history",
    "schedule",
    "availability",
    "triage",
    "agenda",
    "screenshot",
    "kvm_status",
    "job_cancel",
    "profiles",
    "meetings",
]
_MEETING_JOB_KEY = "__meeting__"
_MAX_QR_IMAGE_BYTES = 8 * 1024 * 1024


def _host_is_loopback(header: str | None) -> bool:
    """Accept only `localhost` or a literal loopback IP (any 127/8 address, or ::1) as Host."""

    if not header:
        return False
    host = header.strip()
    if host.startswith("["):
        closing = host.find("]")
        if closing == -1:
            return False
        host = host[1:closing]
    elif host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _sse(event: str, payload: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=True)}\n\n"


def create_app(*, token: str | None = None) -> FastAPI:
    app = FastAPI(
        title="PiKVM Work Agent dashboard",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.token = token or secrets.token_urlsafe(32)
    app.state.jobs = JobManager()
    app.state.log_path = JsonlAvailabilityLogger().path
    app.state.state_path = ReconciliationStateStore().path
    # Unauthenticated per-endpoint reachability probes, cached for about a minute. Tests
    # replace this with a stub so nothing touches the network.
    app.state.reachability = ReachabilityCache()

    @app.middleware("http")
    async def guard_origin(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # A localhost server is still reachable from any page the browser loads, so refuse
        # non-loopback Host headers: DNS rebinding must not be able to drive real HID workflows.
        if not _host_is_loopback(request.headers.get("host")):
            return Response("This dashboard only serves loopback requests.", status_code=421)
        response = await call_next(request)
        if request.url.path.startswith(("/api/", "/static/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    def require_token(request: Request) -> None:
        supplied = request.headers.get(TOKEN_HEADER, "")
        if not secrets.compare_digest(supplied, str(request.app.state.token)):
            raise HTTPException(status_code=403, detail="A valid dashboard token is required.")

    guarded = [Depends(require_token)]

    def start_job(
        request: Request,
        *,
        kind: JobKind,
        target: str,
        keys: tuple[str, ...],
        work: Work,
        cancel: threading.Event | None = None,
    ) -> JobSnapshot:
        manager: JobManager = request.app.state.jobs
        try:
            return manager.start(kind=kind, target=target, keys=keys, work=work, cancel=cancel)
        except JobConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    def profile_names() -> list[str]:
        return [profile.name for profile in operations.profile_snapshots()]

    def workflow_targets(
        requested: str,
        *,
        empty_message: str,
    ) -> tuple[tuple[str, ...], tuple[JobResultLine, ...]]:
        profiles = operations.profile_snapshots()
        if not profiles:
            raise HTTPException(status_code=400, detail=empty_message)
        by_name = {profile.name: profile for profile in profiles}
        if requested == ALL_KVMS:
            selected = profiles
        else:
            profile = by_name.get(requested)
            if profile is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Unknown PiKVM profile {requested!r}.",
                )
            selected = [profile]

        runnable: list[str] = []
        rejected: list[JobResultLine] = []
        for profile in selected:
            problem: str | None = None
            if not profile.configured:
                problem = profile.problem or "Configuration is incomplete."
            elif profile.interactive_totp:
                problem = (
                    "Interactive TOTP cannot run from the dashboard. Enroll this profile in "
                    "Keychain first."
                )
            else:
                try:
                    operations.ensure_runnable((profile.name,))
                except (PiKVMError, OSError, ValueError) as exc:
                    problem = str(exc)
            if problem is None:
                runnable.append(profile.name)
            else:
                rejected.append(JobResultLine(kvm=profile.name, ok=False, text=problem))

        if requested != ALL_KVMS and rejected:
            raise HTTPException(status_code=400, detail=rejected[0].text)
        return tuple(runnable), tuple(rejected)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
        html = html.replace("__DASHBOARD_TOKEN__", str(request.app.state.token))
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/favicon.svg")
    def favicon() -> FileResponse:
        return FileResponse(_STATIC_DIR / "favicon.svg", media_type="image/svg+xml")

    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    @app.get("/api/config", dependencies=guarded)
    def config(request: Request) -> DashboardConfig:
        profiles = operations.profile_snapshots()
        return DashboardConfig(
            capabilities=list(_CAPABILITIES),
            kvms=profiles,
            default_kvm=operations.default_profile(profiles),
            log_path=str(request.app.state.log_path),
            state_path=str(request.app.state.state_path),
            timezone="Asia/Karachi",
        )

    @app.get("/api/schedule", dependencies=guarded)
    def schedule(request: Request) -> ScheduleSnapshot:
        try:
            return operations.schedule_snapshot(
                profiles=operations.profile_snapshots(),
                log_path=Path(request.app.state.log_path),
                reachability=request.app.state.reachability,
            )
        except (ScheduleError, OSError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from None

    @app.get("/api/history", dependencies=guarded)
    def history(
        request: Request,
        kvm: Annotated[str | None, Query(max_length=64)] = None,
        days: Annotated[int, Query(ge=0, le=3650)] = 0,
        limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    ) -> HistoryResponse:
        selected = None if kvm in {None, "", ALL_KVMS} else kvm
        since = datetime.now(UTC) - timedelta(days=days) if days else None
        return read_history(
            request.app.state.log_path,
            kvm=selected,
            since=since,
            limit=limit,
        )

    @app.post("/api/availability", dependencies=guarded, status_code=202)
    def start_availability(
        request: Request,
        payload: Annotated[AvailabilityRequest, Body()],
    ) -> JobSnapshot:
        targets, rejected = workflow_targets(
            payload.kvm,
            empty_message="Slack workflows require at least one name in PIKVM_PROFILES.",
        )
        work = (
            operations.availability_work(
                targets,
                payload.availability,
                preflight_results=rejected,
            )
            if rejected
            else operations.availability_work(targets, payload.availability)
        )

        return start_job(
            request,
            kind=(
                JobKind.AVAILABILITY_GET
                if payload.availability is None
                else JobKind.AVAILABILITY_SET
            ),
            target="all KVMs" if payload.kvm == ALL_KVMS else payload.kvm,
            keys=targets,
            work=work,
        )

    @app.post("/api/triage", dependencies=guarded, status_code=202)
    def start_triage(
        request: Request,
        payload: Annotated[TriageRequest, Body()],
    ) -> JobSnapshot:
        targets, rejected = workflow_targets(
            payload.kvm,
            empty_message="Slack triage requires at least one name in PIKVM_PROFILES.",
        )
        work = (
            operations.triage_work(targets, preflight_results=rejected)
            if rejected
            else operations.triage_work(targets)
        )

        return start_job(
            request,
            kind=JobKind.TRIAGE,
            target="all KVMs" if payload.kvm == ALL_KVMS else payload.kvm,
            keys=targets,
            work=work,
        )

    @app.post("/api/agenda", dependencies=guarded, status_code=202)
    def start_agenda(
        request: Request,
        payload: Annotated[AgendaRequest, Body()],
    ) -> JobSnapshot:
        targets, rejected = workflow_targets(
            payload.kvm,
            empty_message="Reading a calendar requires at least one name in PIKVM_PROFILES.",
        )
        work = (
            operations.agenda_work(targets, preflight_results=rejected)
            if rejected
            else operations.agenda_work(targets)
        )

        return start_job(
            request,
            kind=JobKind.AGENDA,
            target="all KVMs" if payload.kvm == ALL_KVMS else payload.kvm,
            keys=targets,
            work=work,
        )

    @app.post("/api/schedule/actions", dependencies=guarded, status_code=202)
    def start_schedule_action(
        request: Request,
        payload: Annotated[ScheduleActionRequest, Body()],
    ) -> JobSnapshot:
        kinds = {
            ScheduleAction.RUN_NOW: JobKind.SCHEDULE_RUN_NOW,
            ScheduleAction.RECONCILE: JobKind.SCHEDULE_RECONCILE,
            ScheduleAction.INSTALL: JobKind.SCHEDULE_INSTALL,
            ScheduleAction.UNINSTALL: JobKind.SCHEDULE_UNINSTALL,
        }
        touches_kvms = payload.action in {ScheduleAction.RUN_NOW, ScheduleAction.RECONCILE}
        profiles: tuple[str, ...] = ()
        rejected: tuple[JobResultLine, ...] = ()
        if touches_kvms:
            profiles, rejected = workflow_targets(
                ALL_KVMS,
                empty_message="Scheduling requires at least one name in PIKVM_PROFILES.",
            )
        cancel = threading.Event() if touches_kvms else None
        work = (
            operations.schedule_work(
                payload.action,
                payload.availability,
                targets=profiles,
                preflight_results=rejected,
                cancel=cancel,
            )
            if touches_kvms
            else operations.schedule_work(payload.action, payload.availability)
        )

        return start_job(
            request,
            kind=kinds[payload.action],
            target=f"schedule {payload.action.value}",
            keys=profiles if touches_kvms else ("__schedule__",),
            work=work,
            cancel=cancel,
        )

    @app.get("/api/jobs", dependencies=guarded)
    def jobs(
        request: Request,
        response: Response,
        limit: Annotated[int, Query(ge=1, le=_MAX_LISTED_JOBS)] = 20,
    ) -> list[JobSnapshot]:
        response.headers["Cache-Control"] = "no-store"
        manager: JobManager = request.app.state.jobs
        return manager.snapshots(limit=limit)

    @app.get("/api/jobs/{job_id}", dependencies=guarded)
    def job(request: Request, response: Response, job_id: str) -> JobSnapshot:
        response.headers["Cache-Control"] = "no-store"
        manager: JobManager = request.app.state.jobs
        try:
            return manager.snapshot(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown job.") from None

    @app.post("/api/jobs/{job_id}/cancel", dependencies=guarded)
    def cancel_job(request: Request, response: Response, job_id: str) -> JobSnapshot:
        response.headers["Cache-Control"] = "no-store"
        manager: JobManager = request.app.state.jobs
        try:
            snapshot = manager.cancel(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown job.") from None
        if snapshot.status is JobStatus.RUNNING and not snapshot.cancellable:
            raise HTTPException(
                status_code=409,
                detail="This job cannot be interrupted; it stops when its workflow finishes.",
            )
        return snapshot

    @app.get("/api/jobs/{job_id}/events", dependencies=guarded)
    async def job_events(
        request: Request,
        job_id: str,
        after: Annotated[int, Query(ge=0)] = 0,
    ) -> StreamingResponse:
        manager: JobManager = request.app.state.jobs
        try:
            manager.snapshot(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown job.") from None

        async def stream() -> AsyncIterator[str]:
            index = after
            idle = 0.0
            while True:
                if await request.is_disconnected():
                    return
                try:
                    events, status = manager.events_since(job_id, index)
                    if status is not JobStatus.RUNNING:
                        final = json.loads(manager.snapshot(job_id).model_dump_json())
                    else:
                        final = None
                except KeyError:
                    # The job was pruned while this stream was open; end cleanly rather than
                    # leaving the page with a broken stream and no explanation.
                    yield _sse("gone", {"text": "This job is no longer available."})
                    return
                index += len(events)
                for event in events:
                    yield _sse("event", {"text": event})
                if final is not None:
                    yield _sse("done", final)
                    return
                idle = 0.0 if events else idle + _SSE_POLL_SECONDS
                if idle >= _SSE_IDLE_TIMEOUT_SECONDS:
                    yield _sse("timeout", {"text": "Event stream idle timeout."})
                    return
                await asyncio.sleep(_SSE_POLL_SECONDS)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    def profile_error(exc: Exception) -> HTTPException:
        message = str(exc)
        status = 404 if "Unknown" in message else 400
        return HTTPException(status_code=status, detail=message)

    @app.get("/api/profiles", dependencies=guarded)
    def list_profiles() -> dict[str, list[ProfileCard]]:
        service = operations.profile_service()
        try:
            cards = [operations.profile_card(view) for view in service.list_profiles()]
        except (PiKVMError, OSError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from None
        return {"profiles": cards}

    @app.post("/api/profiles", dependencies=guarded, status_code=201)
    def add_profile(payload: ProfileCreateRequest) -> ProfileActionResult:
        service = operations.profile_service()
        try:
            view = service.add(
                name=payload.name,
                url=payload.url,
                username=payload.username,
                password=payload.password,
                totp_required=payload.totp_required,
                verify_ssl=payload.verify_ssl,
            )
        except (PiKVMError, OSError, ValueError) as exc:
            raise profile_error(exc) from None
        card = operations.profile_card(view)
        message = f"Added profile {card.name}."
        if card.totp_required and not card.totp_enrolled:
            message += " It requires 2FA: enroll its provisioning QR next."
        return ProfileActionResult(ok=True, message=message, profile=card)

    @app.post("/api/profiles/{name}/{action}", dependencies=guarded)
    async def profile_action(name: str, action: str, request: Request) -> ProfileActionResult:
        service = operations.profile_service()
        try:
            if action in {"enable", "disable"}:
                view = service.set_enabled(name, action == "enable")
                card = operations.profile_card(view)
                state = "enabled" if card.enabled else "disabled"
                return ProfileActionResult(
                    ok=True, message=f"Profile {card.name} is now {state}.", profile=card
                )
            if action == "test":
                result = await asyncio.to_thread(service.test_connection, name)
                return ProfileActionResult(
                    ok=result.ok,
                    message=result.message,
                    screen_width=result.screen_width,
                    screen_height=result.screen_height,
                )
            if action == "totp":
                # Raw image bytes in the body; decoded in memory, never written to disk, and the
                # seed never leaves the server.
                image = await request.body()
                if not image:
                    raise HTTPException(status_code=400, detail="Upload a PNG or JPEG QR image.")
                if len(image) > _MAX_QR_IMAGE_BYTES:
                    raise HTTPException(status_code=413, detail="The QR image exceeds 8 MB.")
                replace = request.query_params.get("replace", "").lower() in {"1", "true", "yes"}
                notes = await asyncio.to_thread(
                    service.enroll_totp_from_image, name, image, replace_existing=replace
                )
                view = service.get(name)
                return ProfileActionResult(
                    ok=True,
                    message="2FA enrolled and verified.",
                    notes=notes,
                    profile=operations.profile_card(view),
                )
        except HTTPException:
            raise
        except (PiKVMError, OSError, ValueError) as exc:
            raise profile_error(exc) from None
        raise HTTPException(status_code=404, detail=f"Unknown profile action {action!r}.")

    @app.delete("/api/profiles/{name}", dependencies=guarded)
    def remove_profile(name: str) -> ProfileActionResult:
        service = operations.profile_service()
        try:
            notes = service.remove(name)
        except (PiKVMError, OSError, ValueError) as exc:
            raise profile_error(exc) from None
        return ProfileActionResult(ok=True, message=notes[0], notes=notes[1:])

    @app.get("/api/meetings", dependencies=guarded)
    def meetings() -> MeetingsSnapshot:
        try:
            return operations.meetings_snapshot(profile_names())
        except (MeetingError, PiKVMError, OSError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from None

    @app.post("/api/meetings/start", dependencies=guarded, status_code=202)
    def start_meeting(request: Request, payload: MeetingStartRequest) -> JobSnapshot:
        kvm = payload.kvm.strip().lower()
        if kvm not in profile_names():
            raise HTTPException(status_code=404, detail=f"Unknown PiKVM profile {kvm!r}.")
        operations.ensure_runnable((kvm,))
        return start_job(
            request,
            kind=JobKind.MEETING_START,
            target=kvm,
            keys=(_MEETING_JOB_KEY,),
            work=operations.meeting_start_work(kvm),
        )

    @app.post("/api/meetings/stop", dependencies=guarded, status_code=202)
    def stop_meeting(request: Request) -> JobSnapshot:
        return start_job(
            request,
            kind=JobKind.MEETING_STOP,
            target="meeting",
            keys=(_MEETING_JOB_KEY,),
            work=operations.meeting_stop_work(),
        )

    @app.post("/api/meetings/abandon", dependencies=guarded)
    def abandon_meeting(payload: MeetingAbandonRequest) -> dict[str, str]:
        try:
            result = operations.meeting_service().abandon(payload.session_id)
        except MeetingError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"session_id": result.session_id, "message": "Session released; artifacts kept."}

    @app.get("/api/meetings/sessions/{session_id}", dependencies=guarded)
    def meeting_session(session_id: str) -> MeetingSessionDetailCard:
        try:
            detail = operations.meeting_library().detail(session_id)
        except (MeetingError, OSError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from None
        if detail is None:
            raise HTTPException(status_code=404, detail="No such recorded session.")
        return operations.meeting_detail_card(detail)

    @app.get("/api/kvms/{name}/screenshot", dependencies=guarded)
    async def screenshot(name: str) -> Response:
        if name not in profile_names():
            raise HTTPException(status_code=404, detail=f"Unknown PiKVM profile {name!r}.")
        try:
            content, width, height = await asyncio.to_thread(operations.capture_screenshot, name)
        except (PiKVMError, OSError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None
        return Response(
            content,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "no-store",
                "X-Screen-Width": str(width),
                "X-Screen-Height": str(height),
            },
        )

    return app
