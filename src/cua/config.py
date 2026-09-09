"""Central configuration — loaded from environment and config files."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ARTIFACTS_DIR = PROJECT_ROOT / "artifacts"
EVIDENCE_DIR = PROJECT_ROOT / "evidence"
CONFIG_DIR = PROJECT_ROOT / "config"


# ---------------------------------------------------------------------------
# Settings model
# ---------------------------------------------------------------------------
class Settings(BaseModel):
    """Runtime settings — sourced from env vars with sane defaults."""

    # LLM
    llm_provider: str = Field(
        default_factory=lambda: os.getenv("LLM_PROVIDER", "anthropic")
    )
    anthropic_api_key: str = Field(
        default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", "")
    )
    openai_api_key: str = Field(
        default_factory=lambda: os.getenv("OPENAI_API_KEY", "")
    )
    gemini_api_key: str = Field(
        default_factory=lambda: os.getenv("GEMINI_API_KEY", "")
    )
    llm_model: str = Field(
        default_factory=lambda: os.getenv("LLM_MODEL", "")
    )

    # Target
    target_app_url: str = Field(
        default_factory=lambda: os.getenv("TARGET_APP_URL", "http://localhost:5000")
    )

    # Escalation
    novnc_url: str = Field(
        default_factory=lambda: os.getenv("NOVNC_URL", "http://localhost:6080")
    )

    # Safety limits
    max_discovery_steps: int = Field(
        default_factory=lambda: int(os.getenv("MAX_DISCOVERY_STEPS", "25"))
    )
    max_step_retries: int = Field(
        default_factory=lambda: int(os.getenv("MAX_STEP_RETRIES", "3"))
    )
    step_timeout_ms: int = Field(
        default_factory=lambda: int(os.getenv("STEP_TIMEOUT_MS", "10000"))
    )

    # Observability
    evidence_screenshots: bool = Field(
        default_factory=lambda: os.getenv("EVIDENCE_SCREENSHOTS", "true").lower() == "true"
    )
    max_screenshots_per_run: int = Field(
        default_factory=lambda: int(os.getenv("MAX_SCREENSHOTS_PER_RUN", "50"))
    )

    @property
    def active_model(self) -> str:
        """Return the model name to use, with provider-specific defaults."""
        if self.llm_model:
            return self.llm_model
        if self.llm_provider == "anthropic":
            return "claude-sonnet-4-20250514"
        if self.llm_provider == "gemini":
            return "gemini-flash-lite-latest"
        return "gpt-4o"


def get_settings() -> Settings:
    """Singleton-ish settings accessor."""
    return Settings()
