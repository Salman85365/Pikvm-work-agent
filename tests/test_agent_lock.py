from __future__ import annotations

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
