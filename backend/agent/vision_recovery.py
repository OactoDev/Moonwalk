"""Conservative vision-guided recovery for UI actions."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional


ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[str]]

_RECOVERABLE_TOOLS = {
    "browser_click_ref",
    "browser_click_match",
    "browser_type_ref",
    "find_and_act",
    "click_ui",
    "type_in_field",
    "click_element",
}
_FAILURE_MARKERS = (
    "element_not_found",
    "no ui element matching",
    "no text field matching",
    "could not find element",
    "stale_ref",
    "low_selection_confidence",
    "ambiguous",
    "failed to type",
    "failed to paste",
)
_COORD_RE = re.compile(r"\(?\b(?:x\s*[:=]\s*)?(\d{1,4})\s*,\s*(?:y\s*[:=]\s*)?(\d{1,4})\b\)?", re.I)


@dataclass
class VisionRecoveryAttempt:
    attempted: bool
    success: bool
    tool_name: str = ""
    tool_args: dict[str, Any] | None = None
    result: str = ""
    reason: str = ""


def _type_payload_for_tool(tool_name: str, tool_args: dict[str, Any]) -> str:
    if tool_name == "type_in_field":
        return str(tool_args.get("text", "") or "")
    if tool_name == "browser_type_ref":
        return str(tool_args.get("text", "") or "")
    if tool_name == "find_and_act" and str(tool_args.get("action", "")).lower() == "type":
        return str(tool_args.get("value", "") or "")
    return ""


def should_attempt_vision_recovery(
    *,
    tool_name: str,
    tool_args: dict[str, Any],
    tool_result: str,
    verification_message: str,
    verification_confidence: float,
    should_retry: bool,
) -> bool:
    if tool_name not in _RECOVERABLE_TOOLS:
        return False
    if not should_retry and verification_confidence >= 0.9:
        return False
    haystack = " ".join(
        part for part in (str(tool_result or "").lower(), str(verification_message or "").lower()) if part
    )
    if any(marker in haystack for marker in _FAILURE_MARKERS):
        return True
    if verification_confidence < 0.75:
        return True
    if tool_name == "find_and_act" and str(tool_args.get("action", "")).lower() in {"click", "type"}:
        return True
    return False


def _extract_coordinates(screen_result: str) -> Optional[tuple[int, int]]:
    if not screen_result:
        return None
    match = _COORD_RE.search(str(screen_result))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _vision_prompt_for(tool_name: str, tool_args: dict[str, Any]) -> str:
    if tool_name in {"browser_click_match", "browser_click_ref", "click_ui"}:
        target = str(tool_args.get("query", "") or tool_args.get("description", "") or tool_args.get("target", "") or "the intended target")
        return (
            f"Find '{target}' on screen. Return the best click target with one precise coordinate pair like (x, y) "
            f"near the center of the element."
        )
    if tool_name in {"browser_type_ref", "type_in_field"} or (
        tool_name == "find_and_act" and str(tool_args.get("action", "")).lower() == "type"
    ):
        target = str(tool_args.get("field_description", "") or tool_args.get("target", "") or "the intended text field")
        return (
            f"Find the input field for '{target}' on screen. Return one precise coordinate pair like (x, y) "
            f"for where to click to focus it."
        )
    return "Find the intended UI target on screen and return one precise coordinate pair like (x, y)."


async def attempt_vision_recovery(
    *,
    tool_name: str,
    tool_args: dict[str, Any],
    tool_executor: ToolExecutor,
) -> VisionRecoveryAttempt:
    prompt = _vision_prompt_for(tool_name, tool_args)
    screen_result = await tool_executor("read_screen", {"question": prompt})
    coords = _extract_coordinates(screen_result)
    if not coords:
        return VisionRecoveryAttempt(
            attempted=True,
            success=False,
            result=screen_result,
            reason="Vision could not localize a coordinate for recovery.",
        )

    x, y = coords
    click_result = await tool_executor("click_element", {"x": x, "y": y})
    text = _type_payload_for_tool(tool_name, tool_args)
    if text:
        type_result = await tool_executor("type_text", {"text": text})
        return VisionRecoveryAttempt(
            attempted=True,
            success=True,
            tool_name="type_text",
            tool_args={"text": text},
            result=json.dumps(
                {
                    "ok": True,
                    "recovery_strategy": "vision_click_then_type",
                    "coordinates": {"x": x, "y": y},
                    "click_result": click_result,
                    "type_result": type_result,
                },
                ensure_ascii=False,
            ),
            reason="Vision localized the field and typing was retried through the focused UI.",
        )

    return VisionRecoveryAttempt(
        attempted=True,
        success=True,
        tool_name="click_element",
        tool_args={"x": x, "y": y},
        result=json.dumps(
            {
                "ok": True,
                "recovery_strategy": "vision_click",
                "coordinates": {"x": x, "y": y},
                "click_result": click_result,
            },
            ensure_ascii=False,
        ),
        reason="Vision localized the target and the click was retried by coordinate.",
    )
