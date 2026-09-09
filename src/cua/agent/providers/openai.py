"""
OpenAI GPT-4o Provider — uses GPT-4o's vision + structured output for decisions.

GPT-4o is strong at:
- Vision understanding of screenshots
- Structured JSON output
- Function/tool calling

Same prompt contract as the Anthropic provider — the agent loop doesn't
need to know which model is driving decisions.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import openai

from cua.agent.providers.base import (
    AgentAction,
    AgentDecision,
    ConversationMessage,
    DecisionType,
    ExtractedData,
    LLMProvider,
)

logger = logging.getLogger(__name__)

# Same system prompt as Anthropic — keeps behavior consistent
SYSTEM_PROMPT = """You are a computer-use automation agent operating a legacy banking back-office application.
Your job is to accomplish a specific goal by observing the current screen state and deciding what action to take next.

You receive:
1. The goal to accomplish
2. The current accessibility tree (structured list of UI elements)
3. A screenshot of the current screen
4. History of actions you've already taken

You must respond with a JSON object containing your decision. The JSON must have this exact structure:

{
  "decision_type": "action" | "goal_complete" | "goal_failed" | "stuck",
  "reasoning": "Your step-by-step reasoning about what you see and what to do",
  "goal_status": "Brief summary of progress toward the goal",
  "confidence": 0.0 to 1.0,
  "action": {
    "action_type": "click" | "type" | "navigate" | "select" | "clear" | "press_key" | "wait" | "extract" | "screenshot",
    "target_strategy": "a11y_role" | "a11y_label" | "text_content" | "css_selector" | "xpath",
    "target_value": "the locator value — for a11y_role use 'role:name' format, for a11y_label use the label text",
    "target_semantic": "human-readable description of what element you're targeting",
    "value": "text to type, URL to navigate to, key to press, or empty"
  },
  "extracted_data": [
    {"name": "field_name", "value": "extracted_value", "source": "where you found it"}
  ]
}

Rules:
- For action_type "type": set target to the input field, value to the text to type
- For action_type "click": set target to the button/link, value can be empty
- For action_type "navigate": set target_strategy to "css_selector", target_value to "", value to the URL
- For action_type "select": set target to the dropdown, value to the option to select
- For action_type "press_key": set value to the key name (Enter, Tab, Escape, etc.)
- For action_type "extract": include the data in extracted_data array
- When the goal is complete, set decision_type to "goal_complete" and include any extracted data
- When stuck or unable to proceed, set decision_type to "stuck" with reasoning
- IMPORTANT: The app uses HTML frames. The main content is in a frame. Elements from the accessibility tree are what you can interact with.
- For a11y_role targets, use format "role:name" (e.g. "textbox:Member ID" or "button:Search")
- For a11y_label targets, use the aria-label value directly
- Always respond with valid JSON only, no markdown or extra text.
"""


class OpenAIProvider(LLMProvider):
    """GPT-4o-based LLM provider."""

    def __init__(self, api_key: str, model: str = "gpt-4o"):
        self._client = openai.AsyncOpenAI(api_key=api_key)
        self._model = model

    @property
    def model_name(self) -> str:
        return self._model

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
        """Ask GPT-4o to decide the next action."""

        # Build content array with text and optional image
        content: list[dict[str, Any]] = []

        actions_summary = "\n".join(
            f"  Step {i+1}: {a}" for i, a in enumerate(previous_actions)
        ) if previous_actions else "  (none yet)"

        text_content = f"""GOAL: {goal}

STEP: {step_number} of {max_steps}

PREVIOUS ACTIONS:
{actions_summary}

CURRENT ACCESSIBILITY TREE:
{surface_state_text[:8000]}

Decide your next action. Respond with JSON only."""

        content.append({"type": "text", "text": text_content})

        # Add screenshot if available
        if screenshot_base64:
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{screenshot_base64}",
                    "detail": "high",
                },
            })

        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                max_tokens=1024,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": content},
                ],
                response_format={"type": "json_object"},
            )

            response_text = response.choices[0].message.content or "{}"
            decision_data = json.loads(response_text)
            return self._parse_decision(decision_data)

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse GPT-4o response as JSON: {e}")
            return AgentDecision(
                decision_type=DecisionType.STUCK,
                reasoning=f"Failed to parse model response: {e}",
            )
        except Exception as e:
            logger.error(f"OpenAI API call failed: {e}")
            return AgentDecision(
                decision_type=DecisionType.STUCK,
                reasoning=f"API error: {e}",
            )

    async def generate_artifact_metadata(
        self,
        goal: str,
        actions_taken: list[str],
        extracted_data: list[ExtractedData],
    ) -> dict[str, Any]:
        """Ask GPT-4o to generate artifact metadata."""
        actions_text = "\n".join(f"  {i+1}. {a}" for i, a in enumerate(actions_taken))
        data_text = "\n".join(
            f"  - {d.name}: {d.value}" for d in extracted_data
        ) if extracted_data else "  (none)"

        prompt = f"""Based on this completed automation run, generate metadata for a reusable capability artifact.

ORIGINAL GOAL: {goal}

ACTIONS TAKEN:
{actions_text}

DATA EXTRACTED:
{data_text}

Return a JSON object with:
{{
  "name": "machine_readable_name (snake_case)",
  "display_name": "Human Readable Name",
  "description": "What this capability does, for both human reviewers and AI agents",
  "tags": ["tag1", "tag2"],
  "input_parameters": [
    {{"name": "param_name", "type": "string", "required": true, "description": "what this param is", "sensitive": false}}
  ],
  "output_fields": [
    {{"name": "field_name", "type": "string", "description": "what this output is"}}
  ]
}}"""

        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
            text = response.choices[0].message.content or "{}"
            return json.loads(text)
        except Exception as e:
            logger.error(f"Failed to generate artifact metadata: {e}")
            return {
                "name": "unnamed_capability",
                "display_name": "Unnamed Capability",
                "description": goal,
                "tags": [],
                "input_parameters": [],
                "output_fields": [],
            }

    def _parse_decision(self, data: dict[str, Any]) -> AgentDecision:
        """Parse a JSON decision into an AgentDecision."""
        decision_type = DecisionType(data.get("decision_type", "stuck"))

        action = None
        if data.get("action") and decision_type == DecisionType.ACTION:
            act = data["action"]
            action = AgentAction(
                action_type=act.get("action_type", "wait"),
                target_strategy=act.get("target_strategy", "text_content"),
                target_value=act.get("target_value", ""),
                target_semantic=act.get("target_semantic", ""),
                value=act.get("value", ""),
                reasoning=data.get("reasoning", ""),
            )

        extracted = []
        for item in data.get("extracted_data", []):
            extracted.append(ExtractedData(
                name=item.get("name", ""),
                value=item.get("value", ""),
                source=item.get("source", ""),
            ))

        return AgentDecision(
            decision_type=decision_type,
            action=action,
            extracted_data=extracted,
            reasoning=data.get("reasoning", ""),
            goal_status=data.get("goal_status", ""),
            confidence=float(data.get("confidence", 0.5)),
        )
