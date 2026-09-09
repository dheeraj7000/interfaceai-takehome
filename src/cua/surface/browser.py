"""
Browser Surface — Playwright implementation.

Uses Playwright for action execution and the accessibility tree for observation.
This hybrid approach gives us:
- Fast, reliable actions via Playwright's DOM automation
- Stable element identification via the accessibility tree (works even on hostile DOMs)
- Screenshots for LLM vision and evidence capture

The accessibility tree is the primary observation modality because it's more
stable than raw DOM selectors on legacy apps, and the same concept (a11y APIs)
exists on desktop platforms — making this the credible bridge to desktop extension.

Frame handling: Legacy bank apps use framesets. We handle this by targeting
the correct frame for actions. The observe() method captures state from all frames.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Frame,
    Page,
    Playwright,
    async_playwright,
)

from cua.schema.artifact import LocatorStrategy
from cua.surface.base import ElementInfo, Surface, SurfaceState

logger = logging.getLogger(__name__)


class BrowserSurface(Surface):
    """Playwright-based browser surface implementation."""

    def __init__(
        self,
        headless: bool = True,
        slow_mo: int = 0,
        viewport: dict[str, int] | None = None,
        novnc_url: str = "",
    ):
        self._headless = headless
        self._slow_mo = slow_mo
        self._viewport = viewport or {"width": 1280, "height": 900}
        self._novnc_url = novnc_url

        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._paused = False

    @property
    def page(self) -> Page:
        if not self._page:
            raise RuntimeError("Surface not started. Call start() first.")
        return self._page

    def _active_frame(self) -> Frame | Page:
        """Get the active frame — handles frameset-based legacy apps.

        If the page uses frames, we target the 'workspace' frame (our legacy
        bank app's main content frame). Otherwise we return the page itself.
        """
        page = self.page
        # Check for our known frameset structure
        for frame in page.frames:
            if frame.name == "workspace":
                return frame
        # Fallback: if there are child frames, try the largest one
        if len(page.frames) > 1:
            # Return first non-main frame (skip the top-level frame)
            for frame in page.frames:
                if frame != page.main_frame and frame.url and "about:" not in frame.url:
                    return frame
        return page

    async def start(self, entry_point: str, **kwargs: Any) -> None:
        """Launch browser and navigate to entry point."""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self._headless,
            slow_mo=self._slow_mo,
        )
        self._context = await self._browser.new_context(
            viewport=self._viewport,
            ignore_https_errors=True,
        )
        self._page = await self._context.new_page()

        # Navigate and wait for the frameset to load
        await self._page.goto(entry_point, wait_until="domcontentloaded", timeout=30000)
        # Give frames a moment to load
        await asyncio.sleep(1)
        logger.info(f"Browser surface started at {entry_point}")

    async def stop(self) -> None:
        """Close browser and clean up."""
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        logger.info("Browser surface stopped")

    # ----- Observation -----

    async def observe(self) -> SurfaceState:
        """Capture comprehensive state from the browser."""
        frame = self._active_frame()
        timestamp = time.time()

        # Capture accessibility tree
        a11y_tree = await self._get_accessibility_tree(frame)
        a11y_text = self._serialize_a11y_tree(a11y_tree)

        # Capture visible text
        visible_text = await self._get_visible_text_from_frame(frame)

        # Capture screenshot
        screenshot_bytes = await self.page.screenshot(type="png")
        screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

        # Get URL and title
        url = frame.url if hasattr(frame, "url") else self.page.url
        try:
            title = await self.page.title()
        except Exception:
            title = ""

        return SurfaceState(
            url=url,
            page_title=title,
            visible_text=visible_text,
            accessibility_tree=a11y_tree,
            accessibility_tree_text=a11y_text,
            screenshot_base64=screenshot_b64,
            timestamp=timestamp,
        )

    async def screenshot(self, path: str | None = None) -> bytes:
        """Capture screenshot, optionally save to path."""
        kwargs: dict[str, Any] = {"type": "png", "full_page": False}
        if path:
            kwargs["path"] = path
        return await self.page.screenshot(**kwargs)

    async def get_visible_text(self) -> str:
        """Extract visible text from the active frame."""
        frame = self._active_frame()
        return await self._get_visible_text_from_frame(frame)

    async def _get_visible_text_from_frame(self, frame: Frame | Page) -> str:
        """Extract visible text content from a frame."""
        try:
            return await frame.evaluate("""
                () => {
                    const walk = (node) => {
                        if (node.nodeType === Node.TEXT_NODE) {
                            const text = node.textContent.trim();
                            return text ? text + ' ' : '';
                        }
                        if (node.nodeType !== Node.ELEMENT_NODE) return '';
                        const style = window.getComputedStyle(node);
                        if (style.display === 'none' || style.visibility === 'hidden') return '';
                        let result = '';
                        for (const child of node.childNodes) {
                            result += walk(child);
                        }
                        const tag = node.tagName.toLowerCase();
                        if (['br', 'p', 'div', 'tr', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'td', 'th'].includes(tag)) {
                            result += '\\n';
                        }
                        return result;
                    };
                    return walk(document.body).replace(/\\n{3,}/g, '\\n\\n').trim();
                }
            """)
        except Exception as e:
            logger.warning(f"Failed to extract visible text: {e}")
            return ""

    async def _get_accessibility_tree(self, frame: Frame | Page) -> list[ElementInfo]:
        """Build accessibility tree from the frame using Playwright's a11y snapshot."""
        try:
            snapshot = await frame.accessibility.snapshot(interesting_only=True)  # type: ignore[union-attr]
            if not snapshot:
                return []
            elements: list[ElementInfo] = []
            self._walk_a11y_node(snapshot, elements)
            return elements
        except Exception as e:
            logger.warning(f"Failed to get a11y tree: {e}")
            return []

    def _walk_a11y_node(
        self,
        node: dict[str, Any],
        elements: list[ElementInfo],
        depth: int = 0,
    ) -> None:
        """Recursively walk the accessibility tree snapshot."""
        role = node.get("role", "")
        name = node.get("name", "")
        value = node.get("value", "")

        # Skip generic/container roles with no name
        if role not in ("generic", "none", "") or name:
            elements.append(ElementInfo(
                role=role,
                name=name,
                value=str(value) if value else "",
                description=node.get("description", ""),
                text=name,
                is_focused=node.get("focused", False),
                is_enabled=not node.get("disabled", False),
            ))

        for child in node.get("children", []):
            self._walk_a11y_node(child, elements, depth + 1)

    def _serialize_a11y_tree(self, elements: list[ElementInfo]) -> str:
        """Serialize a11y tree into a text format for LLM consumption."""
        lines = []
        for el in elements:
            parts = [f"[{el.role}]"]
            if el.name:
                parts.append(f'"{el.name}"')
            if el.value:
                parts.append(f"value={el.value}")
            if not el.is_enabled:
                parts.append("(disabled)")
            if el.is_focused:
                parts.append("(focused)")
            lines.append(" ".join(parts))
        return "\n".join(lines)

    # ----- Element finding -----

    async def find_element(self, strategy: str, value: str) -> ElementInfo | None:
        """Find a single element using the specified locator strategy."""
        frame = self._active_frame()
        try:
            locator = self._resolve_locator(frame, strategy, value)
            # Check if it exists
            count = await locator.count()
            if count == 0:
                return None
            el = locator.first
            # Gather info
            tag = await el.evaluate("el => el.tagName.toLowerCase()") if await el.count() > 0 else ""
            text = ""
            try:
                text = (await el.inner_text()).strip()
            except Exception:
                pass
            bbox = await el.bounding_box()
            return ElementInfo(
                role=strategy,
                name=value,
                text=text,
                tag=tag,
                is_visible=await el.is_visible(),
                is_enabled=await el.is_enabled(),
                bounding_box=bbox,
            )
        except Exception as e:
            logger.debug(f"find_element({strategy}, {value}) failed: {e}")
            return None

    async def find_elements(self, strategy: str, value: str) -> list[ElementInfo]:
        """Find all matching elements."""
        frame = self._active_frame()
        try:
            locator = self._resolve_locator(frame, strategy, value)
            count = await locator.count()
            elements = []
            for i in range(count):
                el = locator.nth(i)
                text = ""
                try:
                    text = (await el.inner_text()).strip()
                except Exception:
                    pass
                elements.append(ElementInfo(
                    role=strategy,
                    name=value,
                    text=text,
                    is_visible=await el.is_visible(),
                ))
            return elements
        except Exception as e:
            logger.debug(f"find_elements({strategy}, {value}) failed: {e}")
            return []

    async def element_exists(self, strategy: str, value: str) -> bool:
        """Check if an element exists."""
        frame = self._active_frame()
        try:
            locator = self._resolve_locator(frame, strategy, value)
            return await locator.count() > 0
        except Exception:
            return False

    async def get_element_text(self, strategy: str, value: str) -> str | None:
        """Get text content of an element."""
        frame = self._active_frame()
        try:
            locator = self._resolve_locator(frame, strategy, value)
            if await locator.count() == 0:
                return None
            return (await locator.first.inner_text()).strip()
        except Exception:
            return None

    # ----- Actions -----

    async def click(self, strategy: str, value: str) -> None:
        """Click an element."""
        frame = self._active_frame()
        locator = self._resolve_locator(frame, strategy, value)
        await locator.first.click(timeout=10000)
        logger.debug(f"Clicked: {strategy}={value}")

    async def type_text(self, strategy: str, value: str, text: str) -> None:
        """Type text into an element — clears first, then fills."""
        frame = self._active_frame()
        locator = self._resolve_locator(frame, strategy, value)
        await locator.first.fill(text, timeout=10000)
        logger.debug(f"Typed into {strategy}={value}: {text[:20]}...")

    async def clear(self, strategy: str, value: str) -> None:
        """Clear an input field."""
        frame = self._active_frame()
        locator = self._resolve_locator(frame, strategy, value)
        await locator.first.fill("", timeout=10000)

    async def select_option(self, strategy: str, value: str, option: str) -> None:
        """Select a dropdown option by value or label."""
        frame = self._active_frame()
        locator = self._resolve_locator(frame, strategy, value)
        try:
            # Try by value first
            await locator.first.select_option(value=option, timeout=5000)
        except Exception:
            # Fall back to label
            await locator.first.select_option(label=option, timeout=5000)
        logger.debug(f"Selected option '{option}' in {strategy}={value}")

    async def press_key(self, key: str) -> None:
        """Press a keyboard key."""
        await self.page.keyboard.press(key)
        logger.debug(f"Pressed key: {key}")

    async def navigate(self, url: str) -> None:
        """Navigate to a URL."""
        await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(0.5)  # Let frames load
        logger.debug(f"Navigated to: {url}")

    async def scroll(self, direction: str = "down", amount: int = 300) -> None:
        """Scroll the page."""
        delta = amount if direction == "down" else -amount
        await self.page.mouse.wheel(0, delta)

    async def hover(self, strategy: str, value: str) -> None:
        """Hover over an element."""
        frame = self._active_frame()
        locator = self._resolve_locator(frame, strategy, value)
        await locator.first.hover(timeout=10000)

    async def wait_for_element(
        self,
        strategy: str,
        value: str,
        timeout_ms: int = 10000,
    ) -> bool:
        """Wait for an element to appear."""
        frame = self._active_frame()
        try:
            locator = self._resolve_locator(frame, strategy, value)
            await locator.first.wait_for(state="visible", timeout=timeout_ms)
            return True
        except Exception:
            return False

    async def wait_for_text(self, text: str, timeout_ms: int = 10000) -> bool:
        """Wait for specific text to appear on the surface."""
        frame = self._active_frame()
        try:
            locator = frame.get_by_text(text, exact=False)
            await locator.first.wait_for(state="visible", timeout=timeout_ms)
            return True
        except Exception:
            return False

    # ----- Session management -----

    async def pause(self) -> dict[str, str]:
        """Pause automation for human takeover.

        Returns connection info. In the full system, this would expose
        the browser session via noVNC or CDP for remote control.
        """
        self._paused = True
        info: dict[str, str] = {}
        if self._novnc_url:
            info["novnc_url"] = self._novnc_url
        # CDP endpoint for remote debugging
        if self._browser:
            ws = self._browser.contexts[0].pages[0].url if self._browser.contexts else ""
            info["current_url"] = ws
        info["status"] = "paused"
        logger.info(f"Surface paused for human takeover. Access: {info}")
        return info

    async def resume(self) -> None:
        """Resume automation after human handoff."""
        self._paused = False
        # Re-observe state since human may have navigated
        logger.info("Surface resumed after human handoff")

    async def is_alive(self) -> bool:
        """Check if the browser session is still active."""
        try:
            if not self._page or self._page.is_closed():
                return False
            await self._page.title()
            return True
        except Exception:
            return False

    # ----- Private helpers -----

    def _resolve_locator(self, frame: Frame | Page, strategy: str, value: str):
        """Resolve a locator strategy + value into a Playwright locator.

        This is the translation layer between our surface-agnostic locator
        model and Playwright's concrete locator API.
        """
        s = strategy.lower().replace(" ", "_")

        if s in (LocatorStrategy.A11Y_ROLE.value, "a11y_role"):
            # Parse "role=name" format, e.g. "textbox" or "button:Search"
            if ":" in value:
                role, name = value.split(":", 1)
                return frame.get_by_role(role.strip(), name=name.strip())  # type: ignore[arg-type]
            return frame.get_by_role(value.strip())  # type: ignore[arg-type]

        if s in (LocatorStrategy.A11Y_LABEL.value, "a11y_label"):
            return frame.get_by_label(value)

        if s in (LocatorStrategy.TEXT_CONTENT.value, "text_content"):
            return frame.get_by_text(value, exact=False)

        if s in (LocatorStrategy.CSS_SELECTOR.value, "css_selector"):
            return frame.locator(value)

        if s in (LocatorStrategy.XPATH.value, "xpath"):
            return frame.locator(f"xpath={value}")

        if s in (LocatorStrategy.COORDINATES.value, "coordinates"):
            # Coordinates format: "x,y" — we click at that position
            # Return a locator that will use coordinates in the action
            x, y = value.split(",")
            return frame.locator(f"xpath=/html/body").first

        # Default: try as CSS selector
        return frame.locator(value)
