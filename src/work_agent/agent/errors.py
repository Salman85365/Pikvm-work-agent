from __future__ import annotations


class AgentError(Exception):
    """Base error for controller failures that are safe to show locally."""


class AgentConfigurationError(AgentError):
    """Agent controller configuration is missing or invalid."""


class PlannerError(AgentError):
    """The action planner failed without exposing provider details or secrets."""


class PlannerAuthenticationError(PlannerError):
    """OpenAI rejected the configured planner credentials."""


class PlannerPermissionError(PlannerError):
    """The OpenAI project cannot use the configured planner model."""


class PlannerRateLimitError(PlannerError):
    """OpenAI rate-limited the planner request."""


class PlannerNetworkError(PlannerError):
    """The OpenAI planner endpoint could not be reached."""


class PlannerTimeoutError(PlannerError):
    """The OpenAI planner request timed out."""


class PlannerServerError(PlannerError):
    """OpenAI returned a server-side planner failure."""


class PlannerRequestError(PlannerError):
    """OpenAI rejected a non-retryable planner request."""


class PlannerStructuredOutputError(PlannerError):
    """The planner did not return a valid typed proposal."""


class PolicyError(AgentError):
    """A proposal could not be safely classified."""


class ExecutionError(AgentError):
    """A validated action could not be mapped to the existing HID transport."""


class ScreenChangeError(AgentError):
    """Local screenshot comparison failed."""


class ControllerLockError(AgentError):
    """Another controller holds the local PiKVM session lock."""


class DebugArtifactError(AgentError):
    """Explicitly requested debug artifacts could not be written safely."""
