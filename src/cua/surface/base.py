"""
Abstract Surface — the seam between the automation system and the application UI.

This abstraction is what makes the system extensible to different surface types
(web browser, legacy web, desktop app) without changing the agent loop, replay
engine, or artifact schema.

A Surface provides two capabilities:
1. Observation — read the current state (accessibility tree, screenshot, text)
2. Action — interact with the UI (click, type, navigate, etc.)

The recorded artifact is surface-agnostic: it stores actions and locators in
generic terms. The Surface implementation translates those into concrete
operations for the specific platform.

Design note: We deliberately keep this interface thin. A Surface is NOT
responsible for deciding what to do — that's the agent's job (discovery)
or the replay engine's job (replay). The Surface just perceives and acts.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ElementInfo:
    """Information about a UI element discovered on the surface."""
    role: str = ""                    # Accessibility role (button, textbox, link, etc.)
    name: str = ""                    # Accessibility name / label
    value: str = ""                   # Current value (for inputs)
    description: str = ""             # Accessibility description
    text: str = ""                    # Visible text content
    tag: str = ""                     # HTML tag or native control type
    is_visible: bool = True
    is_enabled: bool = True
    is_focused: bool = False
    bounding_box: dict[str, float] | None = None  # {x, y, width, height}
    attributes: dict[str, str] = field(default_factory=dict)  # Raw attributes
    children_count: int = 0
    # Locator hints for artifact recording
    css_selector: str = ""
    xpath: str = ""


@dataclass
class SurfaceState:
    """A snapshot of the current surface state.

    This is what the agent/replay engine "sees" at each step.
    It combines multiple observation modalities for robustness.
    """
    url: str = ""                               # Current URL (web) or window title (desktop)
    page_title: str = ""
    visible_text: str = ""                      # All visible text on the surface
    accessibility_tree: list[ElementInfo] = field(default_factory=list)
    accessibility_tree_text: str = ""           # Serialized a11y tree for LLM consumption
    screenshot_base64: str = ""                 # Base64-encoded screenshot (for vision models)
    screenshot_path: str = ""                   # Path if saved to disk
    html_snapshot: str = ""                     # Raw HTML (web only, for debugging)
    focused_element: ElementInfo | None = None
    error_text: str = ""                        # Any detected error messages on screen
    timestamp: float = 0.0


class Surface(abc.ABC):
    """Abstract base class for application surfaces.

    Implementations:
    - BrowserSurface (Playwright) — for web apps
    - DesktopSurface (stub) — for desktop apps via accessibility APIs
    """

    @abc.abstractmethod
    async def start(self, entry_point: str, **kwargs: Any) -> None:
        """Initialize and navigate to the entry point.

        Args:
            entry_point: URL (web) or app path/identifier (desktop)
        """

    @abc.abstractmethod
    async def stop(self) -> None:
        """Clean up resources (close browser, release handles)."""

    # ----- Observation -----

    @abc.abstractmethod
    async def observe(self) -> SurfaceState:
        """Capture the current state of the surface.

        Returns a SurfaceState with as many modalities as available:
        accessibility tree, screenshot, visible text, URL, etc.
        """

    @abc.abstractmethod
    async def screenshot(self, path: str | None = None) -> bytes:
        """Capture a screenshot. Optionally save to path. Return raw bytes."""

    @abc.abstractmethod
    async def get_visible_text(self) -> str:
        """Extract all visible text from the current surface."""

    @abc.abstractmethod
    async def find_element(
        self,
        strategy: str,
        value: str,
    ) -> ElementInfo | None:
        """Find a single element using a locator strategy.

        Args:
            strategy: One of the LocatorStrategy enum values
            value: The locator value

        Returns:
            ElementInfo if found, None otherwise
        """

    @abc.abstractmethod
    async def find_elements(
        self,
        strategy: str,
        value: str,
    ) -> list[ElementInfo]:
        """Find all matching elements."""

    @abc.abstractmethod
    async def element_exists(self, strategy: str, value: str) -> bool:
        """Check if an element exists on the surface."""

    @abc.abstractmethod
    async def get_element_text(self, strategy: str, value: str) -> str | None:
        """Get the text content of an element."""

    # ----- Action -----

    @abc.abstractmethod
    async def click(self, strategy: str, value: str) -> None:
        """Click an element."""

    @abc.abstractmethod
    async def type_text(self, strategy: str, value: str, text: str) -> None:
        """Type text into an element (clears existing content first)."""

    @abc.abstractmethod
    async def clear(self, strategy: str, value: str) -> None:
        """Clear an input field."""

    @abc.abstractmethod
    async def select_option(self, strategy: str, value: str, option: str) -> None:
        """Select an option from a dropdown/select element."""

    @abc.abstractmethod
    async def press_key(self, key: str) -> None:
        """Press a keyboard key (Enter, Tab, Escape, etc.)."""

    @abc.abstractmethod
    async def navigate(self, url: str) -> None:
        """Navigate to a URL or screen."""

    @abc.abstractmethod
    async def scroll(self, direction: str = "down", amount: int = 300) -> None:
        """Scroll the page."""

    @abc.abstractmethod
    async def hover(self, strategy: str, value: str) -> None:
        """Hover over an element."""

    @abc.abstractmethod
    async def wait_for_element(
        self,
        strategy: str,
        value: str,
        timeout_ms: int = 10000,
    ) -> bool:
        """Wait for an element to appear. Returns True if found within timeout."""

    @abc.abstractmethod
    async def wait_for_text(
        self,
        text: str,
        timeout_ms: int = 10000,
    ) -> bool:
        """Wait for specific text to appear on the surface."""

    # ----- Session management (for escalation) -----

    @abc.abstractmethod
    async def pause(self) -> dict[str, str]:
        """Pause automation and return session access info.

        Returns a dict with connection details for human takeover,
        e.g. {"novnc_url": "...", "cdp_url": "..."}.
        """

    @abc.abstractmethod
    async def resume(self) -> None:
        """Resume automation after human handoff."""

    @abc.abstractmethod
    async def is_alive(self) -> bool:
        """Check if the surface session is still active."""
