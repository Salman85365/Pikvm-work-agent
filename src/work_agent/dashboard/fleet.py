"""Read-only per-KVM liveness for the dashboard: reachability, lock contention, failure streaks.

Nothing here authenticates, sends HID input, or holds a lock. The reachability probe is an
unauthenticated GET that expects PiKVM to answer 401 (or 200), and the lock probe takes and
immediately drops a non-blocking ``flock`` so it can only observe another workflow, never block
one.
"""

from __future__ import annotations

import fcntl
import logging
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from work_agent.agent.lock import LOCK_DIRECTORY, ControllerLock
from work_agent.dashboard.models import KvmAlert, KvmProfile, KvmStatus
from work_agent.schedule.runlog import (
    CONNECTIVITY_STOP_CODES,
    STOP_CODE_CATEGORY_LABELS,
    FailureStreak,
)

_LOGGER = logging.getLogger(__name__)

PROBE_TIMEOUT_SECONDS = 5.0
PROBE_CACHE_SECONDS = 60.0
# The banner fires when a KVM's newest runs all stopped because the PiKVM could not be used.
ALERT_MIN_STREAK = 2
_INFO_PATH = "/api/info"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    reachable: bool
    # Sanitized transport summary such as "HTTP 401" or "connect timeout"; never a URL or body.
    detail: str
    checked_at: datetime


Prober = Callable[[str, bool], ProbeResult]


def probe_endpoint(base_url: str, verify_ssl: bool) -> ProbeResult:
    """GET ``<base_url>/api/info`` without credentials; any HTTP answer means reachable."""

    checked_at = datetime.now(UTC)
    try:
        response = httpx.get(
            base_url.rstrip("/") + _INFO_PATH,
            timeout=PROBE_TIMEOUT_SECONDS,
            verify=verify_ssl,
            follow_redirects=False,
        )
    except httpx.ConnectTimeout:
        return ProbeResult(False, "connect timeout", checked_at)
    except httpx.TimeoutException:
        return ProbeResult(False, "request timeout", checked_at)
    except httpx.ConnectError:
        return ProbeResult(False, "connection refused or host unreachable", checked_at)
    except httpx.HTTPError as exc:
        return ProbeResult(False, type(exc).__name__, checked_at)
    return ProbeResult(True, f"HTTP {response.status_code}", checked_at)


class ReachabilityCache:
    """Cache unauthenticated reachability probes per endpoint for a short window."""

    def __init__(
        self,
        *,
        prober: Prober | None = None,
        ttl_seconds: float = PROBE_CACHE_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._prober = prober or probe_endpoint
        self._ttl = ttl_seconds
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._results: dict[str, tuple[float, ProbeResult]] = {}

    def check(self, base_url: str, *, verify_ssl: bool) -> ProbeResult:
        now = self._monotonic()
        with self._lock:
            cached = self._results.get(base_url)
            if cached is not None and now - cached[0] < self._ttl:
                return cached[1]
        result = self._prober(base_url, verify_ssl)
        with self._lock:
            self._results[base_url] = (self._monotonic(), result)
        return result

    def check_many(self, endpoints: list[tuple[str, bool]]) -> dict[str, ProbeResult]:
        """Probe several endpoints concurrently so one slow KVM does not delay the others."""

        unique = list(dict.fromkeys(endpoints))
        if not unique:
            return {}
        with ThreadPoolExecutor(max_workers=min(8, len(unique))) as pool:
            results = pool.map(lambda item: self.check(item[0], verify_ssl=item[1]), unique)
        return {base_url: result for (base_url, _), result in zip(unique, results, strict=True)}


def workflow_running(endpoint: str, *, directory: Path | None = None) -> bool:
    """Report whether some process currently holds the controller lock for ``endpoint``.

    The lock file is opened read-only and never truncated; a non-blocking exclusive ``flock``
    is attempted and released immediately, so this can only observe, never hold or block.
    """

    lock_directory = directory or LOCK_DIRECTORY
    # ControllerLock owns the endpoint-to-file mapping; reuse it rather than re-deriving the
    # digest here, so the probe can never look at a different file than the real lock.
    path = ControllerLock.for_endpoint(endpoint, directory=lock_directory).path
    if not path.exists():
        return False
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return False
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def _alert_for(streak: FailureStreak | None) -> KvmAlert | None:
    if streak is None or streak.count < ALERT_MIN_STREAK:
        return None
    code = streak.last_stop_code
    if code not in CONNECTIVITY_STOP_CODES:
        return None
    return KvmAlert(
        stop_code=code,
        label=STOP_CODE_CATEGORY_LABELS.get(code, code),
        count=streak.count,
        since=streak.unreachable_since or streak.first_at,
        reason=streak.last_error,
    )


def kvm_statuses(
    profiles: list[KvmProfile],
    *,
    streaks: dict[str, FailureStreak],
    reachability: ReachabilityCache | None,
    lock_directory: Path | None = None,
) -> list[KvmStatus]:
    probes: dict[str, ProbeResult] = {}
    if reachability is not None:
        endpoints = [
            (profile.endpoint, bool(profile.verify_ssl))
            for profile in profiles
            if profile.configured and profile.endpoint
        ]
        try:
            probes = reachability.check_many(endpoints)
        except Exception as exc:  # A probe must never take the whole snapshot down.
            _LOGGER.warning("PiKVM reachability probe failed: %s", type(exc).__name__)

    statuses: list[KvmStatus] = []
    for profile in profiles:
        streak = streaks.get(profile.name)
        probe = probes.get(profile.endpoint or "")
        running = False
        if profile.endpoint:
            try:
                running = workflow_running(profile.endpoint, directory=lock_directory)
            except OSError:
                running = False
        statuses.append(
            KvmStatus(
                name=profile.name,
                endpoint=profile.endpoint,
                reachable=probe.reachable if probe is not None else None,
                reachability_detail=probe.detail if probe is not None else None,
                checked_at=probe.checked_at if probe is not None else None,
                unreachable_since=streak.unreachable_since if streak is not None else None,
                workflow_running=running,
                consecutive_failures=streak.count if streak is not None else 0,
                last_run_at=streak.latest_at if streak is not None else None,
                last_run_outcome=(
                    None
                    if streak is None or streak.latest_success is None
                    else ("success" if streak.latest_success else "failure")
                ),
                last_stop_code=streak.last_stop_code if streak is not None else None,
                alert=_alert_for(streak),
            )
        )
    return statuses
