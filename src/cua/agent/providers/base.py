"""
Abstract LLM Provider — the interface between the agent loop and the model.

The provider receives the current surface state (accessibility tree + screenshot)
and the goal context, and returns a structured decision: what action to take next.

Design:
- The provider does NOT control the loop — it just makes one decision per call.
- The agent loop (loop.py) calls the provider, executes the action, observes
  the result, and calls the provider again. This keeps the loop logic
  provider-agnostic.
- The provider returns structured AgentDecision objects, not raw text.
  This forces clean separation between LLM reasoning and execution.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DecisionType(str, Enum):
    """What the agent decides to do."""
    ACTION = "action"          # Perform an action on the surface
    GOAL_COMPLETE = "goal_complete"  # The goal has been achieved
    GOAL_FAILED = "goal_failed"      # The goal cannot be achieved
    STUCK = "stuck"            # The agent doesn't know what to do
    NEEDS_INFO = "needs_info"  # The agent needs more information


@dataclass
class AgentAction:
    """A concrete action the agent wants to perform."""
    action_type: str       # ActionType value: click, type, navigate, etc.
    target_strategy: str   # LocatorStrategy value
    target_value: str      # Locator value
    target_semantic: str   # Human-readable description of the target
    value: str = ""        # Payload: text to type, URL to navigate to, etc.
    reasoning: str = ""    # Why the agent chose this action


@dataclass
class ExtractedData:
    """Data the agent extracted from the surface."""
    name: str
    value: str
    source: str = ""   # Where it was extracted from


@dataclass
class AgentDecision:
    """The agent's decision for one step of the loop."""
    decision_type: DecisionType
    action: AgentAction | None = None
    extracted_data: list[ExtractedData] = field(default_factory=list)
    reasoning: str = ""
    goal_status: str = ""   # Summary of progress toward the goal
    confidence: float = 0.0  # 0.0 to 1.0


@dataclass
class ConversationMessage:
    """A message in the agent's conversation history."""
    role: str   # "system", "user", "assistant"
    content: Any  # Text or multimodal content


class LLMProvider(abc.ABC):
    """Abstract base for LLM providers used in the discovery agent loop."""

    @abc.abstractmethod
    async def decide(
        self,
        goal: str,
        surface_state_text: str,
        screenshot_base64: str | None,
        step_number: int,
        max_steps: int,
        history: list[ConversationMessage],
        previous_actions: list[str],
    ) -> AgentDecision:
        """Make a decision given the current state.

        Args:
            goal: The natural-language goal to achieve
            surface_state_text: Serialized accessibility tree / visible text
            screenshot_base64: Screenshot for vision models (optional)
            step_number: Current step number in the loop
            max_steps: Maximum steps allowed
            history: Conversation history for context
            previous_actions: Summary of actions taken so far

        Returns:
            AgentDecision with the chosen action or completion signal
        """

    @abc.abstractmethod
    async def generate_artifact_metadata(
        self,
        goal: str,
        actions_taken: list[str],
        extracted_data: list[ExtractedData],
    ) -> dict[str, Any]:
        """After a successful run, generate metadata for the artifact.

        The LLM summarizes what was accomplished and suggests names,
        descriptions, and parameter schemas for the capability.
        """

    @property
    @abc.abstractmethod
    def model_name(self) -> str:
        """The model identifier being used."""
