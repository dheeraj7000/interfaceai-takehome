"""
Desktop Surface — Design stub for native desktop application automation.

This is an intentional design seam, not a TODO. The Surface abstraction
is shared between browser and desktop implementations, which is the key
architectural decision: the artifact schema and replay engine don't need
to know which surface type they're operating on.

A real implementation would use platform accessibility APIs:
- Windows: UI Automation (UIA) via pywinauto or comtypes
- macOS: AXUIElement via pyobjc / atomacos
- Linux: AT-SPI via pyatspi2

The observation path (accessibility tree) is the same concept across all
platforms — that's why we chose it as our primary observation modality
in the browser implementation too. It's the only approach that works
consistently across web, legacy web, and desktop.

The action path would translate our generic actions (click, type, navigate)
into platform-specific calls (UIA.Invoke, AXPress, AT-SPI.DoAction).
"""

from __future__ import annotations

import logging
from typing import Any

from cua.surface.base import ElementInfo, Surface, SurfaceState

logger = logging.getLogger(__name__)


class DesktopSurface(Surface):
    """Stub implementation for desktop app automation.

    Raises NotImplementedError for all methods — exists to document
    the extension point and ensure the Surface interface is complete
    enough for desktop use.

    Implementation notes for a real version:
    - start() would launch the application process and attach to its window
    - observe() would use platform a11y APIs to build the element tree
    - click/type/etc would use a11y APIs or OS-level input simulation
    - screenshot() would use platform screen capture
    - pause/resume would keep the app process alive, just release/reacquire
      the automation handle
    """

    def __init__(self, platform: str = "windows"):
        self._platform = platform

    async def start(self, entry_point: str, **kwargs: Any) -> None:
        raise NotImplementedError(
            f"Desktop surface ({self._platform}) is a design stub. "
            "See desktop.py docstring for implementation notes."
        )

    async def stop(self) -> None:
        raise NotImplementedError("Desktop surface is a design stub.")

    async def observe(self) -> SurfaceState:
        raise NotImplementedError("Desktop surface is a design stub.")

    async def screenshot(self, path: str | None = None) -> bytes:
        raise NotImplementedError("Desktop surface is a design stub.")

    async def get_visible_text(self) -> str:
        raise NotImplementedError("Desktop surface is a design stub.")

    async def find_element(self, strategy: str, value: str) -> ElementInfo | None:
        raise NotImplementedError("Desktop surface is a design stub.")

    async def find_elements(self, strategy: str, value: str) -> list[ElementInfo]:
        raise NotImplementedError("Desktop surface is a design stub.")

    async def element_exists(self, strategy: str, value: str) -> bool:
        raise NotImplementedError("Desktop surface is a design stub.")

    async def get_element_text(self, strategy: str, value: str) -> str | None:
        raise NotImplementedError("Desktop surface is a design stub.")

    async def click(self, strategy: str, value: str) -> None:
        raise NotImplementedError("Desktop surface is a design stub.")

    async def type_text(self, strategy: str, value: str, text: str) -> None:
        raise NotImplementedError("Desktop surface is a design stub.")

    async def clear(self, strategy: str, value: str) -> None:
        raise NotImplementedError("Desktop surface is a design stub.")

    async def select_option(self, strategy: str, value: str, option: str) -> None:
        raise NotImplementedError("Desktop surface is a design stub.")

    async def press_key(self, key: str) -> None:
        raise NotImplementedError("Desktop surface is a design stub.")

    async def navigate(self, url: str) -> None:
        raise NotImplementedError("Desktop surface is a design stub.")

    async def scroll(self, direction: str = "down", amount: int = 300) -> None:
        raise NotImplementedError("Desktop surface is a design stub.")

    async def hover(self, strategy: str, value: str) -> None:
        raise NotImplementedError("Desktop surface is a design stub.")

    async def wait_for_element(self, strategy: str, value: str, timeout_ms: int = 10000) -> bool:
        raise NotImplementedError("Desktop surface is a design stub.")

    async def wait_for_text(self, text: str, timeout_ms: int = 10000) -> bool:
        raise NotImplementedError("Desktop surface is a design stub.")

    async def pause(self) -> dict[str, str]:
        raise NotImplementedError("Desktop surface is a design stub.")

    async def resume(self) -> None:
        raise NotImplementedError("Desktop surface is a design stub.")

    async def is_alive(self) -> bool:
        raise NotImplementedError("Desktop surface is a design stub.")
