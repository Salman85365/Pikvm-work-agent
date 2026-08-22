from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from work_agent.meeting.errors import MeetingStorageError

_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
_SAFE_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


@dataclass(frozen=True, slots=True)
class MeetingArtifacts:
    directory: Path
    manifest: Path
    transcript: Path
    intelligence: Path
    report: Path


class MeetingStorage:
    """Create private per-KVM meeting directories and atomic local artifacts."""

    def __init__(self, root: Path) -> None:
        self._root = root.expanduser().resolve()

    @property
    def root(self) -> Path:
        return self._root

    def create_session(
        self,
        *,
        kvm: str,
        session_id: str,
        started_at: datetime,
    ) -> MeetingArtifacts:
        directory = self.session_directory(
            kvm=kvm,
            session_id=session_id,
            started_at=started_at,
        )
        safe_kvm = directory.parents[1].name
        date = directory.parent.name
        try:
            self._secure_directory(self._root)
            self._secure_directory(self._root / safe_kvm)
            self._secure_directory(self._root / safe_kvm / date)
            directory.mkdir(mode=0o700)
            directory.chmod(0o700)
            self.require_contained(directory)
        except FileExistsError:
            raise MeetingStorageError("That local meeting session already exists.") from None
        except OSError:
            raise MeetingStorageError(
                "The protected local meeting directory could not be created."
            ) from None
        return MeetingArtifacts(
            directory=directory,
            manifest=directory / "manifest.json",
            transcript=directory / "transcript.json",
            intelligence=directory / "intelligence.json",
            report=directory / "report.md",
        )

    def session_directory(
        self,
        *,
        kvm: str,
        session_id: str,
        started_at: datetime,
    ) -> Path:
        """Return a validated session path without creating any artifacts."""

        if started_at.tzinfo is None:
            raise MeetingStorageError("Meeting start time must include a timezone.")
        safe_kvm = _component(kvm, "KVM")
        safe_session = _component(session_id, "meeting session")
        date = started_at.astimezone(UTC).date().isoformat()
        return self.require_contained(self._root / safe_kvm / date / safe_session)

    def require_expected_session_directory(
        self,
        directory: Path,
        *,
        kvm: str,
        session_id: str,
        started_at: datetime,
    ) -> Path:
        expected = self.session_directory(
            kvm=kvm,
            session_id=session_id,
            started_at=started_at,
        )
        actual = self.require_session_directory(directory)
        if actual != expected:
            raise MeetingStorageError(
                "The meeting session directory does not match its KVM and session."
            )
        return actual

    def artifact_path(self, directory: Path, filename: str) -> Path:
        if (
            Path(filename).name != filename
            or filename in {"", ".", ".."}
            or _SAFE_FILENAME.fullmatch(filename) is None
        ):
            raise MeetingStorageError(
                "Meeting artifact filenames must be plain sanitized filenames."
            )
        safe_directory = self.require_session_directory(directory)
        path = safe_directory / filename
        if path.is_symlink():
            raise MeetingStorageError("A meeting artifact path must not be a symbolic link.")
        self.require_contained(path)
        return path

    def input_artifact_path(self, directory: Path, filename: str) -> Path:
        """Return one existing regular artifact scoped to the exact session directory."""

        path = self.artifact_path(directory, filename)
        try:
            metadata = path.lstat()
        except OSError:
            raise MeetingStorageError(
                "A protected meeting input artifact is unavailable."
            ) from None
        if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
            raise MeetingStorageError("A protected meeting input artifact must be a regular file.")
        if path.resolve().parent != self.require_session_directory(directory):
            raise MeetingStorageError("A meeting input artifact crossed its session boundary.")
        return path

    def require_session_directory(self, directory: Path) -> Path:
        absolute = Path(os.path.abspath(directory.expanduser()))
        resolved = self.require_contained(absolute)
        try:
            metadata = absolute.lstat()
        except OSError:
            raise MeetingStorageError(
                "The protected local meeting session directory is unavailable."
            ) from None
        if absolute != resolved or not stat.S_ISDIR(metadata.st_mode) or absolute.is_symlink():
            raise MeetingStorageError(
                "The meeting session directory must be a real private directory."
            )
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise MeetingStorageError("The meeting session directory must use mode 0700.")
        return resolved

    def prepare_output(self, path: Path, *, exist_ok: bool = False) -> Path:
        self.require_contained(path)
        self.require_session_directory(path.parent)
        if path.is_symlink():
            raise MeetingStorageError("A meeting artifact path must not be a symbolic link.")
        try:
            descriptor = os.open(
                path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            os.close(descriptor)
            path.chmod(0o600)
            _sync_directory(path.parent)
        except FileExistsError:
            if exist_ok:
                return self.input_artifact_path(path.parent, path.name)
            raise MeetingStorageError(
                "A protected meeting artifact could not be created."
            ) from None
        except OSError:
            raise MeetingStorageError(
                "A protected meeting artifact could not be created."
            ) from None
        return path

    @contextmanager
    def capture_lock(self, directory: Path) -> Iterator[bool]:
        """Try to hold the private recorder lease for one exact session."""

        path = self.artifact_path(directory, "capture.lock")
        descriptor: int | None = None
        acquired = False
        try:
            descriptor = os.open(
                path,
                os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            descriptor_stat = os.fstat(descriptor)
            path_stat = path.lstat()
            if (
                not stat.S_ISREG(descriptor_stat.st_mode)
                or descriptor_stat.st_dev != path_stat.st_dev
                or descriptor_stat.st_ino != path_stat.st_ino
            ):
                raise OSError("meeting capture lock is not the expected regular file")
            os.fchmod(descriptor, 0o600)
            _sync_directory(path.parent)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            acquired = True
            yield True
        except MeetingStorageError:
            raise
        except OSError:
            raise MeetingStorageError(
                "The protected meeting capture lock is unavailable."
            ) from None
        finally:
            if descriptor is not None:
                if acquired:
                    with suppress(OSError):
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                with suppress(OSError):
                    os.close(descriptor)

    def capture_lock_held(self, directory: Path) -> bool:
        """Return whether another process owns the exact session recorder lease."""

        path = self.artifact_path(directory, "capture.lock")
        descriptor: int | None = None
        acquired = False
        try:
            descriptor = os.open(
                path,
                os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            )
            descriptor_stat = os.fstat(descriptor)
            path_stat = path.lstat()
            if (
                not stat.S_ISREG(descriptor_stat.st_mode)
                or descriptor_stat.st_dev != path_stat.st_dev
                or descriptor_stat.st_ino != path_stat.st_ino
            ):
                raise OSError("meeting capture lock is not the expected regular file")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            acquired = True
            return False
        except FileNotFoundError:
            return False
        except MeetingStorageError:
            raise
        except OSError:
            raise MeetingStorageError(
                "The protected meeting capture lock is unavailable."
            ) from None
        finally:
            if descriptor is not None:
                if acquired:
                    with suppress(OSError):
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                with suppress(OSError):
                    os.close(descriptor)

    def read_text(self, path: Path) -> str:
        safe_path = self.input_artifact_path(path.parent, path.name)
        descriptor: int | None = None
        try:
            descriptor = os.open(safe_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError("meeting input is not a regular file")
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = None
                return stream.read()
        except OSError:
            raise MeetingStorageError(
                "A protected meeting input artifact is unavailable."
            ) from None
        finally:
            if descriptor is not None:
                with suppress(OSError):
                    os.close(descriptor)

    def write_json(self, path: Path, payload: Mapping[str, object]) -> None:
        content = json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n"
        self.write_text(path, content)

    def write_text(self, path: Path, content: str) -> None:
        self.require_contained(path)
        self.require_session_directory(path.parent)
        self._atomic_write(path, content)

    def finalize(self, partial: Path, destination: Path) -> Path:
        self.require_contained(partial)
        self.require_contained(destination)
        if partial.parent != destination.parent:
            raise MeetingStorageError("Meeting artifacts can only be finalized in their session.")
        self.require_session_directory(partial.parent)
        if partial.is_symlink() or destination.is_symlink():
            raise MeetingStorageError("A meeting artifact path must not be a symbolic link.")
        try:
            os.replace(partial, destination)
            destination.chmod(0o600)
            _sync_directory(destination.parent)
        except OSError:
            raise MeetingStorageError(
                "The local meeting recording could not be finalized."
            ) from None
        return destination

    def remove_empty_partial(self, path: Path) -> bool:
        self.require_contained(path)
        self.require_session_directory(path.parent)
        if path.is_symlink():
            raise MeetingStorageError("A meeting artifact path must not be a symbolic link.")
        try:
            if not path.exists() or path.stat().st_size != 0:
                return False
            path.unlink()
            _sync_directory(path.parent)
            return True
        except OSError:
            raise MeetingStorageError("The empty partial recording could not be removed.") from None

    def require_contained(self, path: Path) -> Path:
        resolved = path.expanduser().resolve()
        if resolved != self._root and self._root not in resolved.parents:
            raise MeetingStorageError(
                "A meeting artifact path escaped the configured data directory."
            )
        return resolved

    def _secure_directory(self, path: Path) -> None:
        existed = path.exists()
        if path.is_symlink():
            raise MeetingStorageError("Meeting data directories must not be symbolic links.")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.require_contained(path)
        if not existed:
            path.chmod(0o700)
        metadata = path.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise MeetingStorageError(
                "Meeting data directories must already be private (mode 0700)."
            )

    def _atomic_write(self, path: Path, content: str) -> None:
        temporary: Path | None = None
        descriptor: int | None = None
        try:
            self.require_session_directory(path.parent)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
            )
            temporary = Path(temporary_name)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = None
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            path.chmod(0o600)
            _sync_directory(path.parent)
        except OSError:
            raise MeetingStorageError(
                "A protected meeting artifact could not be written."
            ) from None
        finally:
            if descriptor is not None:
                with suppress(OSError):
                    os.close(descriptor)
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)


def _component(value: str, label: str) -> str:
    normalized = value.strip().lower() if label == "KVM" else value.strip()
    if not normalized or _SAFE_COMPONENT.fullmatch(normalized) is None:
        raise MeetingStorageError(
            f"The {label} identifier must use only letters, numbers, hyphens, or underscores."
        )
    return normalized


def _sync_directory(path: Path) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY)
        os.fsync(descriptor)
    except OSError:
        return
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
