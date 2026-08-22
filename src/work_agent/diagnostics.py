"""Local diagnostic logging.

Every workflow used to reduce its failure to one sanitized sentence and keep nothing else, so a
KVM that went offline for a day was indistinguishable from a planner bug. This module writes a
rotating, owner-only log under ``~/Library/Logs/pikvm-work-agent`` with what the code itself
authored: controller states, stop codes, exception classes and their sanitized messages, and
transport events. It deliberately never receives screen content, model prose, credentials, or
TOTP material; callers must keep to that rule.
"""

from __future__ import annotations

import contextlib
import logging
import os
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

from pydantic import ValidationError

LOG_DIRECTORY = Path.home() / "Library" / "Logs" / "pikvm-work-agent"
LOG_FILE_NAME = "agent.log"
_ROOT_LOGGER_NAME = "work_agent"
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 3
_configured_path: Path | None = None


class _OwnerOnlyRotatingFileHandler(RotatingFileHandler):
    def _open(self):  # type: ignore[no-untyped-def]
        stream = super()._open()
        with contextlib.suppress(OSError):
            os.fchmod(stream.fileno(), 0o600)
        return stream


def configure_logging(
    *,
    directory: Path | None = None,
    level: int = logging.INFO,
) -> Path | None:
    """Attach the rotating file handler once per process; return its path, or None if unwritable.

    Logging must never take a workflow down, so a missing or read-only log directory is reported
    on stderr through the logging module's own fallback and otherwise ignored.
    """

    global _configured_path
    if _configured_path is not None:
        return _configured_path
    target_directory = directory or LOG_DIRECTORY
    path = target_directory / LOG_FILE_NAME
    try:
        target_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        handler = _OwnerOnlyRotatingFileHandler(
            path,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
    except OSError:
        return None
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [%(process)d] %(name)s: %(message)s",
        )
    )
    logger = logging.getLogger(_ROOT_LOGGER_NAME)
    logger.setLevel(level)
    logger.addHandler(handler)
    logger.propagate = False
    _configured_path = path
    return path


def describe_exception(exc: BaseException) -> str:
    """``ClassName: sanitized message`` plus the class names of the cause chain.

    Only the outermost message is included: wrapped causes such as pydantic validation errors or
    HTTP client errors can carry model output or response bodies, so they contribute their class
    name alone.
    """

    parts = [_outer_description(exc)]
    causes: list[str] = []
    seen: set[int] = {id(exc)}
    current: BaseException | None = exc.__cause__ or exc.__context__
    while current is not None and id(current) not in seen and len(causes) < 5:
        seen.add(id(current))
        causes.append(type(current).__name__)
        current = current.__cause__ or current.__context__
    if causes:
        parts.append(f"(caused by {' <- '.join(causes)})")
    return " ".join(parts)


def _outer_description(exc: BaseException) -> str:
    if isinstance(exc, ValidationError):
        # str(ValidationError) embeds the rejected input, which for a perception schema is
        # screen content; keep the field paths and error types only.
        details = ", ".join(
            f"{'.'.join(str(part) for part in error['loc']) or '<root>'}={error['type']}"
            for error in exc.errors()[:8]
        )
        return f"ValidationError: {exc.error_count()} error(s) [{details}]"
    message = str(exc)
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def format_traceback(exc: BaseException) -> str:
    """Frames of the outer exception only; the cause chain would print wrapped messages."""

    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__, chain=False))


def log_exception(logger: logging.Logger, message: str, exc: BaseException) -> None:
    logger.error("%s: %s\n%s", message, describe_exception(exc), format_traceback(exc).rstrip())
