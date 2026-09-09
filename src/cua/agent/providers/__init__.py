"""LLM provider adapters."""

from cua.agent.providers.base import (
    AgentAction,
    AgentDecision,
    ConversationMessage,
    DecisionType,
    ExtractedData,
    LLMProvider,
)
from cua.agent.providers.anthropic import AnthropicProvider
from cua.agent.providers.openai import OpenAIProvider


def create_provider(
    provider_name: str,
    api_key: str,
    model: str = "",
) -> LLMProvider:
    """Factory: create an LLM provider by name.

    Args:
        provider_name: "anthropic" or "openai"
        api_key: The API key for the provider
        model: Optional model override (uses provider default if empty)
    """
    if provider_name == "anthropic":
        return AnthropicProvider(
            api_key=api_key,
            model=model or "claude-sonnet-4-20250514",
        )
    elif provider_name == "openai":
        return OpenAIProvider(
            api_key=api_key,
            model=model or "gpt-4o",
        )
    else:
        raise ValueError(f"Unknown LLM provider: {provider_name}. Use 'anthropic' or 'openai'.")


__all__ = [
    "AgentAction",
    "AgentDecision",
    "AnthropicProvider",
    "ConversationMessage",
    "DecisionType",
    "ExtractedData",
    "LLMProvider",
    "OpenAIProvider",
    "create_provider",
]
