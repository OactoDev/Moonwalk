"""
Moonwalk — Browser Bridge State
===============================
Tracks extension connection state and queued browser actions.
"""

import os
import secrets
import time
from typing import Dict, List, Optional
from .models import ActionRequest, ActionResult, DomChangeEvent, PageSnapshot
from .store import browser_store
from runtime_state import runtime_state_store


class BrowserBridge:
    def __init__(self):
        self._session_token = os.environ.get("MOONWALK_BROWSER_BRIDGE_TOKEN", "").strip() or "dev-bridge-token"
        self._connected_session_id: Optional[str] = None
        self._last_seen_at: float = 0.0
        self._pending_actions: List[ActionRequest] = []
        self._extension_name: str = ""
        self._latest_result_by_action_id: Dict[str, ActionResult] = {}
        self._latest_dom_change_by_action_id: Dict[str, DomChangeEvent] = {}

    @property
    def session_token(self) -> str:
        return self._session_token

    def is_connected(self) -> bool:
        return bool(self._connected_session_id)

    def connected_session_id(self) -> Optional[str]:
        return self._connected_session_id

    def last_seen_at(self) -> float:
        return self._last_seen_at

    def extension_name(self) -> str:
        return self._extension_name

    def authenticate(self, token: str) -> bool:
        return bool(token) and token == self._session_token

    def register_connection(self, session_id: str, extension_name: str = "") -> None:
        self._connected_session_id = session_id
        self._extension_name = extension_name
        self._last_seen_at = time.time()
        runtime_state_store.mark_browser_connected(session_id=session_id)

    def touch(self) -> None:
        self._last_seen_at = time.time()

    def register_snapshot(self, snapshot: PageSnapshot) -> None:
        browser_store.upsert_snapshot(snapshot)
        self._connected_session_id = snapshot.session_id
        self._last_seen_at = time.time()
        runtime_state_store.register_browser_snapshot(snapshot)

    def queue_action(self, request: ActionRequest) -> ActionResult:
        if not self.is_connected():
            return ActionResult(
                ok=False,
                message="Browser extension bridge is not connected.",
                action=request.action,
                ref_id=request.ref_id,
            )

        snapshot = browser_store.get_snapshot(request.session_id or self._connected_session_id)
        if not snapshot:
            # Allow snapshot-independent actions (refresh_snapshot, evaluate_js)
            # to be queued even before the first snapshot arrives.
            _snapshotless_actions = {"refresh_snapshot", "evaluate_js", "extract_data", "extract_readability"}
            if request.action in _snapshotless_actions and (request.session_id or self._connected_session_id):
                request.session_id = request.session_id or self._connected_session_id or ""
                request.action_id = request.action_id or f"act_{int(time.time() * 1000)}_{secrets.token_hex(4)}"
                self._pending_actions.append(request)
                return ActionResult(
                    ok=True,
                    message=f"{request.action} queued (no snapshot yet).",
                    action=request.action,
                    ref_id=request.ref_id,
                    action_id=request.action_id,
                    session_id=request.session_id,
                    pre_generation=0,
                    post_generation=0,
                )
            return ActionResult(
                ok=False,
                message="No active browser snapshot available.",
                action=request.action,
                ref_id=request.ref_id,
            )

        request.session_id = request.session_id or snapshot.session_id
        request.action_id = request.action_id or f"act_{int(time.time() * 1000)}_{secrets.token_hex(4)}"
        if request.action in {"evaluate_js", "extract_data", "extract_readability"}:
            request.metadata = {
                **request.metadata,
                "tab_id": request.metadata.get("tab_id", snapshot.tab_id),
                "generation": request.metadata.get("generation", str(snapshot.generation)),
            }
        if request.action == "refresh_snapshot":
            request.metadata = {
                **request.metadata,
                "tab_id": snapshot.tab_id,
                "generation": str(snapshot.generation),
            }
        
        if request.ref_id:
            element = browser_store.get_element(request.ref_id, request.session_id)
            if element:
                request.metadata = {
                    **request.metadata,
                    "tab_id": snapshot.tab_id,
                    "generation": str(snapshot.generation),
                    "agent_id": str(element.agent_id) if element.agent_id else "",
                    "dom_path": element.dom_path,
                    "role": element.role,
                    "tag": element.tag,
                    "label": element.primary_label(),
                    "text": element.text,
                    "aria_label": element.aria_label,
                    "name": element.name,
                    "placeholder": element.placeholder,
                    "href": element.href,
                }
        self._pending_actions.append(request)
        return ActionResult(
            ok=True,
            message="Action queued for browser extension execution.",
            action=request.action,
            ref_id=request.ref_id,
            action_id=request.action_id,
            session_id=request.session_id,
            pre_generation=snapshot.generation,
            post_generation=snapshot.generation,
        )

    def drain_actions(self, session_id: Optional[str] = None) -> List[ActionRequest]:
        sid = session_id or self._connected_session_id
        if not sid:
            return []
        drained = [a for a in self._pending_actions if a.session_id == sid]
        self._pending_actions = [a for a in self._pending_actions if a.session_id != sid]
        return drained

    def pending_action_count(self, session_id: Optional[str] = None) -> int:
        sid = session_id or self._connected_session_id
        if not sid:
            return 0
        return sum(1 for action in self._pending_actions if action.session_id == sid)

    def record_action_result(self, result: ActionResult) -> None:
        if result.action_id:
            self._latest_result_by_action_id[result.action_id] = result
        self._last_seen_at = time.time()
        runtime_state_store.record_browser_action_result(result)
        details = result.details or {}
        if result.action == "extract_readability" and isinstance(details, dict):
            raw_result = details.get("result", "")
            if raw_result:
                try:
                    import json
                    payload = json.loads(raw_result)
                except Exception:
                    payload = {"ok": False, "message": "invalid_readability_payload"}
                if isinstance(payload, dict):
                    runtime_state_store.record_readability_extraction(payload)

    def latest_action_result(self, action_id: str) -> Optional[ActionResult]:
        return self._latest_result_by_action_id.get(action_id)

    def record_dom_change(self, event: DomChangeEvent) -> None:
        if event.action_id:
            self._latest_dom_change_by_action_id[event.action_id] = event
        self._last_seen_at = time.time()

    def latest_dom_change(self, action_id: str) -> Optional[DomChangeEvent]:
        return self._latest_dom_change_by_action_id.get(action_id)

    def disconnect(self) -> None:
        self._connected_session_id = None
        self._extension_name = ""
        runtime_state_store.mark_browser_disconnected()

    def reset(self) -> None:
        self._connected_session_id = None
        self._last_seen_at = 0.0
        self._pending_actions.clear()
        self._latest_result_by_action_id.clear()
        self._latest_dom_change_by_action_id.clear()
        self._extension_name = ""
        runtime_state_store.mark_browser_disconnected()


browser_bridge = BrowserBridge()
