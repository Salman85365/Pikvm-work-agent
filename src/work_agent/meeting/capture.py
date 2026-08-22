from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import ssl
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeGuard, cast
from urllib.parse import SplitResult, urlsplit, urlunsplit

import av
from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.contrib.media import MediaRecorder
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack
from aiortc.sdp import candidate_from_sdp
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import (
    ConnectionClosed,
    InvalidHandshake,
    InvalidStatus,
    WebSocketException,
)
from websockets.typing import Subprotocol

from work_agent.meeting.errors import MeetingError
from work_agent.pikvm.auth import build_pikvm_auth_headers
from work_agent.pikvm.config import PiKVMSettings
from work_agent.pikvm.totp import TotpProvider

_JANUS_SUBPROTOCOL = "janus-protocol"
_JANUS_PLUGIN = "janus.plugin.ustreamer"
_MAX_SIGNAL_MESSAGE_BYTES = 1_048_576
_KEEPALIVE_SECONDS = 25.0
# Decoded audio may legitimately fall short of wall-clock time: packet loss and jitter drop
# frames without corrupting the file. A part is *degraded*, not invalid, when it falls short by
# more than the larger of these two bounds; only an unreadable or frameless file is rejected.
_AUDIO_SHORTFALL_TOLERANCE_SECONDS = 3.0
_AUDIO_SHORTFALL_TOLERANCE_RATIO = 0.05
_DEFAULT_MAX_RECONNECTS = 3
_DEFAULT_RECONNECT_BACKOFF_SECONDS = 2.0


class MeetingAudioUnavailableError(MeetingError):
    """The selected PiKVM did not provide a usable incoming HDMI-audio stream."""


class MeetingCaptureConnectionError(MeetingError):
    """The selected PiKVM WebRTC session disconnected or failed negotiation."""


class MeetingCaptureLocalError(MeetingCaptureConnectionError):
    """Protected local recording or coordination failed after WebRTC connected."""


class MeetingCaptureUnreachableError(MeetingCaptureConnectionError):
    """The PiKVM could not be reached at all: no TCP/TLS/WebSocket connection was made."""


class MeetingCaptureAuthError(MeetingCaptureConnectionError):
    """The PiKVM answered the WebSocket handshake with 401/403 for this profile's credentials."""


@dataclass(frozen=True, slots=True)
class RecordedAudioPart:
    path: Path
    offset_seconds: float
    duration_seconds: float
    # Decoded audio fell measurably short of wall-clock time (dropped frames); still usable.
    degraded: bool = False


@dataclass(frozen=True, slots=True)
class _ClosedAudioPart:
    partial_path: Path
    offset_seconds: float
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class CaptureResult:
    parts: tuple[RecordedAudioPart, ...]
    duration_seconds: float
    interrupted: bool = False
    interruption_code: str | None = None
    reconnects: int = 0


class _Recorder(Protocol):
    def addTrack(self, track: MediaStreamTrack) -> None: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class _WebSocket(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self, decode: bool | None = None) -> str | bytes: ...

    async def close(self, code: int = 1000, reason: str = "") -> None: ...


class _RemoteAudioTrack(MediaStreamTrack):
    """Read-through remote track that proves readiness on the first received frame."""

    kind = "audio"

    def __init__(
        self,
        source: MediaStreamTrack,
        first_frame: asyncio.Event,
        on_source_failure: Callable[[], None],
    ) -> None:
        super().__init__()
        self._source = source
        self._first_frame = first_frame
        self._on_source_failure = on_source_failure
        self.frames_received = 0

    async def recv(self) -> Any:
        try:
            frame = await self._source.recv()
        except MediaStreamError:
            self._on_source_failure()
            raise
        except Exception:
            self._on_source_failure()
            raise
        self.frames_received += 1
        self._first_frame.set()
        return frame


def janus_websocket_url(base_url: str) -> str:
    """Return the base-path-aware PiKVM Janus WebSocket URL."""

    parsed = urlsplit(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = f"{parsed.path.rstrip('/')}/janus/ws"
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


def janus_watch_message() -> dict[str, object]:
    """The hard receive-only PiKVM media request."""

    return {
        "request": "watch",
        "params": {
            "orientation": 0,
            "audio": True,
            "mic": False,
            "camera": False,
        },
    }


def _is_dict(value: object) -> TypeGuard[dict[str, Any]]:
    return isinstance(value, dict)


def _transaction() -> str:
    return secrets.token_urlsafe(12)


def _plugin_status(message: dict[str, Any]) -> str | None:
    data = message.get("plugindata")
    if not _is_dict(data):
        return None
    payload = data.get("data")
    if not _is_dict(payload):
        return None
    result = payload.get("result")
    if not _is_dict(result):
        return None
    status = result.get("status")
    return status if isinstance(status, str) else None


def _plugin_result(message: dict[str, Any]) -> dict[str, Any] | None:
    data = message.get("plugindata")
    payload = data.get("data") if _is_dict(data) else None
    result = payload.get("result") if _is_dict(payload) else None
    return result if _is_dict(result) else None


class _JanusSignaling:
    """Single-reader Janus dispatcher; SDP and credentials are never logged."""

    def __init__(self, websocket: _WebSocket, *, timeout_seconds: float) -> None:
        self._websocket = websocket
        self._timeout = timeout_seconds
        self._pending: dict[
            str,
            tuple[Callable[[dict[str, Any]], bool], asyncio.Future[dict[str, Any]]],
        ] = {}
        self._plugin_events: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=32)
        self._candidates: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=64)
        self._fatal: asyncio.Future[MeetingCaptureConnectionError] = (
            asyncio.get_running_loop().create_future()
        )
        self._closing = False
        self._receiver = asyncio.create_task(self._receive_loop())

    @property
    def fatal(self) -> asyncio.Future[MeetingCaptureConnectionError]:
        return self._fatal

    async def request(
        self,
        message: dict[str, object],
        *,
        predicate: Callable[[dict[str, Any]], bool],
    ) -> dict[str, Any]:
        transaction = _transaction()
        payload = {**message, "transaction": transaction}
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[transaction] = (predicate, future)
        try:
            await self._send(payload)
            waiters: set[asyncio.Future[Any]] = {future, self._fatal}
            done, _ = await asyncio.wait(
                waiters,
                timeout=self._timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise MeetingCaptureConnectionError("PiKVM WebRTC signaling timed out.")
            if self._fatal in done:
                raise self._fatal.result()
            return future.result()
        finally:
            self._pending.pop(transaction, None)
            if not future.done():
                future.cancel()

    async def send(self, message: dict[str, object]) -> None:
        await self._send({**message, "transaction": _transaction()})

    async def plugin_event(
        self,
        predicate: Callable[[dict[str, Any]], bool],
    ) -> dict[str, Any]:
        async def wait() -> dict[str, Any]:
            while True:
                message = await self._plugin_events.get()
                if predicate(message):
                    return message

        event_task = asyncio.create_task(wait())
        waiters: set[asyncio.Future[Any]] = {event_task, self._fatal}
        done, _ = await asyncio.wait(
            waiters,
            timeout=self._timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            event_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await event_task
            raise MeetingCaptureConnectionError("PiKVM WebRTC signaling timed out.")
        if self._fatal in done:
            event_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await event_task
            raise self._fatal.result()
        return event_task.result()

    async def next_candidate(self) -> dict[str, Any] | None:
        return await self._candidates.get()

    async def close(self) -> None:
        self._closing = True
        self._receiver.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._receiver
        for _, future in self._pending.values():
            if not future.done():
                future.cancel()
        with contextlib.suppress(Exception):
            await self._websocket.close()

    async def _send(self, payload: dict[str, object]) -> None:
        try:
            await self._websocket.send(json.dumps(payload, separators=(",", ":")))
        except (ConnectionClosed, OSError, WebSocketException):
            raise MeetingCaptureConnectionError(
                "The PiKVM WebRTC signaling connection closed."
            ) from None

    async def _receive_loop(self) -> None:
        try:
            while True:
                raw = await self._websocket.recv(decode=True)
                if not isinstance(raw, str):
                    raise ValueError
                message = json.loads(raw)
                if not _is_dict(message):
                    raise ValueError
                transaction = message.get("transaction")
                if isinstance(transaction, str) and transaction in self._pending:
                    predicate, future = self._pending[transaction]
                    if message.get("janus") == "error":
                        if not future.done():
                            future.set_exception(
                                MeetingCaptureConnectionError(
                                    "PiKVM rejected a WebRTC signaling request."
                                )
                            )
                    elif predicate(message) and not future.done():
                        future.set_result(message)

                janus_type = message.get("janus")
                if janus_type == "error":
                    self._set_fatal("PiKVM rejected a WebRTC signaling request.")
                    return
                if janus_type == "event":
                    data = message.get("plugindata")
                    payload = data.get("data") if _is_dict(data) else None
                    if _is_dict(payload) and (
                        payload.get("error_code") is not None or payload.get("error") is not None
                    ):
                        self._set_fatal("PiKVM rejected a WebRTC media request.")
                        return
                    if self._plugin_events.full():
                        raise ValueError
                    self._plugin_events.put_nowait(message)
                elif janus_type == "trickle":
                    candidate = message.get("candidate")
                    if _is_dict(candidate):
                        if self._candidates.full():
                            raise ValueError
                        self._candidates.put_nowait(candidate)
                elif janus_type in {"hangup", "detached"}:
                    self._set_fatal("The PiKVM WebRTC media session ended unexpectedly.")
                    return
        except asyncio.CancelledError:
            raise
        except (ConnectionClosed, OSError, WebSocketException):
            if not self._closing:
                self._set_fatal("The PiKVM WebRTC signaling connection closed.")
        except (UnicodeError, ValueError, json.JSONDecodeError):
            if not self._closing:
                self._set_fatal("PiKVM returned invalid WebRTC signaling data.")

    def _set_fatal(self, message: str) -> None:
        if not self._fatal.done():
            self._fatal.set_result(MeetingCaptureConnectionError(message))


class SegmentedAudioRecorder:
    """Record only a remote audio track into bounded Ogg/Opus parts."""

    def __init__(
        self,
        directory: Path,
        *,
        segment_seconds: float = 600.0,
        recorder_factory: Callable[..., _Recorder] = MediaRecorder,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_first_frame: Callable[[], None] | None = None,
        on_part: Callable[[RecordedAudioPart], None] | None = None,
        first_part_index: int = 1,
        timeline_origin: float | None = None,
    ) -> None:
        if segment_seconds <= 0:
            raise ValueError("Audio segment duration must be greater than zero.")
        if first_part_index < 1:
            raise ValueError("Audio part numbering starts at one.")
        self._directory = directory
        # A reconnected session continues the same recording: its parts are numbered after the
        # earlier ones and their offsets stay relative to the first session's start.
        self._timeline_origin = timeline_origin
        self._segment_seconds = segment_seconds
        self._recorder_factory = recorder_factory
        self._monotonic = monotonic
        self._sleep = sleep
        self._on_first_frame = on_first_frame
        self._on_part = on_part
        self._first_frame = asyncio.Event()
        self._fatal: asyncio.Future[MeetingCaptureConnectionError] = (
            asyncio.get_running_loop().create_future()
        )
        self._source: MediaStreamTrack | None = None
        self._track: _RemoteAudioTrack | None = None
        self._recorder: _Recorder | None = None
        self._rotation_task: asyncio.Task[None] | None = None
        self._promotion_task: asyncio.Task[None] | None = None
        self._first_frame_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._capture_started = 0.0
        self._part_started = 0.0
        self._part_start_frames = 0
        self._part_index = first_part_index - 1
        self._partial_path: Path | None = None
        self._parts: list[RecordedAudioPart] = []
        self._stopped = False

    @property
    def first_frame(self) -> asyncio.Event:
        return self._first_frame

    @property
    def fatal(self) -> asyncio.Future[MeetingCaptureConnectionError]:
        return self._fatal

    @property
    def parts(self) -> tuple[RecordedAudioPart, ...]:
        return tuple(self._parts)

    @property
    def timeline_origin(self) -> float | None:
        """Monotonic instant that offset zero refers to, once a track has been attached."""

        return self._timeline_origin

    @property
    def next_part_index(self) -> int:
        return self._part_index + 1

    async def start(self, source: MediaStreamTrack) -> None:
        async with self._lock:
            if self._stopped:
                raise MeetingCaptureConnectionError(
                    "The protected local audio recorder was already finalized."
                )
            if self._track is not None:
                raise MeetingCaptureConnectionError(
                    "PiKVM offered more than one incoming audio track."
                )
            self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._directory.chmod(0o700)
            now = self._monotonic()
            if self._timeline_origin is None:
                self._timeline_origin = now
            self._capture_started = self._timeline_origin
            self._source = source
            source.on("ended", self._source_failed)
            if source.readyState == "ended":
                self._source_failed()
                raise self._fatal.result()
            self._track = _RemoteAudioTrack(source, self._first_frame, self._source_failed)
            await self._start_part()
            self._rotation_task = asyncio.create_task(self._rotate_loop())
            self._rotation_task.add_done_callback(
                lambda task: self._forward_background_failure(
                    task,
                    "The protected local audio recorder could not rotate to its next segment.",
                )
            )
            self._first_frame_task = asyncio.create_task(self._announce_first_frame())
            self._first_frame_task.add_done_callback(
                lambda task: self._forward_background_failure(
                    task,
                    "The protected local audio recorder could not announce readiness.",
                )
            )

    async def stop(self) -> tuple[RecordedAudioPart, ...]:
        rotation_task: asyncio.Task[None] | None
        promotion_task: asyncio.Task[None] | None
        first_frame_task: asyncio.Task[None] | None
        closed_part: _ClosedAudioPart | None = None
        close_error: MeetingCaptureLocalError | None = None
        promotion_error: MeetingCaptureLocalError | None = None
        background_error: MeetingCaptureLocalError | None = None
        async with self._lock:
            if self._stopped:
                return tuple(self._parts)
            self._stopped = True
            rotation_task = self._rotation_task
            promotion_task = self._promotion_task
            first_frame_task = self._first_frame_task
            self._rotation_task = None
            self._promotion_task = None
            self._first_frame_task = None
            if rotation_task is not None:
                rotation_task.cancel()
            if first_frame_task is not None:
                first_frame_task.cancel()
            try:
                closed_part = await self._close_part()
            except MeetingCaptureLocalError as exc:
                close_error = exc

        for task, message in (
            (
                rotation_task,
                "The protected local audio recorder could not rotate to its next segment.",
            ),
            (
                first_frame_task,
                "The protected local audio recorder could not announce readiness.",
            ),
        ):
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except MeetingCaptureLocalError as exc:
                    background_error = background_error or exc
                except Exception:
                    background_error = background_error or MeetingCaptureLocalError(message)
        if promotion_task is not None:
            try:
                await asyncio.shield(promotion_task)
            except MeetingCaptureLocalError as exc:
                promotion_error = exc
            except Exception:
                promotion_error = MeetingCaptureLocalError(
                    "The protected local audio part could not be finalized."
                )

        if promotion_error is not None:
            raise promotion_error
        if close_error is not None:
            raise close_error
        fatal_error = self._fatal.result() if self._fatal.done() else None
        if isinstance(fatal_error, MeetingCaptureLocalError):
            raise fatal_error
        if background_error is not None:
            raise background_error
        if closed_part is not None:
            await self._promote_part(closed_part)
        if fatal_error is not None:
            raise fatal_error
        return tuple(self._parts)

    def _forward_background_failure(
        self,
        task: asyncio.Task[None],
        message: str,
    ) -> None:
        if task.cancelled() or self._fatal.done():
            return
        try:
            task.result()
        except Exception:
            self._fatal.set_result(MeetingCaptureLocalError(message))

    def _source_failed(self) -> None:
        if not self._stopped and not self._fatal.done():
            self._fatal.set_result(
                MeetingCaptureConnectionError(
                    "The incoming PiKVM audio stream ended before capture stopped."
                )
            )

    async def _announce_first_frame(self) -> None:
        await self._first_frame.wait()
        if self._on_first_frame is not None:
            self._on_first_frame()

    async def _rotate_loop(self) -> None:
        while True:
            await self._sleep(self._segment_seconds)
            promotion_task: asyncio.Task[None] | None = None
            start_error: Exception | None = None
            async with self._lock:
                if self._stopped:
                    return
                closed_part = await self._close_part()
                try:
                    await self._start_part()
                    await asyncio.sleep(0)
                except Exception as exc:
                    start_error = exc
                finally:
                    if closed_part is not None:
                        promotion_task = asyncio.create_task(self._promote_part(closed_part))
                        promotion_task.add_done_callback(
                            lambda task: self._forward_background_failure(
                                task,
                                "The protected local audio part could not be finalized.",
                            )
                        )
                        self._promotion_task = promotion_task
            if promotion_task is not None:
                await asyncio.shield(promotion_task)
            if start_error is not None:
                raise start_error

    async def _start_part(self) -> None:
        if self._track is None:
            raise AssertionError("Audio track must be set before opening a part.")
        self._part_index += 1
        self._part_started = self._monotonic()
        self._part_start_frames = self._track.frames_received
        self._partial_path = self._directory / f"audio-{self._part_index:04d}.partial.ogg"
        try:
            descriptor = os.open(
                self._partial_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            os.close(descriptor)
            self._recorder = self._recorder_factory(str(self._partial_path), format="ogg")
            self._partial_path.chmod(0o600)
        except OSError:
            raise MeetingCaptureLocalError(
                "The protected local audio part could not be created."
            ) from None
        self._recorder.addTrack(self._track)
        await self._recorder.start()

    async def _close_part(self) -> _ClosedAudioPart | None:
        recorder = self._recorder
        partial_path = self._partial_path
        track = self._track
        if recorder is None or partial_path is None or track is None:
            return None
        self._recorder = None
        self._partial_path = None
        ended = self._monotonic()
        try:
            await recorder.stop()
        except Exception:
            raise MeetingCaptureLocalError(
                "The protected local audio part could not be finalized."
            ) from None
        if track.frames_received <= self._part_start_frames:
            with contextlib.suppress(FileNotFoundError):
                partial_path.unlink()
            return None
        return _ClosedAudioPart(
            partial_path=partial_path,
            offset_seconds=max(0.0, self._part_started - self._capture_started),
            duration_seconds=max(0.0, ended - self._part_started),
        )

    async def _promote_part(self, closed: _ClosedAudioPart) -> None:
        part = await asyncio.to_thread(_promote_audio_part, closed)
        self._parts.append(part)
        if self._on_part is not None:
            self._on_part(part)


def _promote_audio_part(closed: _ClosedAudioPart) -> RecordedAudioPart:
    partial_path = closed.partial_path
    final_path = partial_path.with_name(partial_path.name.replace(".partial.ogg", ".ogg"))
    check = _validate_opus_audio_part(
        partial_path,
        expected_duration_seconds=closed.duration_seconds,
    )
    try:
        _fsync_path(partial_path)
        os.replace(partial_path, final_path)
        final_path.chmod(0o600)
        _fsync_path(final_path.parent)
    except OSError:
        raise MeetingCaptureLocalError(
            "The protected local audio part could not be finalized."
        ) from None
    return RecordedAudioPart(
        path=final_path,
        offset_seconds=closed.offset_seconds,
        duration_seconds=closed.duration_seconds,
        degraded=check.degraded,
    )


@dataclass(frozen=True, slots=True)
class AudioPartCheck:
    decoded_seconds: float
    degraded: bool


def audio_shortfall_tolerance_seconds(expected_duration_seconds: float) -> float:
    return max(
        _AUDIO_SHORTFALL_TOLERANCE_SECONDS,
        _AUDIO_SHORTFALL_TOLERANCE_RATIO * expected_duration_seconds,
    )


def _validate_opus_audio_part(
    path: Path,
    *,
    expected_duration_seconds: float | None = None,
) -> AudioPartCheck:
    """Reject only an unreadable, malformed, or frameless part; report a short one as degraded."""

    try:
        if path.stat().st_size <= 0:
            raise ValueError
        with av.open(str(path), mode="r", format="ogg") as container:
            streams = list(container.streams)
            if len(streams) != 1:
                raise ValueError
            stream = streams[0]
            if stream.type != "audio" or stream.codec_context.name != "opus":
                raise ValueError
            decoded_audio_seconds = 0.0
            for packet in container.demux(stream):
                for frame in packet.decode():
                    if (
                        not isinstance(frame, av.AudioFrame)
                        or frame.samples <= 0
                        or frame.sample_rate is None
                        or frame.sample_rate <= 0
                    ):
                        raise ValueError
                    decoded_audio_seconds += frame.samples / frame.sample_rate
            if decoded_audio_seconds <= 0:
                raise ValueError
    except Exception:
        raise MeetingCaptureLocalError(
            "The protected local audio part failed integrity validation."
        ) from None
    degraded = (
        expected_duration_seconds is not None
        and decoded_audio_seconds + audio_shortfall_tolerance_seconds(expected_duration_seconds)
        < expected_duration_seconds
    )
    return AudioPartCheck(decoded_seconds=decoded_audio_seconds, degraded=degraded)


def _fsync_path(path: Path) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY)
        os.fsync(descriptor)
    finally:
        if descriptor is not None:
            with contextlib.suppress(OSError):
                os.close(descriptor)


async def _recorder_finalization_error(
    recorder: SegmentedAudioRecorder,
) -> MeetingCaptureConnectionError | None:
    try:
        await recorder.stop()
    except MeetingCaptureConnectionError as exc:
        return exc
    except Exception:
        return MeetingCaptureLocalError("The protected local meeting audio could not be finalized.")
    return None


class PiKVMWebRTCAudioCapture:
    """Receive and record HDMI audio from one PiKVM without any local input track."""

    def __init__(
        self,
        settings: PiKVMSettings,
        *,
        totp_provider: TotpProvider | None,
        signaling_timeout_seconds: float = 15.0,
        audio_start_timeout_seconds: float = 20.0,
        segment_seconds: float = 600.0,
        monotonic: Callable[[], float] = time.monotonic,
        max_reconnects: int = _DEFAULT_MAX_RECONNECTS,
        reconnect_backoff_seconds: float = _DEFAULT_RECONNECT_BACKOFF_SECONDS,
    ) -> None:
        if max_reconnects < 0:
            raise ValueError("The reconnect bound cannot be negative.")
        if reconnect_backoff_seconds < 0:
            raise ValueError("The reconnect backoff cannot be negative.")
        self._settings = settings
        self._totp_provider = totp_provider
        self._signaling_timeout = signaling_timeout_seconds
        self._audio_start_timeout = audio_start_timeout_seconds
        self._segment_seconds = segment_seconds
        self._monotonic = monotonic
        self._max_reconnects = max_reconnects
        self._reconnect_backoff = reconnect_backoff_seconds
        self._abort: asyncio.Future[MeetingCaptureConnectionError] | None = None

    def abort(self, error: MeetingCaptureConnectionError) -> None:
        """End a running capture from outside, as if the session itself had failed with ``error``.

        Used when a coordinating task the recorder depends on (the stop watcher) dies: the audio
        captured so far is finalized and the failure is reported rather than recording on with
        nobody able to stop it.
        """

        if self._abort is not None and not self._abort.done():
            self._abort.set_result(error)

    async def record(
        self,
        directory: Path,
        *,
        stop_requested: asyncio.Event,
        on_ready: Callable[[], None] | None = None,
        on_part: Callable[[RecordedAudioPart], None] | None = None,
        on_heartbeat: Callable[[], None] | None = None,
        on_reconnect: Callable[[int], None] | None = None,
    ) -> CaptureResult:
        """Record until stop is requested, reconnecting a bounded number of times if the link drops.

        A first session that never delivers audio fails as before. Once audio has flowed, a
        dropped WebRTC or signaling connection opens a fresh Janus session and continues the same
        recording: parts keep numbering upward and their offsets stay relative to the first
        frame, so the timeline in the manifest is honest about the gap. Only after the bound is
        exhausted (or the PiKVM rejects the credentials) is the capture reported as disconnected.
        """

        started = self._monotonic()
        self._abort = asyncio.get_running_loop().create_future()
        parts: list[RecordedAudioPart] = []
        interrupted = False
        interruption_code: str | None = None
        reconnects = 0
        next_part_index = 1
        timeline_origin: float | None = None

        while True:
            reconnecting = timeline_origin is not None
            recorder = SegmentedAudioRecorder(
                directory,
                segment_seconds=self._segment_seconds,
                monotonic=self._monotonic,
                on_first_frame=None if reconnecting else on_ready,
                on_part=on_part,
                first_part_index=next_part_index,
                timeline_origin=timeline_origin,
            )
            connection_error: MeetingError | None = None
            try:
                await self._record_session(
                    recorder,
                    stop_requested=stop_requested,
                    on_heartbeat=on_heartbeat,
                )
            except MeetingCaptureLocalError:
                raise
            except MeetingCaptureConnectionError as exc:
                if not reconnecting and not recorder.first_frame.is_set():
                    raise
                connection_error = exc
            except MeetingAudioUnavailableError:
                if not reconnecting:
                    raise
                connection_error = MeetingCaptureConnectionError(
                    "PiKVM WebRTC reconnected but no incoming HDMI audio frames arrived."
                )
            finally:
                parts.extend(await recorder.stop())
            if recorder.timeline_origin is not None:
                timeline_origin = recorder.timeline_origin
                next_part_index = recorder.next_part_index

            if connection_error is None:
                break
            interrupted = True
            interruption_code = "webrtc_disconnected"
            if (
                stop_requested.is_set()
                or reconnects >= self._max_reconnects
                or isinstance(connection_error, MeetingCaptureAuthError)
            ):
                break
            reconnects += 1
            if on_reconnect is not None:
                on_reconnect(reconnects)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_requested.wait(), timeout=self._reconnect_backoff)
            if stop_requested.is_set():
                break
            interrupted = False
            interruption_code = None

        if not parts:
            raise MeetingAudioUnavailableError(
                "PiKVM WebRTC provided no incoming HDMI audio frames."
            )
        duration = max(
            (part.offset_seconds + part.duration_seconds for part in parts),
            default=max(0.0, self._monotonic() - started),
        )
        return CaptureResult(
            parts=tuple(parts),
            duration_seconds=duration,
            interrupted=interrupted,
            interruption_code=interruption_code,
            reconnects=reconnects,
        )

    async def _record_session(
        self,
        recorder: SegmentedAudioRecorder,
        *,
        stop_requested: asyncio.Event,
        on_heartbeat: Callable[[], None] | None,
    ) -> None:
        parsed = urlsplit(janus_websocket_url(self._settings.base_url))
        ssl_context = _ssl_context(parsed, verify=self._settings.verify_ssl)
        headers = build_pikvm_auth_headers(
            self._settings,
            totp_provider=self._totp_provider,
        )
        websocket: ClientConnection | None = None
        signaling: _JanusSignaling | None = None
        pc: RTCPeerConnection | None = None
        keepalive_task: asyncio.Task[None] | None = None
        candidate_task: asyncio.Task[None] | None = None
        heartbeat_task: asyncio.Task[None] | None = None
        audio_start_task: asyncio.Task[None] | None = None
        session_id: int | None = None
        handle_id: int | None = None

        try:
            websocket = await connect(
                urlunsplit(parsed),
                subprotocols=[cast(Subprotocol, _JANUS_SUBPROTOCOL)],
                additional_headers=headers,
                compression=None,
                proxy=None,
                open_timeout=self._signaling_timeout,
                close_timeout=5,
                max_size=_MAX_SIGNAL_MESSAGE_BYTES,
                max_queue=16,
                ssl=ssl_context,
                # Supplying these explicitly makes websockets reject any cross-origin redirect
                # before custom PiKVM credential headers can be forwarded.
                host=parsed.hostname,
                port=parsed.port or (443 if parsed.scheme == "wss" else 80),
            )
            if websocket.subprotocol != _JANUS_SUBPROTOCOL:
                raise MeetingCaptureConnectionError(
                    "PiKVM did not accept the Janus WebRTC subprotocol."
                )
            signaling = _JanusSignaling(
                websocket,
                timeout_seconds=self._signaling_timeout,
            )
            created = await signaling.request(
                {"janus": "create"},
                predicate=lambda message: message.get("janus") == "success",
            )
            session_id = _response_id(created, "session")
            attached = await signaling.request(
                {
                    "janus": "attach",
                    "plugin": _JANUS_PLUGIN,
                    "opaque_id": f"pikvm-work-agent-{_transaction()}",
                    "session_id": session_id,
                },
                predicate=lambda message: message.get("janus") == "success",
            )
            handle_id = _response_id(attached, "handle")
            keepalive_task = asyncio.create_task(_keepalive(signaling, session_id, stop_requested))
            keepalive_task.add_done_callback(
                lambda task: _forward_task_failure(
                    task,
                    signaling.fatal,
                    "The PiKVM WebRTC keepalive failed.",
                )
            )
            if on_heartbeat is not None:
                heartbeat_task = asyncio.create_task(_heartbeat(on_heartbeat, stop_requested))
                heartbeat_task.add_done_callback(
                    lambda task: _forward_task_failure(
                        task,
                        signaling.fatal,
                        "The protected meeting-recorder state could not be updated.",
                        local=True,
                    )
                )

            await signaling.send(
                {
                    "janus": "message",
                    "body": {"request": "features"},
                    "session_id": session_id,
                    "handle_id": handle_id,
                }
            )
            features_event = await signaling.plugin_event(
                lambda message: _plugin_status(message) == "features"
            )
            features_result = _plugin_result(features_event)
            features = features_result.get("features") if features_result is not None else None
            if not _is_dict(features) or features.get("audio") is not True:
                raise MeetingAudioUnavailableError(
                    "The selected PiKVM does not expose incoming HDMI audio over WebRTC."
                )

            pc = RTCPeerConnection(_rtc_configuration(features))
            audio_track_seen = asyncio.Event()

            @pc.on("track")
            def on_track(track: MediaStreamTrack) -> None:
                nonlocal audio_start_task
                if track.kind == "audio":
                    if audio_track_seen.is_set():
                        if not signaling.fatal.done():
                            signaling.fatal.set_result(
                                MeetingCaptureConnectionError(
                                    "PiKVM offered more than one incoming audio track."
                                )
                            )
                        return
                    audio_track_seen.set()
                    audio_start_task = asyncio.create_task(recorder.start(track))
                    audio_start_task.add_done_callback(
                        lambda task: _forward_task_failure(
                            task,
                            signaling.fatal,
                            "The Mac could not start the protected local audio recording.",
                            local=True,
                        )
                    )

            @pc.on("connectionstatechange")
            def on_connection_state_change() -> None:
                if (
                    pc is not None
                    and pc.connectionState == "failed"
                    and signaling is not None
                    and not signaling.fatal.done()
                ):
                    signaling.fatal.set_result(
                        MeetingCaptureConnectionError("The PiKVM WebRTC media connection failed.")
                    )

            await signaling.send(
                {
                    "janus": "message",
                    "body": janus_watch_message(),
                    "session_id": session_id,
                    "handle_id": handle_id,
                }
            )
            offer_event = await signaling.plugin_event(
                lambda message: isinstance(message.get("jsep"), dict)
            )
            jsep = offer_event.get("jsep")
            if not _is_dict(jsep):
                raise MeetingCaptureConnectionError("PiKVM returned an invalid WebRTC offer.")
            offer_type = jsep.get("type")
            offer_sdp = jsep.get("sdp")
            if offer_type != "offer" or not isinstance(offer_sdp, str) or not offer_sdp:
                raise MeetingCaptureConnectionError("PiKVM returned an invalid WebRTC offer.")
            await pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type="offer"))
            _set_receive_only_directions(pc)
            candidate_task = asyncio.create_task(_apply_remote_candidates(signaling, pc))
            candidate_task.add_done_callback(
                lambda task: _forward_task_failure(
                    task,
                    signaling.fatal,
                    "PiKVM returned invalid WebRTC network candidates.",
                )
            )
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)
            if pc.localDescription is None:
                raise MeetingCaptureConnectionError("The Mac could not create a WebRTC answer.")
            _assert_no_local_media(pc)
            await signaling.send(
                {
                    "janus": "message",
                    "body": {"request": "start"},
                    "jsep": {
                        "type": pc.localDescription.type,
                        "sdp": pc.localDescription.sdp,
                        "trickle": False,
                    },
                    "session_id": session_id,
                    "handle_id": handle_id,
                }
            )
            await signaling.plugin_event(lambda message: _plugin_status(message) == "started")
            await _wait_for_audio(
                recorder.first_frame,
                signaling.fatal,
                recorder.fatal,
                self._audio_start_timeout,
                abort=self._abort,
            )
            await _wait_until_stopped(
                stop_requested,
                signaling.fatal,
                recorder.fatal,
                abort=self._abort,
            )
        except MeetingError:
            raise
        except InvalidStatus as exc:
            if exc.response.status_code in {401, 403}:
                raise MeetingCaptureAuthError(
                    "The PiKVM rejected this profile's credentials for WebRTC audio."
                ) from None
            raise MeetingCaptureConnectionError(
                "The PiKVM refused the WebRTC signaling connection."
            ) from None
        except (InvalidHandshake, ConnectionClosed, WebSocketException):
            raise MeetingCaptureConnectionError(
                "The Mac could not establish the PiKVM WebRTC audio session."
            ) from None
        except (OSError, TimeoutError):
            if websocket is None:
                raise MeetingCaptureUnreachableError(
                    "The PiKVM could not be reached for WebRTC audio."
                ) from None
            raise MeetingCaptureConnectionError(
                "The Mac could not establish the PiKVM WebRTC audio session."
            ) from None
        except Exception:
            raise MeetingCaptureConnectionError(
                "An unexpected local error stopped PiKVM WebRTC audio capture."
            ) from None
        finally:
            # Finalize local audio before asking a possibly disconnected remote peer to stop.
            if audio_start_task is not None:
                audio_start_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await audio_start_task
                audio_start_task = None
            finalization_error = await _recorder_finalization_error(recorder)
            for task in (
                heartbeat_task,
                keepalive_task,
                candidate_task,
            ):
                if task is not None:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
            if signaling is not None and session_id is not None and handle_id is not None:
                with contextlib.suppress(Exception):
                    await signaling.send(
                        {
                            "janus": "message",
                            "body": {"request": "stop"},
                            "session_id": session_id,
                            "handle_id": handle_id,
                        }
                    )
                with contextlib.suppress(Exception):
                    await signaling.send(
                        {
                            "janus": "hangup",
                            "session_id": session_id,
                            "handle_id": handle_id,
                        }
                    )
            if pc is not None:
                with contextlib.suppress(Exception):
                    await pc.close()
            if signaling is not None:
                if session_id is not None and handle_id is not None:
                    with contextlib.suppress(Exception):
                        await signaling.send(
                            {
                                "janus": "detach",
                                "session_id": session_id,
                                "handle_id": handle_id,
                            }
                        )
                if session_id is not None:
                    with contextlib.suppress(Exception):
                        await signaling.send({"janus": "destroy", "session_id": session_id})
                await signaling.close()
            elif websocket is not None:
                with contextlib.suppress(Exception):
                    await websocket.close()
            if finalization_error is not None:
                raise finalization_error


def _ssl_context(parsed: SplitResult, *, verify: bool) -> ssl.SSLContext | None:
    if parsed.scheme != "wss":
        return None
    context = ssl.create_default_context()
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def _response_id(message: dict[str, Any], kind: str) -> int:
    data = message.get("data")
    identifier = data.get("id") if _is_dict(data) else None
    if not isinstance(identifier, int) or identifier <= 0:
        raise MeetingCaptureConnectionError(f"PiKVM returned an invalid Janus {kind} identifier.")
    return identifier


def _rtc_configuration(features: dict[str, Any]) -> RTCConfiguration:
    ice = features.get("ice")
    url = ice.get("url") if _is_dict(ice) else None
    if isinstance(url, str) and url.strip():
        return RTCConfiguration(iceServers=[RTCIceServer(urls=url.strip())])
    return RTCConfiguration()


def _set_receive_only_directions(pc: RTCPeerConnection) -> None:
    has_audio = False
    for transceiver in pc.getTransceivers():
        if transceiver.kind == "audio":
            transceiver.direction = "recvonly"
            has_audio = True
        elif transceiver.kind == "video":
            transceiver.direction = "inactive"
        else:
            raise MeetingCaptureConnectionError("PiKVM offered an unsupported WebRTC media type.")
    if not has_audio:
        raise MeetingAudioUnavailableError(
            "PiKVM WebRTC did not offer an incoming HDMI audio track."
        )


def _assert_no_local_media(pc: RTCPeerConnection) -> None:
    if any(sender.track is not None for sender in pc.getSenders()):
        raise MeetingCaptureConnectionError(
            "The receive-only WebRTC boundary rejected an unexpected local media track."
        )


async def _apply_remote_candidates(
    signaling: _JanusSignaling,
    pc: RTCPeerConnection,
) -> None:
    while True:
        payload = await signaling.next_candidate()
        if payload is None or payload.get("completed") is True:
            await pc.addIceCandidate(None)
            continue
        raw = payload.get("candidate")
        if not isinstance(raw, str) or not raw:
            continue
        candidate = candidate_from_sdp(raw.removeprefix("candidate:"))
        mid = payload.get("sdpMid")
        line = payload.get("sdpMLineIndex")
        candidate.sdpMid = mid if isinstance(mid, str) else None
        candidate.sdpMLineIndex = line if isinstance(line, int) else None
        await pc.addIceCandidate(candidate)


async def _keepalive(
    signaling: _JanusSignaling,
    session_id: int,
    stop_requested: asyncio.Event,
) -> None:
    while not stop_requested.is_set():
        try:
            await asyncio.wait_for(stop_requested.wait(), timeout=_KEEPALIVE_SECONDS)
        except TimeoutError:
            await signaling.send({"janus": "keepalive", "session_id": session_id})


async def _heartbeat(callback: Callable[[], None], stop_requested: asyncio.Event) -> None:
    while not stop_requested.is_set():
        callback()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_requested.wait(), timeout=5.0)


async def _wait_for_audio(
    first_frame: asyncio.Event,
    fatal: asyncio.Future[MeetingCaptureConnectionError],
    recorder_fatal: asyncio.Future[MeetingCaptureConnectionError],
    timeout: float,
    *,
    abort: asyncio.Future[MeetingCaptureConnectionError] | None = None,
) -> None:
    frame_task = asyncio.create_task(first_frame.wait())
    failures = [future for future in (recorder_fatal, fatal, abort) if future is not None]
    waiters: set[asyncio.Future[Any]] = {frame_task, *failures}
    done, _ = await asyncio.wait(
        waiters,
        timeout=timeout,
        return_when=asyncio.FIRST_COMPLETED,
    )
    if not done:
        frame_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await frame_task
        raise MeetingAudioUnavailableError(
            "PiKVM WebRTC connected but no incoming HDMI audio frames arrived."
        )
    failed = [future for future in failures if future in done]
    if failed:
        frame_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await frame_task
        raise failed[0].result()


async def _wait_until_stopped(
    stop_requested: asyncio.Event,
    fatal: asyncio.Future[MeetingCaptureConnectionError],
    recorder_fatal: asyncio.Future[MeetingCaptureConnectionError],
    *,
    abort: asyncio.Future[MeetingCaptureConnectionError] | None = None,
) -> None:
    stop_task = asyncio.create_task(stop_requested.wait())
    failures = [future for future in (recorder_fatal, fatal, abort) if future is not None]
    waiters: set[asyncio.Future[Any]] = {stop_task, *failures}
    done, _ = await asyncio.wait(
        waiters,
        return_when=asyncio.FIRST_COMPLETED,
    )
    failed = [future for future in failures if future in done]
    if failed:
        stop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stop_task
        raise failed[0].result()


def _forward_task_failure(
    task: asyncio.Task[None],
    fatal: asyncio.Future[MeetingCaptureConnectionError],
    message: str,
    *,
    local: bool = False,
) -> None:
    if task.cancelled() or fatal.done():
        return
    try:
        task.result()
    except Exception:
        error_type = MeetingCaptureLocalError if local else MeetingCaptureConnectionError
        fatal.set_result(error_type(message))
