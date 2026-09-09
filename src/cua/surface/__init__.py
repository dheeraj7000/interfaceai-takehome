"""Surface abstraction — how we perceive and act on application UIs."""

from cua.surface.base import ElementInfo, Surface, SurfaceState
from cua.surface.browser import BrowserSurface
from cua.surface.desktop import DesktopSurface

__all__ = [
    "BrowserSurface",
    "DesktopSurface",
    "ElementInfo",
    "Surface",
    "SurfaceState",
]
