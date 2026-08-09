from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
from pathlib import Path
from typing import TextIO

from work_agent.agent.errors import ControllerLockError


class ControllerLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: TextIO | None = None

    @classmethod
    def for_endpoint(cls, endpoint: str) -> ControllerLock:
        endpoint_digest = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:16]
        return cls(Path(tempfile.gettempdir()) / f"pikvm-work-agent-{endpoint_digest}.lock")

    def __enter__(self) -> ControllerLock:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()

    def acquire(self) -> None:
        if self._handle is not None:
            return
        handle = self._path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise ControllerLockError(
                "Another local agent controller is already using this PiKVM."
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\n")
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None
