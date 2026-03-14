import asyncio
import json
import time

from browser.bridge import browser_bridge
from browser.models import PageSnapshot, ElementRef, ViewportMeta

# Mock a connected browser bridge
browser_bridge.register_connection("test-session")

# Mock a snapshot
snapshot = PageSnapshot(
    session_id="test-session",
    tab_id="tab-1",
    url="http://example.com",
    title="Test Page",
    generation=1,
    elements=[
        ElementRef(
            ref_id="mw_1",
            agent_id=1,
            role="button",
            tag="button",
            visible=True,
            enabled=True,
            action_types=["click"],
            generation=1,
        )
    ],
    viewport=ViewportMeta()
)
browser_bridge.register_snapshot(snapshot)

# Import the function to test
from tools.browser_tools import _queue_browser_action
from browser.models import ActionResult

async def test_queue_action():
    print("Test: queue_browser_action blocks and waits for execution")
    
    # We'll run the queue_browser_action task in the background
    # and simulate the extension responding
    
    async def simulate_extension():
        await asyncio.sleep(1.0)
        # 1. Drain actions (like the extension does)
        actions = browser_bridge.drain_actions("test-session")
        if not actions:
            print("Extension: no actions found")
            return
            
        action = actions[0]
        print(f"Extension: received action {action.action_id}")
        
        # 2. Simulate action execution taking some time
        await asyncio.sleep(1.5)
        
        # 3. Report result
        print(f"Extension: reporting result for {action.action_id}")
        result = ActionResult(
            ok=True,
            message="Action executed.",
            action=action.action,
            ref_id=action.ref_id,
            action_id=action.action_id,
            session_id=action.session_id,
            pre_generation=1,
            post_generation=1,
        )
        browser_bridge.record_action_result(result)
        
        # 4. We won't simulate a full snapshot refresh here to test the fallback behavior
        print("Extension: done simulating.")

    print(f"Starting queue at {time.time()}")
    task1 = asyncio.create_task(_queue_browser_action("click", "mw_1", session_id="test-session"))
    task2 = asyncio.create_task(simulate_extension())
    
    result_json = await task1
    await task2
    print(f"Finished queue at {time.time()}")
    
    result = json.loads(result_json)
    print(f"Result: {json.dumps(result, indent=2)}")
    
    if result["ok"]:
        print("TEST PASSED: action was successful and _queue_browser_action waited for it.")
    else:
        print("TEST FAILED")

if __name__ == "__main__":
    asyncio.run(test_queue_action())
