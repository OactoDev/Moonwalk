import asyncio
import json
import time

from browser.bridge import browser_bridge
from browser.models import PageSnapshot, ElementRef, ViewportMeta

browser_bridge.register_connection("test-session")
snapshot = PageSnapshot(
    session_id="test-session", tab_id="tab-1", url="http://example.com", title="Test Page", generation=1,
    elements=[
        ElementRef(ref_id="mw_1", agent_id=1, role="button", tag="button", visible=True, enabled=True, action_types=["click"], generation=1)
    ],
    viewport=ViewportMeta()
)
browser_bridge.register_snapshot(snapshot)

# Import just what we need directly to avoid tools/__init__.py circular import loop
import sys
import importlib.util

spec = importlib.util.spec_from_file_location("browser_tools", "tools/browser_tools.py")
browser_tools = importlib.util.module_from_spec(spec)
# mock registry
class MockRegistry:
    def register(self, *args, **kwargs): return lambda f: f
sys.modules['tools.registry'] = type('Mock', (), {'registry': MockRegistry()})()

spec.loader.exec_module(browser_tools)
_queue_browser_action = browser_tools._queue_browser_action
from browser.models import ActionResult

async def test_queue_action():
    print("Test: queue_browser_action blocks")
    async def simulate_extension():
        await asyncio.sleep(1.0)
        actions = browser_bridge.drain_actions("test-session")
        action = actions[0]
        print(f"Extension rx: {action.action_id}")
        await asyncio.sleep(1.5)
        print(f"Extension tx: {action.action_id}")
        browser_bridge.record_action_result(ActionResult(
            ok=True, message="Executed.", action=action.action, ref_id=action.ref_id, action_id=action.action_id,
            session_id=action.session_id, pre_generation=1, post_generation=1,
        ))
    t1 = time.time()
    task1 = asyncio.create_task(_queue_browser_action("click", "mw_1", session_id="test-session"))
    task2 = asyncio.create_task(simulate_extension())
    res = json.loads(await task1)
    await task2
    dur = time.time() - t1
    print(f"Done in {dur:.2f}s, ok={res['ok']}, message='{res['message']}'")
    assert dur > 2.0, "Didn't block"
asyncio.run(test_queue_action())
