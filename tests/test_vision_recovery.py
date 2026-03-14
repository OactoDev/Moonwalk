import asyncio
import os
import sys


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "backend"))

from agent.vision_recovery import attempt_vision_recovery, should_attempt_vision_recovery


def test_should_attempt_vision_recovery_for_low_confidence_browser_failure():
    assert should_attempt_vision_recovery(
        tool_name="browser_click_match",
        tool_args={"query": "Compose"},
        tool_result='{"ok": false, "error_code": "element_not_found"}',
        verification_message="browser_click_match failed: Could not find element",
        verification_confidence=0.95,
        should_retry=True,
    ) is True


def test_should_not_attempt_vision_recovery_for_non_ui_tool():
    assert should_attempt_vision_recovery(
        tool_name="get_web_information",
        tool_args={"query": "housing"},
        tool_result='{"ok": false}',
        verification_message="failed",
        verification_confidence=0.4,
        should_retry=True,
    ) is False


def test_attempt_vision_recovery_clicks_and_types():
    calls = []

    async def fake_executor(name: str, args: dict):
        calls.append((name, dict(args)))
        if name == "read_screen":
            return "The search box is visible near coordinates (320, 180)."
        if name == "click_element":
            return "Clicked at (320, 180)"
        if name == "type_text":
            return "Typed 5 characters into the active field."
        raise AssertionError(f"Unexpected tool: {name}")

    result = asyncio.run(
        attempt_vision_recovery(
            tool_name="type_in_field",
            tool_args={"field_description": "Search", "text": "hello"},
            tool_executor=fake_executor,
        )
    )

    assert result.success is True
    assert result.tool_name == "type_text"
    assert [name for name, _ in calls] == ["read_screen", "click_element", "type_text"]
