from __future__ import annotations


class VisionError(Exception):
    """Base error for screen-analysis failures that are safe to show in the CLI."""


class VisionConfigurationError(VisionError):
    """Local OpenAI screen-analysis configuration is missing or invalid."""


class VisionAuthenticationError(VisionError):
    """OpenAI rejected the configured API credentials."""


class VisionPermissionError(VisionError):
    """The OpenAI project cannot use the requested model or operation."""


class VisionRateLimitError(VisionError):
    """OpenAI rate-limited the screen-analysis request."""


class VisionNetworkError(VisionError):
    """The OpenAI API could not be reached."""


class VisionTimeoutError(VisionError):
    """The OpenAI screen-analysis request timed out."""


class VisionServerError(VisionError):
    """OpenAI returned a server-side failure."""


class VisionRequestError(VisionError):
    """OpenAI rejected a non-retryable screen-analysis request."""


class VisionStructuredOutputError(VisionError):
    """OpenAI did not return a valid structured screen analysis."""


class VisionImageError(VisionError):
    """A supplied screenshot could not be decoded safely."""


class VisionCoordinateError(VisionError):
    """Normalized coordinates cannot be converted for the supplied screen size."""
