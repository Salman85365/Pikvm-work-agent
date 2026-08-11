from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from work_agent.dashboard import operations
from work_agent.dashboard.history import read_history
from work_agent.dashboard.jobs import JobConflictError, JobManager, Work
from work_agent.dashboard.models import (
    ALL_KVMS,
    AvailabilityRequest,
    DashboardConfig,
    HistoryResponse,
    JobKind,
    JobSnapshot,
    JobStatus,
    ScheduleAction,
    ScheduleActionRequest,
    ScheduleSnapshot,
)
from work_agent.pikvm import PiKVMError
from work_agent.schedule.errors import ScheduleError
from work_agent.schedule.state import ReconciliationStateStore
from work_agent.slack.logging import JsonlAvailabilityLogger

TOKEN_HEADER = "X-Dashboard-Token"
_STATIC_DIR = Path(__file__).parent / "static"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]", "0:0:0:0:0:0:0:1"})
_SSE_POLL_SECONDS = 0.2
_SSE_IDLE_TIMEOUT_SECONDS = 900.0


def _host_is_loopback(header: str | None) -> bool:
    if not header:
        return False
    host = header.strip()
    if host.startswith("["):
        closing = host.find("]")
        host = host[: closing + 1] if closing != -1 else host
    elif ":" in host:
        host = host.rsplit(":", 1)[0]
    return host.lower() in _LOOPBACK_HOSTS


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

    @app.middleware("http")
    async def guard_origin(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # A localhost server is still reachable from any page the browser loads, so refuse
        # non-loopback Host headers: DNS rebinding must not be able to drive real HID workflows.
        if not _host_is_loopback(request.headers.get("host")):
            return Response("This dashboard only serves loopback requests.", status_code=421)
        return await call_next(request)

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
    ) -> JobSnapshot:
        manager: JobManager = request.app.state.jobs
        try:
            return manager.start(kind=kind, target=target, keys=keys, work=work)
        except JobConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    def profile_names() -> list[str]:
        return [profile.name for profile in operations.profile_snapshots()]

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
            kvms=profiles,
            default_kvm=operations.default_profile(profiles),
            log_path=str(request.app.state.log_path),
            state_path=str(request.app.state.state_path),
            timezone="Asia/Karachi",
        )

    @app.get("/api/schedule", dependencies=guarded)
    def schedule() -> ScheduleSnapshot:
        try:
            return operations.schedule_snapshot()
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
        profiles = profile_names()
        if not profiles:
            raise HTTPException(
                status_code=400,
                detail="Slack workflows require at least one name in PIKVM_PROFILES.",
            )
        if payload.kvm == ALL_KVMS:
            targets = tuple(profiles)
        elif payload.kvm in profiles:
            targets = (payload.kvm,)
        else:
            raise HTTPException(status_code=404, detail=f"Unknown PiKVM profile {payload.kvm!r}.")

        try:
            operations.ensure_runnable(targets)
        except (PiKVMError, OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

        return start_job(
            request,
            kind=(
                JobKind.AVAILABILITY_GET
                if payload.availability is None
                else JobKind.AVAILABILITY_SET
            ),
            target="all KVMs" if payload.kvm == ALL_KVMS else payload.kvm,
            keys=targets,
            work=operations.availability_work(targets, payload.availability),
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
        profiles = tuple(profile_names())
        if touches_kvms:
            if not profiles:
                raise HTTPException(
                    status_code=400,
                    detail="Scheduling requires at least one name in PIKVM_PROFILES.",
                )
            try:
                operations.ensure_runnable(profiles)
            except (PiKVMError, OSError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None

        return start_job(
            request,
            kind=kinds[payload.action],
            target=f"schedule {payload.action.value}",
            keys=profiles if touches_kvms else ("__schedule__",),
            work=operations.schedule_work(payload.action, payload.availability),
        )

    @app.get("/api/jobs/{job_id}", dependencies=guarded)
    def job(request: Request, job_id: str) -> JobSnapshot:
        manager: JobManager = request.app.state.jobs
        try:
            return manager.snapshot(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown job.") from None

    @app.get("/api/jobs/{job_id}/events", dependencies=guarded)
    async def job_events(request: Request, job_id: str) -> StreamingResponse:
        manager: JobManager = request.app.state.jobs
        try:
            manager.snapshot(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown job.") from None

        async def stream() -> AsyncIterator[str]:
            index = 0
            idle = 0.0
            while True:
                if await request.is_disconnected():
                    return
                events, status = manager.events_since(job_id, index)
                index += len(events)
                for event in events:
                    yield _sse("event", {"text": event})
                if status is not JobStatus.RUNNING:
                    final = json.loads(manager.snapshot(job_id).model_dump_json())
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
