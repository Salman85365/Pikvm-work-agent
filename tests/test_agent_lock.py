from __future__ import annotations

import threading
from pathlib import Path

import pytest

from work_agent.agent.errors import ControllerLockError
from work_agent.agent.lock import ControllerLock


def test_only_one_controller_can_hold_local_lock(tmp_path: Path) -> None:
    path = tmp_path / "controller.lock"
    first = ControllerLock(path)
    second = ControllerLock(path)

    first.acquire()
    try:
        with pytest.raises(ControllerLockError, match="Another local agent controller"):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()


def test_controller_lock_can_queue_until_the_current_workflow_releases(tmp_path: Path) -> None:
    path = tmp_path / "controller.lock"
    first = ControllerLock(path)
    second = ControllerLock(path)
    waiting = threading.Event()
    acquired = threading.Event()
    errors: list[BaseException] = []

    def acquire_second() -> None:
        try:
            second.acquire(timeout_seconds=0.5, on_wait=waiting.set)
            acquired.set()
            second.release()
        except BaseException as exc:  # pragma: no cover - asserted through the parent thread
            errors.append(exc)

    first.acquire()
    thread = threading.Thread(target=acquire_second)
    thread.start()
    assert waiting.wait(timeout=0.25)
    first.release()
    thread.join(timeout=0.75)

    assert not thread.is_alive()
    assert errors == []
    assert acquired.is_set()


def test_the_same_lock_instance_is_reentrant_for_nested_controller_phases(
    tmp_path: Path,
) -> None:
    path = tmp_path / "controller.lock"
    workflow_lock = ControllerLock(path)
    contender = ControllerLock(path)

    workflow_lock.acquire()
    workflow_lock.acquire()
    workflow_lock.release()

    with pytest.raises(ControllerLockError):
        contender.acquire()

    workflow_lock.release()
    contender.acquire()
    contender.release()
