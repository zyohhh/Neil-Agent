"""Application-level exception hierarchy."""


class NeilAgentError(RuntimeError):
    """Base class for expected, user-facing application errors."""


class LLMError(NeilAgentError):
    """A model provider or response error."""


class AgentError(NeilAgentError):
    """An agent orchestration or loop error."""


class ToolError(NeilAgentError):
    """An expected tool validation or execution error."""
