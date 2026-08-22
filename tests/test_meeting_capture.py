from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import stat
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import av
import pytest
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack

from work_agent.meeting import capture
from work_agent.meeting.capture import (
    MeetingCaptureConnectionError,
    MeetingCaptureLocalError,
    SegmentedAudioRecorder,
)
from work_agent.pikvm.config import PiKVMSettings


class _WebSocket:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[str | bytes | BaseException] = asyncio.Queue()
        self.sent: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.closed = False

    async def send(self, message: str) -> None:
        await self.sent.put(json.loads(message))

    async def recv(self, decode: bool | None = None) -> str | bytes:
        del decode
        item = await self.incoming.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self, code: int = 1000, reason: str = "") -> None:
        del code, reason
        self.closed = True


class _OneFrameTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._delivered = False

    async def recv(self) -> object:
        if self._delivered:
            await asyncio.Future[None]()
        self._delivered = True
        return object()


class _Recorder:
    instances: ClassVar[list[_Recorder]] = []

    def __init__(self, path: str, *, format: str) -> None:
        assert format == "ogg"
        self.path = Path(path)
        _write_opus_ogg(self.path)
        self.track: MediaStreamTrack | None = None
        self.reader: asyncio.Task[object] | None = None
        self.stops = 0
        type(self).instances.append(self)

    def addTrack(self, track: MediaStreamTrack) -> None:
        self.track = track

    async def start(self) -> None:
        assert self.track is not None
        self.reader = asyncio.create_task(self.track.recv())

    async def stop(self) -> None:
        self.stops += 1
        if self.reader is not None:
            await self.reader


def _write_opus_ogg(
    path: Path,
    *,
    stream_count: int = 1,
    duration_seconds: float = 5.0,
) -> None:
    with av.open(str(path), mode="w", format="ogg") as container:
        streams = []
        for _ in range(stream_count):
            stream = container.add_stream("libopus", rate=48_000)
            stream.layout = "mono"
            streams.append(stream)
        for stream in streams:
            for index in range(max(1, round(duration_seconds * 50))):
                frame = av.AudioFrame(format="s16", layout="mono", samples=960)
                frame.sample_rate = 48_000
                frame.pts = index * 960
                for plane in frame.planes:
                    plane.update(bytes(plane.buffer_size))
                for packet in stream.encode(frame):
                    container.mux(packet)
            for packet in stream.encode(None):
                container.mux(packet)


def _times(values: list[float]) -> Iterator[float]:
    yield from values


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://pikvm.example", "wss://pikvm.example/janus/ws"),
        ("https://pikvm.example/kvm", "wss://pikvm.example/kvm/janus/ws"),
        ("https://pikvm.example/nested/kvm/", "wss://pikvm.example/nested/kvm/janus/ws"),
        ("http://127.0.0.1:8080/kvm", "ws://127.0.0.1:8080/kvm/janus/ws"),
    ],
)
def test_janus_websocket_url_preserves_the_profile_base_path(
    base_url: str,
    expected: str,
) -> None:
    assert capture.janus_websocket_url(base_url) == expected


def test_watch_request_can_only_receive_pikvm_hdmi_audio() -> None:
    assert capture.janus_watch_message() == {
        "request": "watch",
        "params": {
            "orientation": 0,
            "audio": True,
            "mic": False,
            "camera": False,
        },
    }


def test_receive_only_boundary_has_no_local_audio_or_video_track() -> None:
    async def exercise() -> None:
        pc = RTCPeerConnection()
        try:
            audio = pc.addTransceiver("audio", direction="sendrecv")
            video = pc.addTransceiver("video", direction="sendrecv")

            capture._set_receive_only_directions(pc)
            capture._assert_no_local_media(pc)

            assert audio.direction == "recvonly"
            assert video.direction == "inactive"
            assert all(sender.track is None for sender in pc.getSenders())
        finally:
            await pc.close()

    asyncio.run(exercise())


def test_capture_module_has_no_mac_microphone_capture_path() -> None:
    source_path = Path(capture.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_modules: set[str] = set()
    imported_names: set[str] = set()
    called_names: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                imported_modules.add(node.module.split(".", 1)[0])
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                called_names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                called_names.add(node.func.attr)

    assert imported_modules.isdisjoint(
        {"avfoundation", "pyaudio", "soundcard", "sounddevice", "subprocess"}
    )
    assert imported_names.isdisjoint({"AudioStreamTrack", "MediaPlayer"})
    assert called_names.isdisjoint({"getUserMedia", "MediaPlayer"})


def test_segmented_recorder_finalizes_private_audio_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        _Recorder.instances.clear()
        clock = _times([100.0, 100.0, 104.0])
        ready: list[bool] = []
        announced_parts: list[capture.RecordedAudioPart] = []
        synced_modes: list[int] = []
        modes_seen_by_callback: list[tuple[int, ...]] = []
        real_fsync = os.fsync

        def fsync(descriptor: int) -> None:
            synced_modes.append(os.fstat(descriptor).st_mode)
            real_fsync(descriptor)

        def announce_part(part: capture.RecordedAudioPart) -> None:
            announced_parts.append(part)
            modes_seen_by_callback.append(tuple(synced_modes))

        monkeypatch.setattr(capture.os, "fsync", fsync)
        recorder = SegmentedAudioRecorder(
            tmp_path / "capture",
            segment_seconds=60,
            recorder_factory=_Recorder,
            monotonic=lambda: next(clock),
            on_first_frame=lambda: ready.append(True),
            on_part=announce_part,
        )

        await recorder.start(_OneFrameTrack())
        await asyncio.wait_for(recorder.first_frame.wait(), timeout=1)
        await asyncio.sleep(0)
        first_stop = await recorder.stop()
        second_stop = await recorder.stop()

        assert ready == [True]
        assert first_stop == second_stop
        assert first_stop == tuple(announced_parts)
        assert len(first_stop) == 1
        assert first_stop[0].offset_seconds == 0
        assert first_stop[0].duration_seconds == 4
        assert first_stop[0].path.name == "audio-0001.ogg"
        capture._validate_opus_audio_part(first_stop[0].path)
        assert first_stop[0].path.stat().st_size > 0
        assert first_stop[0].path.stat().st_mode & 0o777 == 0o600
        assert (tmp_path / "capture").stat().st_mode & 0o777 == 0o700
        assert not list((tmp_path / "capture").glob("*.partial.ogg"))
        assert len(_Recorder.instances) == 1
        assert _Recorder.instances[0].stops == 1
        assert modes_seen_by_callback
        assert any(stat.S_ISREG(mode) for mode in modes_seen_by_callback[0])
        assert any(stat.S_ISDIR(mode) for mode in modes_seen_by_callback[0])

    asyncio.run(exercise())


def test_segmented_recorder_rejects_corrupt_audio_before_promotion_or_checkpoint(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        announced: list[capture.RecordedAudioPart] = []
        clock = _times([100.0, 100.0, 101.0])

        class CorruptRecorder(_Recorder):
            def __init__(self, path: str, *, format: str) -> None:
                super().__init__(path, format=format)
                self.path.write_bytes(b"SECRET-SYNTHETIC-CORRUPTION")

        recorder = SegmentedAudioRecorder(
            tmp_path / "capture",
            segment_seconds=60,
            recorder_factory=CorruptRecorder,
            monotonic=lambda: next(clock),
            on_part=announced.append,
        )
        await recorder.start(_OneFrameTrack())
        await asyncio.wait_for(recorder.first_frame.wait(), timeout=1)

        with pytest.raises(MeetingCaptureLocalError, match="integrity validation") as caught:
            await recorder.stop()

        assert "SECRET-SYNTHETIC-CORRUPTION" not in str(caught.value)
        assert announced == []
        assert recorder.parts == ()
        partial = tmp_path / "capture" / "audio-0001.partial.ogg"
        assert partial.read_bytes() == b"SECRET-SYNTHETIC-CORRUPTION"
        assert partial.stat().st_mode & 0o777 == 0o600
        assert not (tmp_path / "capture" / "audio-0001.ogg").exists()

    asyncio.run(exercise())


def test_audio_part_validation_requires_exactly_one_opus_audio_stream(
    tmp_path: Path,
) -> None:
    valid = tmp_path / "valid.ogg"
    multiple = tmp_path / "multiple.ogg"
    valid_but_short = tmp_path / "valid-but-short.ogg"
    _write_opus_ogg(valid)
    _write_opus_ogg(multiple, stream_count=2)
    _write_opus_ogg(valid_but_short, duration_seconds=0.02)

    healthy = capture._validate_opus_audio_part(valid, expected_duration_seconds=5)
    assert healthy.degraded is False
    assert healthy.decoded_seconds == pytest.approx(5.0, abs=0.1)
    with pytest.raises(MeetingCaptureLocalError, match="integrity validation"):
        capture._validate_opus_audio_part(multiple)
    # Dropped frames make a part short, not invalid: it is kept and marked degraded.
    short = capture._validate_opus_audio_part(valid_but_short, expected_duration_seconds=5)
    assert short.degraded is True
    assert short.decoded_seconds > 0


def test_audio_shortfall_tolerance_is_proportional_and_only_frameless_files_fail(
    tmp_path: Path,
) -> None:
    assert capture.audio_shortfall_tolerance_seconds(30) == 3.0
    assert capture.audio_shortfall_tolerance_seconds(300) == 15.0

    slightly_short = tmp_path / "slightly-short.ogg"
    _write_opus_ogg(slightly_short, duration_seconds=8)
    within = capture._validate_opus_audio_part(slightly_short, expected_duration_seconds=10)
    assert within.degraded is False
    beyond = capture._validate_opus_audio_part(slightly_short, expected_duration_seconds=12)
    assert beyond.degraded is True

    empty = tmp_path / "empty.ogg"
    empty.write_bytes(b"")
    with pytest.raises(MeetingCaptureLocalError, match="integrity validation"):
        capture._validate_opus_audio_part(empty)


def test_segmented_recorder_cannot_start_after_cleanup_wins_the_race(tmp_path: Path) -> None:
    async def exercise() -> None:
        _Recorder.instances.clear()
        recorder = SegmentedAudioRecorder(
            tmp_path / "capture",
            segment_seconds=60,
            recorder_factory=_Recorder,
        )

        delayed_start = asyncio.create_task(recorder.start(_OneFrameTrack()))
        await recorder.stop()

        with pytest.raises(MeetingCaptureConnectionError):
            await delayed_start
        assert _Recorder.instances == []
        assert not list((tmp_path / "capture").glob("*.partial.ogg"))

    asyncio.run(exercise())


def test_audio_part_fsync_failure_is_sanitized_and_never_announced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        announced: list[capture.RecordedAudioPart] = []
        clock = _times([100.0, 100.0, 101.0])
        recorder = SegmentedAudioRecorder(
            tmp_path / "capture",
            segment_seconds=60,
            recorder_factory=_Recorder,
            monotonic=lambda: next(clock),
            on_part=announced.append,
        )
        await recorder.start(_OneFrameTrack())
        await asyncio.wait_for(recorder.first_frame.wait(), timeout=1)

        def fail_fsync(_: int) -> None:
            raise OSError("SECRET-MEETING-CONTENT")

        monkeypatch.setattr(capture.os, "fsync", fail_fsync)

        with pytest.raises(MeetingCaptureLocalError) as caught:
            await recorder.stop()

        assert "SECRET-MEETING-CONTENT" not in str(caught.value)
        assert announced == []
        assert list((tmp_path / "capture").glob("*.partial.ogg"))
        assert not (tmp_path / "capture" / "audio-0001.ogg").exists()

    asyncio.run(exercise())


def test_audio_recorder_flush_failure_is_sanitized_and_never_announced(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        announced: list[capture.RecordedAudioPart] = []
        clock = _times([100.0, 100.0, 101.0])

        class FailingStopRecorder(_Recorder):
            async def stop(self) -> None:
                raise OSError("SECRET-LOCAL-PATH")

        recorder = SegmentedAudioRecorder(
            tmp_path / "capture",
            segment_seconds=60,
            recorder_factory=FailingStopRecorder,
            monotonic=lambda: next(clock),
            on_part=announced.append,
        )
        await recorder.start(_OneFrameTrack())
        await asyncio.wait_for(recorder.first_frame.wait(), timeout=1)

        with pytest.raises(MeetingCaptureLocalError, match="could not be finalized") as caught:
            await recorder.stop()

        assert "SECRET-LOCAL-PATH" not in str(caught.value)
        assert announced == []
        assert recorder.parts == ()
        assert (tmp_path / "capture" / "audio-0001.partial.ogg").is_file()
        assert not (tmp_path / "capture" / "audio-0001.ogg").exists()

    asyncio.run(exercise())


def test_rotation_failure_wakes_the_capture_wait_as_a_sanitized_local_error(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        _Recorder.instances.clear()
        rotation_gate = asyncio.Event()
        sleep_calls = 0
        factory_calls = 0
        announced: list[capture.RecordedAudioPart] = []
        clock = _times([100.0, 100.0, 102.0, 102.0])

        async def controlled_sleep(_: float) -> None:
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls == 1:
                await rotation_gate.wait()
            else:
                await asyncio.Future[None]()

        def recorder_factory(path: str, *, format: str) -> _Recorder:
            nonlocal factory_calls
            factory_calls += 1
            if factory_calls == 2:
                raise OSError("SECRET-MEETING-CONTENT")
            return _Recorder(path, format=format)

        recorder = SegmentedAudioRecorder(
            tmp_path / "capture",
            segment_seconds=60,
            recorder_factory=recorder_factory,
            monotonic=lambda: next(clock),
            sleep=controlled_sleep,
            on_part=announced.append,
        )
        await recorder.start(_OneFrameTrack())
        await asyncio.wait_for(recorder.first_frame.wait(), timeout=1)
        signaling_fatal: asyncio.Future[MeetingCaptureConnectionError] = (
            asyncio.get_running_loop().create_future()
        )
        stop_requested = asyncio.Event()
        capture_wait = asyncio.create_task(
            capture._wait_until_stopped(stop_requested, signaling_fatal, recorder.fatal)
        )

        rotation_gate.set()
        with pytest.raises(
            MeetingCaptureLocalError,
            match="could not rotate to its next segment",
        ) as caught:
            await asyncio.wait_for(capture_wait, timeout=1)

        assert "SECRET-MEETING-CONTENT" not in str(caught.value)
        assert stop_requested.is_set() is False
        assert len(announced) == 1
        assert announced[0].path.name == "audio-0001.ogg"
        assert announced[0].path.is_file()
        assert factory_calls == 2
        with pytest.raises(MeetingCaptureLocalError, match="could not rotate"):
            await recorder.stop()

    asyncio.run(exercise())


def test_rotation_starts_the_next_consumer_before_off_thread_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        _Recorder.instances.clear()
        rotation_gate = asyncio.Event()
        second_receive_started = asyncio.Event()
        validation_started = threading.Event()
        release_validation = threading.Event()
        next_was_consuming: list[bool] = []
        announced: list[capture.RecordedAudioPart] = []
        clock = _times([100.0, 100.0, 102.0, 102.0, 104.0])
        sleep_calls = 0

        class TwoFrameTrack(MediaStreamTrack):
            kind = "audio"

            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            async def recv(self) -> object:
                self.calls += 1
                if self.calls == 2:
                    second_receive_started.set()
                if self.calls <= 2:
                    return object()
                await asyncio.Future[None]()

        async def controlled_sleep(_: float) -> None:
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls == 1:
                await rotation_gate.wait()
            else:
                await asyncio.Future[None]()

        real_validate = capture._validate_opus_audio_part

        def blocking_validate(
            path: Path,
            *,
            expected_duration_seconds: float | None = None,
        ) -> capture.AudioPartCheck:
            next_was_consuming.append(second_receive_started.is_set())
            validation_started.set()
            if not release_validation.wait(timeout=5):
                raise RuntimeError("test validation release timed out")
            return real_validate(
                path,
                expected_duration_seconds=expected_duration_seconds,
            )

        monkeypatch.setattr(capture, "_validate_opus_audio_part", blocking_validate)
        recorder = SegmentedAudioRecorder(
            tmp_path / "capture",
            segment_seconds=60,
            recorder_factory=_Recorder,
            monotonic=lambda: next(clock),
            sleep=controlled_sleep,
            on_part=announced.append,
        )
        await recorder.start(TwoFrameTrack())
        await asyncio.wait_for(recorder.first_frame.wait(), timeout=1)

        rotation_gate.set()

        async def wait_for_validation() -> None:
            while not validation_started.is_set():
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_validation(), timeout=1)
        assert next_was_consuming == [True]
        assert len(_Recorder.instances) == 2
        assert _Recorder.instances[1].reader is not None

        event_loop_tick = asyncio.Event()
        asyncio.get_running_loop().call_soon(event_loop_tick.set)
        await asyncio.wait_for(event_loop_tick.wait(), timeout=1)
        assert announced == []

        release_validation.set()

        async def wait_for_checkpoint() -> None:
            while len(announced) < 1:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_checkpoint(), timeout=1)
        parts = await recorder.stop()
        assert [part.path.name for part in parts] == ["audio-0001.ogg", "audio-0002.ogg"]
        assert parts == tuple(announced)

    asyncio.run(exercise())


def test_source_ending_after_one_frame_keeps_the_short_part_as_degraded(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        announced: list[capture.RecordedAudioPart] = []
        clock = _times([100.0, 100.0, 105.0])

        class EndingTrack(MediaStreamTrack):
            kind = "audio"

            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            async def recv(self) -> object:
                self.calls += 1
                if self.calls == 1:
                    return object()
                self.stop()
                raise MediaStreamError

        class SourceEndingRecorder(_Recorder):
            def __init__(self, path: str, *, format: str) -> None:
                super().__init__(path, format=format)
                _write_opus_ogg(self.path, duration_seconds=0.02)

            async def start(self) -> None:
                assert self.track is not None

                async def consume() -> object:
                    frame = await self.track.recv()
                    with pytest.raises(MediaStreamError):
                        await self.track.recv()
                    return frame

                self.reader = asyncio.create_task(consume())

        recorder = SegmentedAudioRecorder(
            tmp_path / "capture",
            segment_seconds=60,
            recorder_factory=SourceEndingRecorder,
            monotonic=lambda: next(clock),
            on_part=announced.append,
        )
        await recorder.start(EndingTrack())
        await asyncio.wait_for(recorder.first_frame.wait(), timeout=1)
        failure = await asyncio.wait_for(asyncio.shield(recorder.fatal), timeout=1)
        assert "incoming PiKVM audio stream ended" in str(failure)

        with pytest.raises(MeetingCaptureConnectionError, match="stream ended") as caught:
            await recorder.stop()

        assert not isinstance(caught.value, MeetingCaptureLocalError)
        assert len(announced) == 1
        assert announced[0].degraded is True
        assert recorder.parts == tuple(announced)
        assert (tmp_path / "capture" / "audio-0001.ogg").exists()
        assert not (tmp_path / "capture" / "audio-0001.partial.ogg").exists()

    asyncio.run(exercise())


def test_source_end_preserves_a_full_valid_part_as_interrupted_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        announced: list[capture.RecordedAudioPart] = []
        ready: list[bool] = []
        clock = _times([100.0, 100.0, 100.0, 105.0, 105.0])

        class FiveSecondEndingTrack(MediaStreamTrack):
            kind = "audio"

            def __init__(self) -> None:
                super().__init__()
                self.index = 0

            async def recv(self) -> av.AudioFrame:
                if self.index >= 250:
                    self.stop()
                    raise MediaStreamError
                frame = av.AudioFrame(format="s16", layout="mono", samples=960)
                frame.sample_rate = 48_000
                frame.pts = self.index * 960
                for plane in frame.planes:
                    plane.update(bytes(plane.buffer_size))
                self.index += 1
                return frame

        settings = PiKVMSettings(
            base_url="https://pikvm.example",
            username="unused-test-user",
            password="unused-test-password",
        )
        recorder = capture.PiKVMWebRTCAudioCapture(
            settings,
            totp_provider=None,
            segment_seconds=60,
            monotonic=lambda: next(clock),
            max_reconnects=0,
        )

        async def source_ending_session(
            segmented: SegmentedAudioRecorder,
            *,
            stop_requested: asyncio.Event,
            on_heartbeat: Any,
        ) -> None:
            del stop_requested, on_heartbeat
            await segmented.start(FiveSecondEndingTrack())
            await asyncio.wait_for(segmented.first_frame.wait(), timeout=1)
            await asyncio.sleep(0)
            failure = await asyncio.wait_for(asyncio.shield(segmented.fatal), timeout=1)
            try:
                raise failure
            finally:
                await segmented.stop()

        monkeypatch.setattr(recorder, "_record_session", source_ending_session)
        result = await asyncio.wait_for(
            recorder.record(
                tmp_path / "capture",
                stop_requested=asyncio.Event(),
                on_ready=lambda: ready.append(True),
                on_part=announced.append,
            ),
            timeout=3,
        )

        assert result.interrupted is True
        assert result.interruption_code == "webrtc_disconnected"
        assert result.parts == tuple(announced)
        assert len(result.parts) == 1
        assert result.parts[0].path.name == "audio-0001.ogg"
        assert result.parts[0].duration_seconds == 5
        capture._validate_opus_audio_part(
            result.parts[0].path,
            expected_duration_seconds=5,
        )
        assert ready == [True]

    asyncio.run(exercise())


class _FramesThenSilenceTrack(MediaStreamTrack):
    """Delivers a fixed number of real Opus-encodable frames, then blocks until cancelled."""

    kind = "audio"

    def __init__(self, frames: int) -> None:
        super().__init__()
        self._remaining = frames
        self.index = 0

    async def recv(self) -> av.AudioFrame:
        if self._remaining <= 0:
            await asyncio.Future[None]()
        self._remaining -= 1
        frame = av.AudioFrame(format="s16", layout="mono", samples=960)
        frame.sample_rate = 48_000
        frame.pts = self.index * 960
        for plane in frame.planes:
            plane.update(bytes(plane.buffer_size))
        self.index += 1
        return frame


def _capture_settings() -> PiKVMSettings:
    return PiKVMSettings(
        base_url="https://pikvm.example",
        username="unused-test-user",
        password="unused-test-password",
        totp_required=False,
    )


async def _deliver_five_seconds(segmented: SegmentedAudioRecorder, clock: list[float]) -> None:
    track = _FramesThenSilenceTrack(250)
    await segmented.start(track)
    await asyncio.wait_for(segmented.first_frame.wait(), timeout=1)
    while track.index < 250:
        await asyncio.sleep(0)
    clock[0] += 5.0


def test_a_dropped_connection_after_audio_reconnects_and_continues_the_same_timeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        clock = [100.0]
        announced: list[capture.RecordedAudioPart] = []
        ready: list[bool] = []
        reconnects: list[int] = []
        stop_requested = asyncio.Event()
        attempts = 0
        recorder = capture.PiKVMWebRTCAudioCapture(
            _capture_settings(),
            totp_provider=None,
            segment_seconds=60,
            monotonic=lambda: clock[0],
            max_reconnects=3,
            reconnect_backoff_seconds=0,
        )

        async def flaky_session(
            segmented: SegmentedAudioRecorder,
            *,
            stop_requested: asyncio.Event,
            on_heartbeat: Any,
        ) -> None:
            nonlocal attempts
            attempts += 1
            del on_heartbeat
            if attempts == 1:
                await _deliver_five_seconds(segmented, clock)
                raise MeetingCaptureConnectionError("The PiKVM WebRTC media connection failed.")
            # The reconnect happens a while later; the gap must show in the offsets.
            clock[0] += 5.0
            await _deliver_five_seconds(segmented, clock)
            stop_requested.set()

        monkeypatch.setattr(recorder, "_record_session", flaky_session)
        result = await asyncio.wait_for(
            recorder.record(
                tmp_path / "capture",
                stop_requested=stop_requested,
                on_ready=lambda: ready.append(True),
                on_part=announced.append,
                on_reconnect=reconnects.append,
            ),
            timeout=5,
        )

        assert attempts == 2
        assert reconnects == [1]
        assert result.reconnects == 1
        assert result.interrupted is False
        assert result.interruption_code is None
        assert [part.path.name for part in result.parts] == ["audio-0001.ogg", "audio-0002.ogg"]
        assert [(part.offset_seconds, part.duration_seconds) for part in result.parts] == [
            (0.0, 5.0),
            (10.0, 5.0),
        ]
        assert result.duration_seconds == 15.0
        assert result.parts == tuple(announced)
        assert all(part.degraded is False for part in result.parts)
        # Readiness is announced once for the recording, not once per connection.
        assert ready == [True]

    asyncio.run(exercise())


def test_reconnects_are_bounded_and_then_reported_as_a_disconnect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        clock = [100.0]
        attempts = 0
        recorder = capture.PiKVMWebRTCAudioCapture(
            _capture_settings(),
            totp_provider=None,
            segment_seconds=60,
            monotonic=lambda: clock[0],
            max_reconnects=2,
            reconnect_backoff_seconds=0,
        )

        async def always_dropping(
            segmented: SegmentedAudioRecorder,
            *,
            stop_requested: asyncio.Event,
            on_heartbeat: Any,
        ) -> None:
            nonlocal attempts
            attempts += 1
            del stop_requested, on_heartbeat
            if attempts == 1:
                await _deliver_five_seconds(segmented, clock)
                raise MeetingCaptureConnectionError("dropped")
            # Later attempts cannot even reach the PiKVM.
            raise capture.MeetingCaptureUnreachableError("unreachable")

        monkeypatch.setattr(recorder, "_record_session", always_dropping)
        result = await asyncio.wait_for(
            recorder.record(tmp_path / "capture", stop_requested=asyncio.Event()),
            timeout=5,
        )

        assert attempts == 3
        assert result.reconnects == 2
        assert result.interrupted is True
        assert result.interruption_code == "webrtc_disconnected"
        assert [part.path.name for part in result.parts] == ["audio-0001.ogg"]

    asyncio.run(exercise())


def test_a_credential_rejection_during_reconnect_gives_up_immediately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        clock = [100.0]
        attempts = 0
        recorder = capture.PiKVMWebRTCAudioCapture(
            _capture_settings(),
            totp_provider=None,
            segment_seconds=60,
            monotonic=lambda: clock[0],
            max_reconnects=3,
            reconnect_backoff_seconds=0,
        )

        async def rejected_on_reconnect(
            segmented: SegmentedAudioRecorder,
            *,
            stop_requested: asyncio.Event,
            on_heartbeat: Any,
        ) -> None:
            nonlocal attempts
            attempts += 1
            del stop_requested, on_heartbeat
            if attempts == 1:
                await _deliver_five_seconds(segmented, clock)
                raise MeetingCaptureConnectionError("dropped")
            raise capture.MeetingCaptureAuthError("rejected")

        monkeypatch.setattr(recorder, "_record_session", rejected_on_reconnect)
        result = await asyncio.wait_for(
            recorder.record(tmp_path / "capture", stop_requested=asyncio.Event()),
            timeout=5,
        )

        assert attempts == 2
        assert result.reconnects == 1
        assert result.interrupted is True
        assert len(result.parts) == 1

    asyncio.run(exercise())


def test_a_first_session_that_never_delivers_audio_is_not_reconnected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        attempts = 0
        recorder = capture.PiKVMWebRTCAudioCapture(
            _capture_settings(),
            totp_provider=None,
            max_reconnects=3,
            reconnect_backoff_seconds=0,
        )

        async def never_connects(
            segmented: SegmentedAudioRecorder,
            *,
            stop_requested: asyncio.Event,
            on_heartbeat: Any,
        ) -> None:
            nonlocal attempts
            attempts += 1
            del segmented, stop_requested, on_heartbeat
            raise capture.MeetingCaptureUnreachableError("unreachable")

        monkeypatch.setattr(recorder, "_record_session", never_connects)
        with pytest.raises(capture.MeetingCaptureUnreachableError):
            await recorder.record(tmp_path / "capture", stop_requested=asyncio.Event())

        assert attempts == 1

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [(401, capture.MeetingCaptureAuthError), (403, capture.MeetingCaptureAuthError)],
)
def test_handshake_rejections_are_classified_as_auth_and_unreachable(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    expected: type[MeetingCaptureConnectionError],
) -> None:
    from websockets.datastructures import Headers
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Response

    async def exercise() -> None:
        recorder = capture.PiKVMWebRTCAudioCapture(_capture_settings(), totp_provider=None)

        async def rejected(*args: Any, **kwargs: Any) -> None:
            raise InvalidStatus(Response(status_code, "Unauthorized", Headers()))

        monkeypatch.setattr(capture, "connect", rejected)
        with pytest.raises(expected):
            await recorder._record_session(
                SegmentedAudioRecorder(Path("/nonexistent")),
                stop_requested=asyncio.Event(),
                on_heartbeat=None,
            )

        async def refused(*args: Any, **kwargs: Any) -> None:
            raise OSError("connection refused")

        monkeypatch.setattr(capture, "connect", refused)
        with pytest.raises(capture.MeetingCaptureUnreachableError):
            await recorder._record_session(
                SegmentedAudioRecorder(Path("/nonexistent")),
                stop_requested=asyncio.Event(),
                on_heartbeat=None,
            )

    asyncio.run(exercise())


def test_abort_ends_a_running_capture_with_the_given_local_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        recorder = capture.PiKVMWebRTCAudioCapture(_capture_settings(), totp_provider=None)
        session_started = asyncio.Event()

        async def waiting_session(
            segmented: SegmentedAudioRecorder,
            *,
            stop_requested: asyncio.Event,
            on_heartbeat: Any,
        ) -> None:
            del on_heartbeat
            fatal: asyncio.Future[MeetingCaptureConnectionError] = (
                asyncio.get_running_loop().create_future()
            )
            session_started.set()
            await capture._wait_until_stopped(
                stop_requested,
                fatal,
                segmented.fatal,
                abort=recorder._abort,
            )

        monkeypatch.setattr(recorder, "_record_session", waiting_session)
        task = asyncio.create_task(
            recorder.record(tmp_path / "capture", stop_requested=asyncio.Event())
        )
        await asyncio.wait_for(session_started.wait(), timeout=1)
        recorder.abort(MeetingCaptureLocalError("The stop watcher died."))

        with pytest.raises(MeetingCaptureLocalError, match="stop watcher died"):
            await asyncio.wait_for(task, timeout=2)

    asyncio.run(exercise())


def test_record_session_cleanup_preserves_source_connection_interruption() -> None:
    async def exercise() -> None:
        interruption = MeetingCaptureConnectionError(
            "The incoming PiKVM audio stream ended before capture stopped."
        )

        class SourceEndedRecorder:
            async def stop(self) -> tuple[capture.RecordedAudioPart, ...]:
                raise interruption

        result = await capture._recorder_finalization_error(SourceEndedRecorder())  # type: ignore[arg-type]

        assert result is interruption
        assert isinstance(result, MeetingCaptureLocalError) is False

    asyncio.run(exercise())


def test_janus_ack_does_not_complete_a_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        websocket = _WebSocket()
        monkeypatch.setattr(capture, "_transaction", lambda: "transaction-1")
        signaling = capture._JanusSignaling(websocket, timeout_seconds=1)
        try:
            request = asyncio.create_task(
                signaling.request(
                    {"janus": "create"},
                    predicate=lambda message: message.get("janus") == "success",
                )
            )
            sent = await asyncio.wait_for(websocket.sent.get(), timeout=1)
            assert sent == {"janus": "create", "transaction": "transaction-1"}

            await websocket.incoming.put(
                json.dumps({"janus": "ack", "transaction": "transaction-1"})
            )
            await asyncio.sleep(0)
            assert request.done() is False

            success = {
                "janus": "success",
                "transaction": "transaction-1",
                "data": {"id": 123},
            }
            await websocket.incoming.put(json.dumps(success))
            assert await asyncio.wait_for(request, timeout=1) == success
        finally:
            await signaling.close()
        assert websocket.closed is True

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("incoming", "expected"),
    [
        ("not-json password=secret", "PiKVM returned invalid WebRTC signaling data."),
        (OSError("password=secret"), "The PiKVM WebRTC signaling connection closed."),
    ],
)
def test_janus_fatal_input_is_sanitized(
    incoming: str | BaseException,
    expected: str,
) -> None:
    async def exercise() -> None:
        websocket = _WebSocket()
        signaling = capture._JanusSignaling(websocket, timeout_seconds=1)
        try:
            event = asyncio.create_task(signaling.plugin_event(lambda _: True))
            await websocket.incoming.put(incoming)
            with pytest.raises(MeetingCaptureConnectionError, match=re.escape(expected)) as error:
                await asyncio.wait_for(event, timeout=1)
            assert "secret" not in str(error.value)
        finally:
            await signaling.close()

    asyncio.run(exercise())


def test_pending_janus_request_fails_immediately_when_connection_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        websocket = _WebSocket()
        monkeypatch.setattr(capture, "_transaction", lambda: "transaction-1")
        signaling = capture._JanusSignaling(websocket, timeout_seconds=30)
        try:
            request = asyncio.create_task(
                signaling.request(
                    {"janus": "create"},
                    predicate=lambda message: message.get("janus") == "success",
                )
            )
            await asyncio.wait_for(websocket.sent.get(), timeout=1)
            await websocket.incoming.put(OSError("password=secret"))
            with pytest.raises(
                MeetingCaptureConnectionError,
                match=re.escape("The PiKVM WebRTC signaling connection closed."),
            ) as error:
                await asyncio.wait_for(request, timeout=1)
            assert "secret" not in str(error.value)
        finally:
            await signaling.close()

    asyncio.run(exercise())


def test_unsolicited_janus_error_fails_plugin_wait_without_exposing_detail() -> None:
    async def exercise() -> None:
        websocket = _WebSocket()
        signaling = capture._JanusSignaling(websocket, timeout_seconds=30)
        try:
            event = asyncio.create_task(
                signaling.plugin_event(
                    lambda message: capture._plugin_status(message) == "features"
                )
            )
            await websocket.incoming.put(
                json.dumps(
                    {
                        "janus": "error",
                        "transaction": "untracked-send",
                        "error": {"code": 403, "reason": "password=secret"},
                    }
                )
            )
            with pytest.raises(
                MeetingCaptureConnectionError,
                match=re.escape("PiKVM rejected a WebRTC signaling request."),
            ) as error:
                await asyncio.wait_for(event, timeout=1)
            assert "secret" not in str(error.value)
        finally:
            await signaling.close()

    asyncio.run(exercise())


def test_normal_pikvm_webrtc_lifecycle_is_receive_only_and_keeps_auth_off_wire_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        password = "SECRET-PIKVM-PASSWORD"
        totp_code = "123456"
        username = "SECRET-PIKVM-USER"
        stop_requested = asyncio.Event()
        ready: list[bool] = []
        websocket = LifecycleWebSocket()
        connect_calls: list[tuple[str, dict[str, Any]]] = []
        peer_connections: list[FakePeerConnection] = []
        recorders: list[FakeSegmentedRecorder] = []

        class TotpProvider:
            def current_code(self) -> str:
                return totp_code

        class IncomingAudioTrack(MediaStreamTrack):
            kind = "audio"

            def __init__(self) -> None:
                super().__init__()
                self.frames_read = 0

            async def recv(self) -> av.AudioFrame:
                self.frames_read += 1
                frame = av.AudioFrame(format="s16", layout="mono", samples=960)
                frame.sample_rate = 48_000
                frame.pts = 0
                return frame

        class FakeTransceiver:
            def __init__(self, kind: str) -> None:
                self.kind = kind
                self.direction = "sendrecv"

        class FakePeerConnection:
            def __init__(self, configuration: object) -> None:
                self.configuration = configuration
                self.connectionState = "new"
                self.localDescription: RTCSessionDescription | None = None
                self.remote_description: RTCSessionDescription | None = None
                self.handlers: dict[str, Any] = {}
                self.transceivers = [FakeTransceiver("audio"), FakeTransceiver("video")]
                self.incoming_track = IncomingAudioTrack()
                self.candidates: list[object | None] = []
                self.closed = False
                peer_connections.append(self)

            def on(self, event: str) -> Any:
                def register(callback: Any) -> Any:
                    self.handlers[event] = callback
                    return callback

                return register

            async def setRemoteDescription(self, description: RTCSessionDescription) -> None:
                self.remote_description = description
                self.handlers["track"](self.incoming_track)

            def getTransceivers(self) -> list[FakeTransceiver]:
                return self.transceivers

            async def createAnswer(self) -> RTCSessionDescription:
                return RTCSessionDescription(sdp="v=0\r\na=recvonly\r\n", type="answer")

            async def setLocalDescription(self, description: RTCSessionDescription) -> None:
                self.localDescription = description

            def getSenders(self) -> list[SimpleNamespace]:
                return [SimpleNamespace(track=None)]

            async def addIceCandidate(self, candidate: object | None) -> None:
                self.candidates.append(candidate)

            async def close(self) -> None:
                self.closed = True
                self.connectionState = "closed"

        class FakeSegmentedRecorder:
            def __init__(
                self,
                directory: Path,
                *,
                on_first_frame: Any = None,
                on_part: Any = None,
                **_: Any,
            ) -> None:
                self.directory = directory
                self.on_first_frame = on_first_frame
                self.on_part = on_part
                self.first_frame = asyncio.Event()
                self.fatal: asyncio.Future[MeetingCaptureLocalError] = (
                    asyncio.get_running_loop().create_future()
                )
                self.parts: tuple[capture.RecordedAudioPart, ...] = ()
                self.stop_calls = 0
                self.timeline_origin: float | None = None
                self.next_part_index = 1
                recorders.append(self)

            async def start(self, source: MediaStreamTrack) -> None:
                frame = await source.recv()
                assert isinstance(frame, av.AudioFrame)
                self.directory.mkdir(mode=0o700, parents=True)
                path = self.directory / "audio-0001.ogg"
                _write_opus_ogg(path)
                self.parts = (capture.RecordedAudioPart(path, 0.0, 1.0),)
                self.first_frame.set()
                if self.on_first_frame is not None:
                    self.on_first_frame()
                stop_requested.set()

            async def stop(self) -> tuple[capture.RecordedAudioPart, ...]:
                self.stop_calls += 1
                return self.parts

        async def fake_connect(uri: str, **kwargs: Any) -> LifecycleWebSocket:
            connect_calls.append((uri, kwargs))
            return websocket

        monkeypatch.setattr(capture, "connect", fake_connect)
        monkeypatch.setattr(capture, "RTCPeerConnection", FakePeerConnection)
        monkeypatch.setattr(capture, "SegmentedAudioRecorder", FakeSegmentedRecorder)

        settings = PiKVMSettings(
            base_url="https://pikvm.example/kvm",
            username=username,
            password=password,
            totp_required=True,
        )
        recorder = capture.PiKVMWebRTCAudioCapture(
            settings,
            totp_provider=TotpProvider(),
            signaling_timeout_seconds=1,
            audio_start_timeout_seconds=1,
        )
        result = await asyncio.wait_for(
            recorder.record(
                tmp_path / "session",
                stop_requested=stop_requested,
                on_ready=lambda: ready.append(True),
            ),
            timeout=2,
        )

        assert len(connect_calls) == 1
        uri, connect_options = connect_calls[0]
        assert uri == "wss://pikvm.example/kvm/janus/ws"
        assert connect_options["subprotocols"] == ["janus-protocol"]
        assert connect_options["additional_headers"] == {
            "X-KVMD-User": username,
            "X-KVMD-Passwd": password + totp_code,
            "User-Agent": "pikvm-work-agent/0.1.1",
        }
        assert connect_options["proxy"] is None
        assert connect_options["compression"] is None
        assert connect_options["host"] == "pikvm.example"
        assert connect_options["port"] == 443

        assert ready == [True]
        assert len(result.parts) == 1
        assert result.interrupted is False
        assert len(peer_connections) == 1
        peer = peer_connections[0]
        assert peer.incoming_track.frames_read == 1
        assert [(item.kind, item.direction) for item in peer.transceivers] == [
            ("audio", "recvonly"),
            ("video", "inactive"),
        ]
        assert all(sender.track is None for sender in peer.getSenders())
        assert peer.closed is True
        assert len(recorders) == 1
        assert recorders[0].stop_calls >= 1
        assert websocket.closed is True

        sent = websocket.messages
        assert [message["janus"] for message in sent] == [
            "create",
            "attach",
            "message",
            "message",
            "message",
            "message",
            "hangup",
            "detach",
            "destroy",
        ]
        assert [message.get("body", {}).get("request") for message in sent] == [
            None,
            None,
            "features",
            "watch",
            "start",
            "stop",
            None,
            None,
            None,
        ]
        assert sent[3]["body"] == capture.janus_watch_message()
        assert sent[4]["jsep"]["type"] == "answer"
        assert sent[4]["jsep"]["trickle"] is False

        wire_payloads = json.dumps(sent, sort_keys=True)
        assert password not in uri
        assert totp_code not in uri
        assert username not in uri
        assert password not in wire_payloads
        assert totp_code not in wire_payloads
        assert username not in wire_payloads
        assert password not in repr(result)
        assert totp_code not in repr(result)

    class LifecycleWebSocket(_WebSocket):
        def __init__(self) -> None:
            super().__init__()
            self.subprotocol = "janus-protocol"
            self.messages: list[dict[str, Any]] = []

        async def send(self, message: str) -> None:
            payload = json.loads(message)
            self.messages.append(payload)
            transaction = payload["transaction"]
            janus = payload["janus"]
            if janus == "create":
                await self.incoming.put(
                    json.dumps(
                        {
                            "janus": "success",
                            "transaction": transaction,
                            "data": {"id": 101},
                        }
                    )
                )
            elif janus == "attach":
                await self.incoming.put(
                    json.dumps(
                        {
                            "janus": "success",
                            "transaction": transaction,
                            "data": {"id": 202},
                        }
                    )
                )
            elif janus == "message":
                request = payload["body"]["request"]
                if request == "features":
                    await self.incoming.put(
                        _plugin_event(
                            {
                                "status": "features",
                                "features": {"audio": True, "ice": {}},
                            }
                        )
                    )
                elif request == "watch":
                    await self.incoming.put(
                        _plugin_event(
                            {"status": "starting"},
                            jsep={"type": "offer", "sdp": "v=0\r\nm=audio\r\n"},
                        )
                    )
                elif request == "start":
                    await self.incoming.put(_plugin_event({"status": "started"}))

    def _plugin_event(
        result: dict[str, Any],
        *,
        jsep: dict[str, str] | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "janus": "event",
            "session_id": 101,
            "sender": 202,
            "plugindata": {
                "plugin": "janus.plugin.ustreamer",
                "data": {"result": result},
            },
        }
        if jsep is not None:
            payload["jsep"] = jsep
        return json.dumps(payload)

    asyncio.run(exercise())
