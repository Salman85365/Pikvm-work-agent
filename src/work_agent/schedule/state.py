from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from work_agent.schedule.errors import ScheduleError
from work_agent.slack.models import Availability, AvailabilityBatchResult

# A writer holds the state lock only for one small read-modify-write, so a wait this long means
# a stuck process, not contention; failing is better than a launchd job that never exits.
_LOCK_WAIT_SECONDS = 30.0
_LOCK_POLL_SECONDS = 0.05


@dataclass(frozen=True, slots=True)
class ReconciliationProfileState:
    availability: str
    verified_at: datetime | None


class ReconciliationStateStore:
    def __init__(
        self,
        path: Path | None = None,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._path = path or (
            Path.home()
            / "Library"
            / "Application Support"
            / "pikvm-work-agent"
            / "slack-availability-state.json"
        )
        self._now = now or (lambda: datetime.now(UTC))

    @property
    def path(self) -> Path:
        return self._path

    def profiles_requiring_reconciliation(
        self,
        profiles: tuple[str, ...],
        desired: Availability,
        *,
        verified_after: datetime | None = None,
    ) -> tuple[str, ...]:
        """Return the profiles whose recorded state does not prove ``desired`` is applied.

        A recorded match only counts if it was verified after ``verified_after`` (normally the
        last schedule boundary): a value verified before the boundary may have been forced by a
        run-now and says nothing about the state Slack shows now.
        """

        states = self.profile_states()
        due: list[str] = []
        for profile in profiles:
            recorded = states.get(profile)
            if recorded is None or recorded.availability != desired.value:
                due.append(profile)
                continue
            stale = verified_after is not None and (
                recorded.verified_at is None or recorded.verified_at <= verified_after
            )
            if stale:
                due.append(profile)
        return tuple(due)

    def applied_state(self) -> tuple[dict[str, str], datetime | None]:
        """Return the last verified state per profile for read-only inspection."""

        state = self._read()
        return self._normalized_applied(state), self._parse_datetime(state.get("updated_at"))

    def profile_states(self) -> dict[str, ReconciliationProfileState]:
        """Return each profile's last verified availability and verification time.

        State files created before per-profile timestamps were introduced remain useful: their
        aggregate ``updated_at`` value is used as the timestamp for every legacy profile.
        """

        state = self._read()
        applied = self._normalized_applied(state)
        return self._profile_states_from(state, applied)

    def record_successes(
        self,
        result: AvailabilityBatchResult,
        *,
        known_profiles: Iterable[str] | None = None,
    ) -> None:
        """Record verified matches; drop entries for profiles no longer in ``known_profiles``."""

        successes = {
            item.kvm: item.desired.value
            for item in result.results
            if item.success and item.desired is not None and item.observed == item.desired
        }
        keep = None if known_profiles is None else set(known_profiles) | set(successes)
        if not successes and not keep:
            return

        with self._exclusive_lock():
            state = self._read()
            normalized = self._normalized_applied(state)
            previous_profiles = self._profile_states_from(state, normalized)
            verified_at = {
                profile: details.verified_at.isoformat()
                for profile, details in previous_profiles.items()
                if details.verified_at is not None
            }
            if keep is not None:
                stale = [profile for profile in normalized if profile not in keep]
                if not successes and not stale:
                    return
                for profile in stale:
                    normalized.pop(profile, None)
                    verified_at.pop(profile, None)
            updated_at = self._now()
            for profile, availability in successes.items():
                normalized[profile] = availability
                verified_at[profile] = updated_at.isoformat()
            payload: dict[str, object] = {
                "updated_at": updated_at.isoformat(),
                "applied": normalized,
                "verified_at": verified_at,
            }
            self._write(payload)

    @staticmethod
    def _normalized_applied(state: dict[str, object]) -> dict[str, str]:
        applied = state.get("applied", {})
        if not isinstance(applied, dict):
            return {}
        return {
            profile: availability
            for profile, availability in applied.items()
            if isinstance(profile, str) and isinstance(availability, str)
        }

    @staticmethod
    def _parse_datetime(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    @classmethod
    def _profile_states_from(
        cls,
        state: dict[str, object],
        applied: dict[str, str],
    ) -> dict[str, ReconciliationProfileState]:
        legacy_updated_at = cls._parse_datetime(state.get("updated_at"))
        raw_verified_at = state.get("verified_at", {})
        verified_at = raw_verified_at if isinstance(raw_verified_at, dict) else {}
        return {
            profile: ReconciliationProfileState(
                availability=availability,
                verified_at=cls._parse_datetime(verified_at.get(profile)) or legacy_updated_at,
            )
            for profile, availability in applied.items()
        }

    def _read(self) -> dict[str, object]:
        if not self._path.exists():
            return {}
        try:
            loaded = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        lock_path = self._path.with_name(f"{self._path.name}.lock")
        descriptor: int | None = None
        try:
            self._prepare_parent()
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            os.fchmod(descriptor, 0o600)
            self._wait_for_lock(descriptor)
            yield
        except OSError:
            raise ScheduleError("The local reconciliation state could not be written.") from None
        finally:
            if descriptor is not None:
                with suppress(OSError):
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                with suppress(OSError):
                    os.close(descriptor)

    @staticmethod
    def _wait_for_lock(descriptor: int) -> None:
        deadline = time.monotonic() + _LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ScheduleError(
                        "The local reconciliation state is locked by another process that did "
                        f"not finish within {_LOCK_WAIT_SECONDS:g} seconds."
                    ) from None
                time.sleep(_LOCK_POLL_SECONDS)

    def _prepare_parent(self) -> None:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._path.parent.chmod(0o700)

    def _write(self, payload: dict[str, object]) -> None:
        temporary: Path | None = None
        descriptor: int | None = None
        try:
            self._prepare_parent()
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                dir=self._path.parent,
            )
            temporary = Path(temporary_name)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = None
                json.dump(payload, stream, ensure_ascii=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
        except OSError:
            raise ScheduleError("The local reconciliation state could not be written.") from None
        finally:
            if descriptor is not None:
                with suppress(OSError):
                    os.close(descriptor)
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)
