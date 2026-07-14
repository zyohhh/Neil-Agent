"""Tests for application exception boundaries."""

from neil_agent.errors import AgentError, LLMError, NeilAgentError, ToolError


def test_layer_errors_share_user_facing_base_class() -> None:
    assert issubclass(AgentError, NeilAgentError)
    assert issubclass(LLMError, NeilAgentError)
    assert issubclass(ToolError, NeilAgentError)
