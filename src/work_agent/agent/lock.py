from __future__ import annotations

import fcntl
import hashlib
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from work_agent.agent.errors import ControllerLockError

# A scheduled controller session is bounded to 180 seconds, so a caller that waits a little
# longer than that takes the endpoint as soon as the other workflow releases it instead of
# failing on contact with a launchd run or a dashboard job.
DEFAULT_LOCK_WAIT_SECONDS = 185.0

# Fixed per-user location rather than tempfile.gettempdir(): TMPDIR differs between a login
# shell, launchd, ssh, and `env -i`, and two processes with different lock paths would both drive
# HID on the same KVM while each believed it held the lock.
LOCK_DIRECTORY = Path.home() / "Library" / "Application Support" / "pikvm-work-agent" / "locks"


class ControllerLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: TextIO | None = None
        self._depth = 0

    @classmethod
    def for_endpoint(cls, endpoint: str, *, directory: Path | None = None) -> ControllerLock:
        endpoint_digest = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:16]
        target = directory or LOCK_DIRECTORY
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        return cls(target / f"pikvm-work-agent-{endpoint_digest}.lock")

    @property
    def held(self) -> bool:
        return self._handle is not None

    @property
    def path(self) -> Path:
        return self._path

    def __enter__(self) -> ControllerLock:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()

    def acquire(
        self,
        *,
        timeout_seconds: float = 0.0,
        poll_interval_seconds: float = 0.1,
        on_wait: Callable[[], None] | None = None,
    ) -> None:
        """Acquire this endpoint lock, optionally waiting for a live workflow to finish.

        The same lock instance is re-entrant so a workflow can hold one lease across several
        controller phases. Separate instances and processes still contend through ``flock``.
        """

        if timeout_seconds < 0:
            raise ValueError("Controller lock timeout cannot be negative.")
        if poll_interval_seconds <= 0:
            raise ValueError("Controller lock poll interval must be greater than zero.")
        if self._handle is not None:
            self._depth += 1
            return
        handle = self._path.open("a+", encoding="utf-8")
        deadline = time.monotonic() + timeout_seconds
        reported_wait = False
        try:
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    remaining = deadline - time.monotonic()
                    if timeout_seconds == 0 or remaining <= 0:
                        raise ControllerLockError(
                            "Another local agent controller is already using this PiKVM."
                        ) from exc
                    if not reported_wait and on_wait is not None:
                        on_wait()
                        reported_wait = True
                    time.sleep(min(poll_interval_seconds, remaining))
            handle.seek(0)
            handle.truncate()
            handle.write(f"pid={os.getpid()}\n")
            handle.flush()
            os.fchmod(handle.fileno(), 0o600)
        except BaseException:
            handle.close()
            raise
        self._handle = handle
        self._depth = 1

    def release(self) -> None:
        if self._handle is None:
            return
        if self._depth > 1:
            self._depth -= 1
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None
            self._depth = 0
