"""Application-level exception hierarchy."""


class NeilAgentError(RuntimeError):
    """Base class for expected, user-facing application errors."""


class LLMError(NeilAgentError):
    """A model provider or response error."""


class AgentError(NeilAgentError):
    """An agent orchestration or loop error."""


class ToolError(NeilAgentError):
    """An expected tool validation or execution error."""


class SessionError(NeilAgentError):
    """A local session storage or validation error."""


class InstructionError(NeilAgentError):
    """A project-instruction load, initialization, or reload error."""


class HookError(NeilAgentError):
    """A lifecycle hook registration, decision, or callback error."""


class AuditError(NeilAgentError):
    """A bounded local audit-log initialization or write error."""


class ApprovalError(NeilAgentError):
    """A non-interactive approval request is invalid, stale, or already used."""
