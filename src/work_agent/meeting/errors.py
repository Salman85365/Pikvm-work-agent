from __future__ import annotations


class MeetingError(Exception):
    """A sanitized meeting workflow failure."""


class MeetingConfigurationError(MeetingError):
    """Meeting configuration is missing or unsafe."""


class MeetingStorageError(MeetingError):
    """A protected local meeting artifact could not be accessed."""


class MeetingStateConflictError(MeetingError):
    """Recorder state changed or another recording already owns it."""


class MeetingStateCorruptError(MeetingError):
    """Recorder state could not be validated safely."""
