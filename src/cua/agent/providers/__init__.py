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
from cua.agent.providers.gemini import GeminiProvider


def create_provider(
    provider_name: str,
    api_key: str,
    model: str = "",
) -> LLMProvider:
    """Factory: create an LLM provider by name.

    Args:
        provider_name: "anthropic", "openai", or "gemini"
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
    elif provider_name == "gemini":
        return GeminiProvider(
            api_key=api_key,
            model=model or "gemini-flash-lite-latest",
        )
    else:
        raise ValueError(
            f"Unknown LLM provider: {provider_name}. Use 'anthropic', 'openai', or 'gemini'."
        )


__all__ = [
    "AgentAction",
    "AgentDecision",
    "AnthropicProvider",
    "ConversationMessage",
    "DecisionType",
    "ExtractedData",
    "GeminiProvider",
    "LLMProvider",
    "OpenAIProvider",
    "create_provider",
]
