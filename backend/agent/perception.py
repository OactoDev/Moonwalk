"""
Moonwalk — 3-Layer Perception Engine
=====================================
L1: AppleScript  → active app, window title, browser URL (always, ~50ms)
L2: Browser DOM  → page content, selected text (when browser is active)
L3: Gemini Vision → screenshot for visual understanding (on demand)
"""

import asyncio
import subprocess
import os
import time
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from runtime_state import runtime_state_store


@dataclass
class ContextSnapshot:
    """Combined context from all perception layers."""
    # L1 — AppleScript (always populated)
    active_app: str = ""
    window_title: str = ""
    browser_url: Optional[str] = None

    # L2 — Browser DOM (populated when browser is active)
    page_title: Optional[str] = None
    selected_text: Optional[str] = None
    visible_text: Optional[str] = None

    # L3 — Vision (populated on demand)
    screenshot_path: Optional[str] = None

    # Metadata
    clipboard: Optional[str] = None
    timestamp: float = 0.0

    def to_prompt_string(self) -> str:
        """Format context as a structured block for the LLM system prompt."""
        now = datetime.now()
        lines = [
            "[Desktop Context]",
            f"  Date: {now.strftime('%A, %B %d, %Y')}",
            f"  Time: {now.strftime('%I:%M %p')}",
            f"  Active App: {self.active_app or 'Unknown'}",
            f"  Window Title: {self.window_title or 'Unknown'}",
        ]
        if self.browser_url:
            lines.append(f"  Browser URL: {self.browser_url}")
        if self.page_title and self.page_title != self.window_title:
            lines.append(f"  Page Title: {self.page_title}")
        if self.selected_text:
            lines.append(f"  Selected Text: {self.selected_text[:500]}")
        if self.visible_text:
            # Trim to a useful window and label clearly
            trimmed = self.visible_text[:1000].strip()
            if trimmed:
                lines.append(f"  Visible Page Text (first 1000 chars): {trimmed}")
        if self.clipboard:
            lines.append(f"  Clipboard: {self.clipboard[:300]}")
        if self.screenshot_path:
            lines.append(f"  Screenshot: attached (use read_screen for full analysis)")
        lines.append("[End Context]")
        return "\n".join(lines)


# ── Known browsers for L2 activation ──
BROWSERS = {"google chrome", "safari", "arc", "firefox", "brave browser", "microsoft edge", "chromium"}


# ═══════════════════════════════════════════════════════════════
#  Layer 1 — AppleScript (fast metadata, always runs)
# ═══════════════════════════════════════════════════════════════

async def _run_osascript(script: str) -> str:
    """Run an AppleScript snippet asynchronously and return stdout."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
        return stdout.decode("utf-8", errors="replace").strip()
    except (asyncio.TimeoutError, Exception):
        return ""


async def get_active_app() -> str:
    """Get the name of the frontmost application."""
    return await _run_osascript(
        'tell application "System Events" to get name of first process whose frontmost is true'
    )


async def get_window_title() -> str:
    """Get the window title of the frontmost application."""
    return await _run_osascript(
        'tell application "System Events" to get title of front window of (first process whose frontmost is true)'
    )


async def get_browser_url(app_name: str) -> Optional[str]:
    """Get the current URL from a known browser."""
    name_lower = app_name.lower()
    if "chrome" in name_lower or "chromium" in name_lower or "brave" in name_lower:
        return await _run_osascript(
            f'tell application "{app_name}" to get URL of active tab of front window'
        )
    elif "safari" in name_lower:
        return await _run_osascript(
            'tell application "Safari" to get URL of front document'
        )
    elif "arc" in name_lower:
        return await _run_osascript(
            'tell application "Arc" to get URL of active tab of front window'
        )
    return None


async def get_clipboard() -> Optional[str]:
    """Get current clipboard contents."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "pbpaste",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
        text = stdout.decode("utf-8", errors="replace").strip()
        return text if text else None
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════
#  Layer 2 — Browser DOM (when a browser is the active app)
# ═══════════════════════════════════════════════════════════════

async def get_browser_selected_text(app_name: str) -> Optional[str]:
    """Get text currently selected in the browser via AppleScript + JS."""
    name_lower = app_name.lower()
    if "chrome" in name_lower or "chromium" in name_lower or "brave" in name_lower:
        return await _run_osascript(
            f'tell application "{app_name}" to execute active tab of front window javascript "window.getSelection().toString()"'
        )
    elif "safari" in name_lower:
        return await _run_osascript(
            'tell application "Safari" to do JavaScript "window.getSelection().toString()" in front document'
        )
    return None


async def get_browser_page_content(app_name: str) -> Optional[str]:
    """Get visible text content from the browser page."""
    name_lower = app_name.lower()
    js = "document.body.innerText.substring(0, 2000)"
    if "chrome" in name_lower or "chromium" in name_lower or "brave" in name_lower:
        return await _run_osascript(
            f'tell application "{app_name}" to execute active tab of front window javascript "{js}"'
        )
    elif "safari" in name_lower:
        return await _run_osascript(
            f'tell application "Safari" to do JavaScript "{js}" in front document'
        )
    return None


# ═══════════════════════════════════════════════════════════════
#  Layer 3 — Vision (screenshot for Gemini multimodal)
# ═══════════════════════════════════════════════════════════════

SCREENSHOT_DIR = os.path.join(tempfile.gettempdir(), "moonwalk")

async def capture_screenshot() -> Optional[str]:
    """Capture the current screen and return the image path."""
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    filepath = os.path.join(SCREENSHOT_DIR, f"screen_{int(time.time())}.png")
    try:
        proc = await asyncio.create_subprocess_exec(
            "screencapture", "-x", "-t", "png", filepath,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=5.0)
        if os.path.exists(filepath):
            return filepath
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════════
#  Orchestrator — decides which layers to activate
# ═══════════════════════════════════════════════════════════════

# Keywords that suggest the user wants visual context
VISION_KEYWORDS = {
    "see", "screen", "look", "show", "what's on", "what is on",
    "help me with this", "this page", "what am i", "read this",
    "screenshot", "image", "picture", "visual"
}


def _needs_vision(request_text: str) -> bool:
    """Heuristic: does the request imply we need to 'see' the screen?"""
    lower = request_text.lower()
    return any(kw in lower for kw in VISION_KEYWORDS)


async def snapshot(request_text: str = "", include_vision: bool = False) -> ContextSnapshot:
    """
    Build a ContextSnapshot by running perception layers in parallel.
    
    - L1 (AppleScript) always runs
    - L2 (Browser DOM) runs if the active app is a browser
    - L3 (Vision) runs if `include_vision=True` or the request implies visual context
    """
    ctx = ContextSnapshot(timestamp=time.time())

    # ── L1: Always run these in parallel ──
    app_name, window_title, clipboard = await asyncio.gather(
        get_active_app(),
        get_window_title(),
        get_clipboard(),
    )
    ctx.active_app = app_name
    ctx.window_title = window_title
    ctx.clipboard = clipboard
    runtime_state_store.update_os_state(
        active_app=app_name,
        window_title=window_title,
        clipboard=clipboard or "",
    )

    # ── L2: If browser is active, grab DOM context ──
    if app_name.lower() in BROWSERS:
        bridge_state = runtime_state_store.snapshot().browser_state
        url, selected, page_content = await asyncio.gather(
            get_browser_url(app_name),
            get_browser_selected_text(app_name),
            get_browser_page_content(app_name),
        )
        if bridge_state.connected and bridge_state.url:
            ctx.browser_url = bridge_state.url
            ctx.page_title = bridge_state.title or window_title
            runtime_state_store.update_os_state(
                browser_url=bridge_state.url,
                provenance="browser_bridge",
                degraded=False,
            )
        else:
            ctx.browser_url = url
            runtime_state_store.update_os_state(
                browser_url=url or "",
                provenance="applescript_fallback" if url else "",
                degraded=bool(url),
            )
        ctx.selected_text = selected if selected else None
        ctx.visible_text = page_content if page_content else None
        if not ctx.page_title:
            ctx.page_title = window_title  # browser window title = page title

    # ── L3: Vision if needed ──
    if include_vision or _needs_vision(request_text):
        ctx.screenshot_path = await capture_screenshot()

    return ctx

async def get_minimal_context() -> str:
    """Fast (~50ms) context fetch used strictly for inter-tool state awareness."""
    try:
        app_name, window_title = await asyncio.gather(
            get_active_app(),
            get_window_title()
        )
        return f"Current Desktop State -> Active App: '{app_name}', Window Title: '{window_title}'"
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════
#  WorldState Builder (V2 integration)
# ═══════════════════════════════════════════════════════════════

async def build_world_state(
    user_text: str = "",
    include_vision: bool = False
):
    """
    Build a WorldState object for V2 agent.
    
    This is the V2 equivalent of snapshot() but returns a structured
    WorldState instead of ContextSnapshot.
    
    Args:
        user_text: User's request text (for intent parsing)
        include_vision: Force screenshot capture
        
    Returns:
        WorldState object with structured context
    """
    from agent.world_state import WorldState, IntentParser, EntityExtractor
    
    # Get context snapshot first
    ctx = await snapshot(user_text, include_vision)
    
    # Parse intent and extract entities
    intent_parser = IntentParser()
    entity_extractor = EntityExtractor()
    
    intent = intent_parser.parse(user_text) if user_text else None
    entities = entity_extractor.extract(user_text) if user_text else {}
    
    # Build world state
    return WorldState(
        # Desktop state
        active_app=ctx.active_app,
        window_title=ctx.window_title,
        browser_url=ctx.browser_url,
        
        # Extracted entities
        mentioned_apps=entities.get("apps", []),
        mentioned_files=entities.get("files", []),
        mentioned_urls=entities.get("urls", []),
        
        # Clipboard
        clipboard_content=ctx.clipboard,
        
        # Screenshot
        has_screenshot=ctx.screenshot_path is not None,
        screenshot_path=ctx.screenshot_path,
        
        # Intent
        intent=intent,
        
        # Metadata
        timestamp=ctx.timestamp
    )
