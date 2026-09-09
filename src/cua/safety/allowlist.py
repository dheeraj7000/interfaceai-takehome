"""
Safety Allowlist — configurable policy for what the agent is permitted to do.

The allowlist enforces:
1. Domain/URL restrictions — agent can only operate on approved domains and paths
2. Action type restrictions — which action types are permitted per domain
3. Risky action gating — irreversible actions require explicit confirmation

This is the outermost guardrail. Every action passes through the allowlist
before reaching the surface. If the allowlist rejects it, it doesn't execute.

Configuration is loaded from a YAML file (config/allowlist.yml) so it can
be reviewed, versioned, and customized per deployment.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from cua.schema.artifact import ActionType, RiskLevel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Policy models
# ---------------------------------------------------------------------------


class DomainPolicy(BaseModel):
    """Policy for a single domain or URL pattern."""
    pattern: str = Field(description="Regex pattern matching allowed URLs")
    allowed_actions: list[ActionType] = Field(
        default_factory=lambda: list(ActionType),
        description="Which action types are permitted on this domain"
    )
    blocked_paths: list[str] = Field(
        default_factory=list,
        description="Regex patterns for paths that are always blocked"
    )
    require_confirmation_for_risky: bool = Field(
        default=True,
        description="Whether risky actions need explicit confirmation on this domain"
    )
    max_actions_per_run: int = Field(
        default=100,
        description="Safety cap on total actions per run against this domain"
    )


class AllowlistConfig(BaseModel):
    """Top-level allowlist configuration."""
    domains: list[DomainPolicy] = Field(default_factory=list)
    global_blocked_actions: list[ActionType] = Field(
        default_factory=list,
        description="Action types blocked everywhere regardless of domain"
    )
    require_confirmation_for_risky: bool = Field(
        default=True,
        description="Global default: require confirmation for risky actions"
    )
    max_discovery_steps: int = Field(
        default=25,
        description="Max steps the discovery agent can take"
    )
    max_replay_steps: int = Field(
        default=50,
        description="Max steps a replay can execute"
    )


# ---------------------------------------------------------------------------
# Allowlist check results
# ---------------------------------------------------------------------------


class AllowlistDecision(BaseModel):
    """Result of an allowlist check."""
    allowed: bool
    reason: str
    requires_confirmation: bool = False
    risk_level: RiskLevel = RiskLevel.SAFE


# ---------------------------------------------------------------------------
# Allowlist engine
# ---------------------------------------------------------------------------


class Allowlist:
    """Evaluates actions against the configured safety policy."""

    def __init__(self, config: AllowlistConfig | None = None):
        self._config = config or self._default_config()
        self._action_count: dict[str, int] = {}  # domain -> count

    @classmethod
    def from_file(cls, path: str | Path) -> Allowlist:
        """Load allowlist from a YAML config file."""
        p = Path(path)
        if not p.exists():
            logger.warning(f"Allowlist file not found: {p}. Using defaults.")
            return cls()
        data = yaml.safe_load(p.read_text())
        config = AllowlistConfig(**data)
        return cls(config)

    @classmethod
    def _default_config(cls) -> AllowlistConfig:
        """Sensible defaults for development — allows localhost only."""
        return AllowlistConfig(
            domains=[
                DomainPolicy(
                    pattern=r"^https?://localhost(:\d+)?(/.*)?$",
                    allowed_actions=list(ActionType),
                    blocked_paths=[
                        r"/admin",
                        r"/api/delete",
                    ],
                    require_confirmation_for_risky=True,
                ),
                DomainPolicy(
                    pattern=r"^https?://127\.0\.0\.1(:\d+)?(/.*)?$",
                    allowed_actions=list(ActionType),
                    blocked_paths=[],
                    require_confirmation_for_risky=True,
                ),
                DomainPolicy(
                    pattern=r"^https?://target-app(:\d+)?(/.*)?$",
                    allowed_actions=list(ActionType),
                    blocked_paths=[],
                    require_confirmation_for_risky=True,
                ),
            ],
            require_confirmation_for_risky=True,
        )

    def check_url(self, url: str) -> AllowlistDecision:
        """Check if a URL is allowed."""
        for domain_policy in self._config.domains:
            if re.match(domain_policy.pattern, url):
                # Check blocked paths
                for blocked in domain_policy.blocked_paths:
                    if re.search(blocked, url):
                        return AllowlistDecision(
                            allowed=False,
                            reason=f"URL matches blocked path pattern: {blocked}",
                        )
                return AllowlistDecision(allowed=True, reason="URL matches allowed domain")

        return AllowlistDecision(
            allowed=False,
            reason=f"URL '{url}' does not match any allowed domain pattern",
        )

    def check_action(
        self,
        url: str,
        action: ActionType,
        risk_level: RiskLevel = RiskLevel.SAFE,
        confirmation_provided: bool = False,
    ) -> AllowlistDecision:
        """Check if an action is allowed at the given URL.

        Args:
            url: Current page URL
            action: The action type being attempted
            risk_level: How risky this action is
            confirmation_provided: Whether the caller has confirmed risky actions
        """
        # Check globally blocked actions
        if action in self._config.global_blocked_actions:
            return AllowlistDecision(
                allowed=False,
                reason=f"Action '{action.value}' is globally blocked by policy",
            )

        # Find matching domain policy
        matching_policy: DomainPolicy | None = None
        for domain_policy in self._config.domains:
            if re.match(domain_policy.pattern, url):
                matching_policy = domain_policy
                break

        if not matching_policy:
            return AllowlistDecision(
                allowed=False,
                reason=f"No domain policy matches URL: {url}",
            )

        # Check action type against domain policy
        if action not in matching_policy.allowed_actions:
            return AllowlistDecision(
                allowed=False,
                reason=f"Action '{action.value}' not permitted on this domain",
            )

        # Check action count
        domain_key = matching_policy.pattern
        count = self._action_count.get(domain_key, 0)
        if count >= matching_policy.max_actions_per_run:
            return AllowlistDecision(
                allowed=False,
                reason=f"Max actions per run ({matching_policy.max_actions_per_run}) exceeded",
            )

        # Check risky actions
        needs_confirmation = (
            risk_level in (RiskLevel.RISKY, RiskLevel.MODERATE)
            and matching_policy.require_confirmation_for_risky
        )

        if needs_confirmation and not confirmation_provided:
            return AllowlistDecision(
                allowed=False,
                reason=f"Risky action '{action.value}' requires confirmation",
                requires_confirmation=True,
                risk_level=risk_level,
            )

        # Allowed — increment counter
        self._action_count[domain_key] = count + 1

        return AllowlistDecision(
            allowed=True,
            reason="Action permitted by policy",
            risk_level=risk_level,
        )

    def reset_counters(self) -> None:
        """Reset action counters (call at start of each run)."""
        self._action_count.clear()

    @property
    def config(self) -> AllowlistConfig:
        return self._config
