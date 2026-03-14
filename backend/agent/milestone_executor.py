"""
Moonwalk — Milestone Executor
==============================
LLM micro-loop engine for milestone-based execution.

Unlike the step-based executor that follows a fixed plan, the Milestone
Executor gives the LLM full autonomy *within* each milestone's scope.
For each milestone it:

  1. Perceives the current environment
  2. Shows the LLM the milestone goal + success signal + results so far
  3. The LLM decides which tool to call next (or declares done)
  4. Executes the tool and feeds the result back to the LLM
  5. Repeats until the milestone is complete or the runtime safety cap is reached

This replaces the StepReasoner for complex, multi-milestone tasks —
the plan is defined by *outcomes* (milestones), not by specific
tool sequences.
"""

from __future__ import annotations

import json
import time as _time
from dataclasses import dataclass, field
from typing import Any, Optional, Callable, Awaitable
from functools import partial

from agent.planner import Milestone, MilestonePlan, MilestoneStatus
from providers import LLMProvider
from tools.selector import expand_milestone_hint_tools, resolve_milestone_allowed_tools

print = partial(print, flush=True)

# Raw browser tools hidden from the milestone executor's LLM prompt.
# They remain callable internally (the ACI compound tools use them),
# but the milestone LLM should use the higher-level ACI tools instead.
_RAW_BROWSER_TOOLS: frozenset[str] = frozenset({
    "browser_snapshot",
    "browser_find",
    "browser_click_match",
    "browser_click_ref",
    "browser_type_ref",
    "browser_select_ref",
    "browser_refresh_refs",
    "browser_scroll",
    "browser_wait_for",
    "browser_read_page",
    "browser_read_text",
    "browser_list_tabs",
    "browser_switch_tab",
})

_HARD_SAFETY_CAP = 50

# UI-mutating tools that trigger auto-screen-recovery on failure
_UI_MUTATING_TOOLS: frozenset[str] = frozenset({
    "click_ui", "type_in_field", "type_text", "press_key",
    "run_shortcut", "click_element", "hover_element", "mouse_action",
    "browser_click_ref", "browser_type_ref", "browser_select_ref",
    "browser_click_match", "find_and_act",
    "open_app", "open_url", "quit_app", "close_window",
})

_TRIVIAL_PROGRESS_TOOLS: frozenset[str] = frozenset({
    "open_url",
    "browser_scroll",
    "web_search",
})

_LOW_SIGNAL_ACTION_TOOLS: frozenset[str] = frozenset({
    "press_key",
    "run_shortcut",
    "mouse_action",
    "browser_scroll",
    "web_search",
})

_RESPONSE_ONLY_MILESTONE_MARKERS: tuple[str, ...] = (
    "send a response",
    "send response",
    "send the response",
    "send a modal",
    "show a modal",
    "show the modal",
    "product modal",
    "deliver to the user",
    "sent to the user",
    "sent to user",
    "via send_response",
    "response is sent",
    "modal is sent",
)

_AWAIT_REPLY_SENTINEL = "AWAIT_REPLY:"

_FAILED_UI_RESULT_MARKERS: frozenset[str] = frozenset({
    "no ui element matching",
    "no text field matching",
    "no close match for",
    "visible elements:",
    "available elements:",
    "not expose it via accessibility",
    "try read_screen",
    "error in click_ui",
    "error in type_in_field",
    "failed to type text",
    "failed to paste text",
})


# ═══════════════════════════════════════════════════════════════
#  Data Types
# ═══════════════════════════════════════════════════════════════

@dataclass
class MilestoneAction:
    """Record of a single action taken within a milestone."""
    tool: str
    args: dict
    result: str = ""
    success: bool = False
    error: str = ""
    duration: float = 0.0


# ═══════════════════════════════════════════════════════════════
#  Prompts
# ═══════════════════════════════════════════════════════════════

MILESTONE_EXECUTOR_SYSTEM = """\
You are the execution engine for Moonwalk, a macOS desktop AI assistant.
You are working on ONE milestone at a time. Your job: decide which tool to call
NEXT to achieve the milestone's goal, based on what you observe in the
environment and the results of previous actions.

CRITICAL RULES:
1. Focus ONLY on the current milestone's goal — don't try to complete future milestones.
2. Use the success_signal to know when you're done — once the signal is met, declare "done".
3. Adapt your approach based on tool results — if something fails, try a different way.
4. Be specific with tool arguments — use actual data from the environment/results.
5. For browser interactions: always perceive before acting. Read content before interacting.
5a. For web research, prefer `get_web_information(...)` over raw mechanism choices. It can search, choose a promising result, follow it in the browser, and then return page content/summary/structured items in one call when you provide a `query` with `target_type=page_content|page_summary|structured_data`. Use `target_type=search_results` only when you explicitly need the result list itself.
5b. After you get usable search results, FOLLOW THEM. Do not issue another similar search if you already have relevant result URLs. Open or read one of the authoritative sources next.
6. For writing tools (gdocs_create, gdocs_append): prefer setting "title" only when the
   document content can be synthesized from prior research or from the task itself.
   Only provide "body" or "text" directly if you already have concrete content that must
   be preserved verbatim.
7. NEVER fabricate data. If you need information, search/read for it first.
8. Output ONLY valid JSON — no markdown, no explanation outside the JSON.
9. Each action should make meaningful progress. Avoid repeating failed actions identically.
10. Use deliverables from previous milestones when available — they contain real data.
11. Prefer `web_scrape` over `run_python` for web extraction. Use `run_python` only as a last resort.
12. Active skill overlays are ADVISORY. Use them to guide strategy, but adapt to real observations and evidence.
13. In desktop chat apps, prefer `type_in_field` to focus Search or Message inputs before relying on repeated `press_key` navigation.
14. If a tool says it could not find the requested UI element or field, treat that as a failure and change strategy — do not count it as progress.
15. For "send it again" or repeat-message requests, reuse `last_typed_text` from the environment with `type_text`/`type_in_field`. Do not assume the clipboard contains the right message unless the clipboard content clearly matches."""

MILESTONE_EXECUTOR_PROMPT = """\
## Overall Task
{task_summary}

## Current Milestone ({milestone_id}/{total_milestones})
Goal: {milestone_goal}
Success Signal: {success_signal}
Suggested Tools: {hint_tools}

## Deliverables from Previous Milestones
{deliverables}

## Active Skill Overlays
{skill_context}

## Recent Structured Observations
{observations}

## Search Leads
{search_leads}

## Actions Taken in This Milestone (count: {action_count})
{action_history}

## Stall Guidance
{stall_warning}

## Current Environment
{env_state}

## Last Action Result
{last_result}

## Available Tools
{tool_list}

## Decision Required
What tool should I call NEXT to achieve the milestone goal?
Or is the milestone already complete based on the results so far?

Return JSON:
{{
  "done": false,
  "tool": "tool_name",
  "args": {{}},
  "reasoning": "Why this action moves us toward the success signal",
  "description": "Human-readable description for the UI",
  "deliverable": null
}}

If the milestone is COMPLETE (success signal is met):
{{
  "done": true,
  "tool": "",
  "args": {{}},
  "reasoning": "How the success signal was met",
  "description": "Milestone complete",
  "deliverable": "The key result/data to pass to dependent milestones"
}}"""


# ═══════════════════════════════════════════════════════════════
#  Milestone Executor
# ═══════════════════════════════════════════════════════════════

class MilestoneExecutor:
    """LLM micro-loop engine for milestone-based execution."""

    def __init__(
        self,
        provider: LLMProvider,
        tool_declarations: list[dict],
    ):
        self.provider = provider
        self._tool_list_cache: str = ""
        self._tool_declarations = tool_declarations
        self._build_tool_list()

    def _build_tool_list(self) -> None:
        """Build compact tool list for prompts (hides raw browser tools)."""
        self._tool_list_cache = self._format_tool_list()

    def _format_tool_list(self, allowed_tool_names: Optional[set[str]] = None) -> str:
        """Build compact tool list for prompts (hides raw browser tools)."""
        lines: list[str] = []
        for decl in self._tool_declarations:
            name = decl["name"]
            if allowed_tool_names is not None and name not in allowed_tool_names:
                continue
            if name in _RAW_BROWSER_TOOLS:
                continue  # Milestone LLM uses ACI tools instead
            desc = decl.get("description", "")
            params = decl.get("parameters", {})
            props = params.get("properties", {}) if isinstance(params, dict) else {}
            required = set(params.get("required", [])) if isinstance(params, dict) else set()
            param_parts: list[str] = []
            for pname, pschema in props.items():
                if pname == "reasoning":
                    continue  # Don't show injected reasoning param
                marker = "*" if pname in required else ""
                if isinstance(pschema, dict) and isinstance(pschema.get("enum"), list) and pschema["enum"]:
                    ptype = "{" + "|".join(str(v) for v in pschema["enum"][:8]) + "}"
                else:
                    ptype = pschema.get("type", "string") if isinstance(pschema, dict) else "string"
                param_parts.append(f"{pname}{marker}:{ptype}")
            param_str = ", ".join(param_parts) if param_parts else "(no args)"
            short_desc = desc[:100] + "…" if len(desc) > 100 else desc
            lines.append(f"  {name}({param_str}) — {short_desc}")
        return "\n".join(lines)

    def _expanded_hint_tools(self, milestone: Milestone) -> set[str]:
        return expand_milestone_hint_tools(milestone.hint_tools or [])

    def _resolve_allowed_tools(
        self,
        milestone: Milestone,
        request_scope_tool_names: Optional[set[str]],
    ) -> tuple[Optional[set[str]], Optional[set[str]]]:
        return resolve_milestone_allowed_tools(milestone.hint_tools or [], request_scope_tool_names)

    def _is_action_relevant_to_milestone(self, action: MilestoneAction, milestone: Milestone) -> bool:
        allowed = self._expanded_hint_tools(milestone)
        if allowed:
            return action.tool in allowed
        return True

    def _summarize_tool_result(self, result_text: str) -> str:
        raw = (result_text or "").strip()
        if not raw:
            return "  (none)"

        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            compact = " ".join(raw.split())
            return f"  {compact[:400]}"

        if not isinstance(data, dict):
            compact = " ".join(str(raw).split())
            return f"  {compact[:400]}"

        lines: list[str] = []
        title = str(data.get("title", "")).strip()
        url = str(data.get("url", "")).strip()
        page_type = str(data.get("page_type", "")).strip()
        if title:
            lines.append(f"  Title: {title[:160]}")
        if url:
            lines.append(f"  URL: {url[:220]}")
        if page_type:
            lines.append(f"  Page type: {page_type}")

        items = data.get("items")
        if isinstance(items, list) and items:
            for idx, item in enumerate(items[:5], 1):
                if not isinstance(item, dict):
                    continue
                label = str(item.get("label", "")).strip()
                context = str(item.get("context", "")).strip()
                href = str(item.get("href", "")).strip()
                rank = item.get("rank", idx)
                row = " | ".join(part for part in (label, context, href) if part)
                if row:
                    lines.append(f"  [{rank}] {row[:240]}")
            if lines:
                return "\n".join(lines[:8])

        content = str(data.get("content", "") or data.get("text", "") or data.get("summary", "")).strip()
        if content:
            compact_lines = [line.strip() for line in content.splitlines() if line.strip()]
            for line in compact_lines[:5]:
                lines.append(f"  {line[:220]}")
            return "\n".join(lines[:8])

        if data.get("message"):
            lines.append(f"  Message: {str(data.get('message'))[:220]}")
        if not lines:
            lines.append(f"  {raw[:300]}")
        return "\n".join(lines[:8])

    def _build_recent_observations(self, actions: list[MilestoneAction], last_result: str) -> str:
        recent_successes = [a for a in reversed(actions) if a.success][:3]
        sections: list[str] = []
        for action in reversed(recent_successes):
            summary = self._summarize_tool_result(action.result)
            sections.append(f"- {action.tool}\n{summary}")
        if not sections and last_result and last_result != "(no actions taken yet)":
            sections.append(f"- last_result\n{self._summarize_tool_result(last_result)}")
        return "\n".join(sections) if sections else "  (none yet)"

    def _build_search_leads(self, actions: list[MilestoneAction]) -> str:
        lines: list[str] = []
        seen_urls: set[str] = set()
        for action in reversed(actions):
            if not action.success:
                continue
            try:
                data = json.loads(action.result)
            except (TypeError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            target_type = str(data.get("target_type", "")).strip().lower()
            if target_type != "search_results":
                continue
            for item in data.get("items", [])[:5]:
                if not isinstance(item, dict):
                    continue
                href = str(item.get("href", "")).strip()
                if not href or href in seen_urls:
                    continue
                seen_urls.add(href)
                label = str(item.get("label", "")).strip()
                context = str(item.get("context", "")).strip()
                row = " | ".join(part for part in (label, context, href) if part)
                if row:
                    lines.append(f"  - {row[:240]}")
                if len(lines) >= 6:
                    return "\n".join(lines)
        return "\n".join(lines) if lines else "  (none yet)"

    async def execute_milestone(
        self,
        milestone: Milestone,
        plan: MilestonePlan,
        env_perceiver: Callable[[], Awaitable[dict]],
        tool_executor: Callable[[str, dict], Awaitable[tuple[str, bool]]],
        deliverables: dict[int, str],
        skill_context: str = "",
        request_scope_tool_names: Optional[set[str]] = None,
        blocked_await_signatures: Optional[set[str]] = None,
        ws_callback=None,
    ) -> tuple[bool, str]:
        """Execute a single milestone via LLM micro-loop.

        Args:
            milestone: The milestone to execute
            plan: The full milestone plan (for context)
            env_perceiver: Async function returning environment state dict
            tool_executor: Async function (tool, args) -> (result_str, success_bool)
            deliverables: Results from completed milestones {id: deliverable_str}
            ws_callback: WebSocket callback for streaming updates

        Returns:
            Tuple of (success, result_summary)
        """
        actions: list[MilestoneAction] = []
        last_result = "(no actions taken yet)"

        print(f"\n[MilestoneExec] ═══ Milestone {milestone.id}: {milestone.goal} ═══")
        print(f"[MilestoneExec]   Success signal: {milestone.success_signal}")
        print(f"[MilestoneExec]   Hint tools: {milestone.hint_tools}")
        # ...existing code...
        priority_tool_names, fallback_tool_names = self._resolve_allowed_tools(milestone, request_scope_tool_names)
        blocked_awaits = set(blocked_await_signatures or set())
        tool_list_text = self._format_tool_list(priority_tool_names)

        action_num = 0
        decision_failures = 0
        consecutive_rejections = 0
        stall_warning = "  (none)"
        blocked_search_failures: dict[str, str] = {}

        while action_num < _HARD_SAFETY_CAP:
            milestone.actions_taken = action_num

            # 1. Perceive environment
            env_state = await env_perceiver()
            env_lines = [f"  {k}: {v}" for k, v in env_state.items() if v]
            env_str = "\n".join(env_lines) if env_lines else "  (no environment data)"

            # 2. Build action history
            history_lines: list[str] = []
            for i, a in enumerate(actions, 1):
                icon = "✓" if a.success else "✗"
                result_preview = (a.result[:150] if a.result else a.error[:100]).replace("\n", " ")
                history_lines.append(f"  {icon} Action {i}: {a.tool} → {result_preview}")
            action_history = "\n".join(history_lines) if history_lines else "  (none yet)"

            # 3. Build deliverables summary
            deliv_lines: list[str] = []
            for mid, dval in deliverables.items():
                m = next((m for m in plan.milestones if m.id == mid), None)
                label = m.goal if m else f"Milestone {mid}"
                deliv_lines.append(f"  [{mid}] {label}: {str(dval)[:300]}")
            deliv_str = "\n".join(deliv_lines) if deliv_lines else "  (none — this is the first milestone)"

            # 4. Build prompt
            prompt = MILESTONE_EXECUTOR_PROMPT.format(
                task_summary=plan.task_summary,
                milestone_id=milestone.id,
                total_milestones=len(plan.milestones),
                milestone_goal=milestone.goal,
                success_signal=milestone.success_signal,
                hint_tools=", ".join(milestone.hint_tools) if milestone.hint_tools else "(any)",
                deliverables=deliv_str,
                skill_context=skill_context or getattr(plan, "skill_context", "") or "(none)",
                observations=self._build_recent_observations(actions, last_result),
                search_leads=self._build_search_leads(actions),
                action_count=action_num,
                action_history=action_history,
                stall_warning=stall_warning,
                env_state=env_str,
                last_result=last_result[:800] if last_result else "(none)",
                tool_list=tool_list_text,
            )

            # 5. Ask LLM what to do
            t_llm = _time.time()
            try:
                response = await self.provider.generate(
                    messages=[{"role": "user", "parts": [{"text": prompt}]}],
                    system_prompt=MILESTONE_EXECUTOR_SYSTEM,
                    tools=[],
                    temperature=0.15,
                )
                llm_elapsed = _time.time() - t_llm

                if not response or not response.text:
                    err_msg = response.error if (response and hasattr(response, "error") and response.error) else "No text returned"
                    print(f"[MilestoneExec] ⚠ Empty LLM response at action {action_num + 1} (Reason: {err_msg})")
                    decision_failures += 1
                    if decision_failures >= 3:
                        print(f"[MilestoneExec] ⚠ Stalled: too many empty LLM responses")
                        break
                    continue

                decision = self._parse_decision(response.text)
                print(
                    f"[MilestoneExec]   Action {action_num + 1}: "
                    f"{'DONE' if decision.get('done') else decision.get('tool', '?')} "
                    f"({llm_elapsed:.2f}s) — {decision.get('reasoning', '')[:100]}"
                )

            except Exception as e:
                print(f"[MilestoneExec] ⚠ LLM decision failed: {e}")
                decision_failures += 1
                if decision_failures >= 3:
                    print(f"[MilestoneExec] ⚠ Stalled: too many LLM errors")
                    break
                continue

            # 6. Check if done
            if decision.get("done", False):
                deliverable = decision.get("deliverable", "")
                reasoning = decision.get("reasoning", "")
                evidence_ok, evidence_reason = self._completion_has_evidence(
                    milestone=milestone,
                    actions=actions,
                    deliverable=str(deliverable or reasoning or ""),
                )
                if not evidence_ok:
                    decision_failures += 1
                    last_result = f"Completion rejected: {evidence_reason}"
                    stall_warning = f"  Completion rejected: {evidence_reason}"
                    print(f"[MilestoneExec] ⚠ Completion rejected: {evidence_reason}")
                    if decision_failures >= 3:
                        print(f"[MilestoneExec] ⚠ Stalled: repeated unsupported completion claims")
                        break
                    continue
                milestone.result_summary = str(deliverable or reasoning)[:500]
                milestone.actions_taken = action_num
                print(f"[MilestoneExec] ✅ Milestone {milestone.id} COMPLETE: {reasoning[:150]}")
                return True, str(deliverable or reasoning)

            # 7. Execute the tool
            tool = decision.get("tool", "")
            args = decision.get("args", {})
            description = decision.get("description", "")

            if not tool:
                print(f"[MilestoneExec] ⚠ No tool specified, skipping action")
                decision_failures += 1
                if decision_failures >= 3:
                    print(f"[MilestoneExec] ⚠ Stalled: too many empty tool decisions")
                    break
                continue

            if priority_tool_names is not None and tool not in priority_tool_names:
                # Two-tier: check if tool is in the fallback set
                if fallback_tool_names is not None and tool in fallback_tool_names:
                    # Soft warning — accept but nudge toward priority tools
                    last_result = (
                        f"Tool '{tool}' executed (outside suggested set). "
                        f"Prefer: {', '.join(sorted(list(priority_tool_names)[:8]))}"
                    )
                    consecutive_rejections = 0
                    print(f"[MilestoneExec] ⚡ {tool} accepted via fallback tier")
                    # Fall through to execution below
                else:
                    # Hard rejection — truly unknown tool
                    consecutive_rejections += 1
                    decision_failures += 1
                    last_result = (
                        f"Tool '{tool}' is not available. "
                        f"Use one of: {', '.join(sorted(list(priority_tool_names)[:10]))}"
                    )
                    stall_warning = f"  {last_result}"
                    print(f"[MilestoneExec] ⚠ {last_result}")
                    # Auto-expand on stall: after 2 consecutive rejections,
                    # promote all fallback tools to priority.
                    if consecutive_rejections >= 2 and fallback_tool_names:
                        priority_tool_names = set(fallback_tool_names)
                        tool_list_text = self._format_tool_list(priority_tool_names)
                        consecutive_rejections = 0
                        decision_failures = max(0, decision_failures - 1)
                        print(f"[MilestoneExec] 🔓 Auto-expanded tool scope to {len(priority_tool_names)} tools")
                    if decision_failures >= 3:
                        print(f"[MilestoneExec] ⚠ Stalled: repeated out-of-scope tool selections")
                        break
                    continue

            if tool == "await_reply":
                await_signature = self._stable_args(args)
                if await_signature in blocked_awaits:
                    decision_failures += 1
                    last_result = (
                        "Repeated await_reply request blocked for this milestone. "
                        "Use the user's latest reply or choose a different tool."
                    )
                    stall_warning = f"  {last_result}"
                    print(f"[MilestoneExec] ⚠ {last_result}")
                    if decision_failures >= 3:
                        print(f"[MilestoneExec] ⚠ Stalled: repeated await_reply selections")
                        break
                    continue
            search_retry_key = self._search_retry_key(tool, args)
            if search_retry_key and search_retry_key in blocked_search_failures:
                decision_failures += 1
                last_result = blocked_search_failures[search_retry_key]
                stall_warning = f"  {last_result}"
                print(f"[MilestoneExec] ⚠ {last_result}")
                if decision_failures >= 3:
                    print(f"[MilestoneExec] ⚠ Stalled: repeated failed search selections")
                    break
                continue
            decision_failures = 0

            # Stream progress to UI
            if ws_callback:
                await ws_callback({
                    "type": "doing",
                    "text": description or f"Milestone {milestone.id}: {tool}",
                    "tool": tool,
                })

            t_exec = _time.time()
            result_str, success = await tool_executor(tool, args)
            exec_elapsed = _time.time() - t_exec

            action = MilestoneAction(
                tool=tool,
                args=args,
                result=result_str if success else "",
                success=success,
                error=result_str if not success else "",
                duration=exec_elapsed,
            )
            actions.append(action)
            last_result = result_str[:800] if result_str else "(empty result)"

            print(
                f"[MilestoneExec]   {'✓' if success else '✗'} {tool} ({exec_elapsed:.2f}s) "
                f"→ {last_result[:120]}"
            )

            if not success:
                search_retry_key = self._search_retry_key(tool, args)
                failure_summary = self._search_failure_summary(tool, args, result_str)
                if search_retry_key and failure_summary:
                    blocked_search_failures[search_retry_key] = failure_summary

                # ── Auto-screen-recovery: when a UI tool fails, automatically
                #    capture the screen so the LLM can SEE what went wrong ──
                if tool in _UI_MUTATING_TOOLS:
                    try:
                        screen_result, screen_ok = await tool_executor(
                            "read_screen",
                            {"question": f"What is on screen? The previous action '{tool}' failed."},
                        )
                        if screen_ok and screen_result:
                            auto_action = MilestoneAction(
                                tool="read_screen",
                                args={"question": "(auto-recovery after failed UI action)"},
                                result=screen_result,
                                success=True,
                                error="",
                                duration=0.0,
                            )
                            actions.append(auto_action)
                            last_result = (
                                f"[AUTO-RECOVERY] Previous action '{tool}' failed. "
                                f"Here is what the screen currently shows:\n"
                                f"{screen_result[:600]}"
                            )
                            print(f"[MilestoneExec] 👁 Auto-recovery: captured screen after failed {tool}")
                    except Exception as e:
                        print(f"[MilestoneExec] ⚠ Auto-recovery read_screen failed: {e}")

            if tool == "await_reply" and success:
                await_signature = self._stable_args(args)
                payload = {
                    "milestone_id": milestone.id,
                    "message": result_str,
                    "signature": await_signature,
                }
                milestone.actions_taken = action_num + 1
                milestone.result_summary = result_str[:500]
                return False, _AWAIT_REPLY_SENTINEL + json.dumps(payload, ensure_ascii=False)

            if tool == "send_response" and success and self._is_response_only_milestone(milestone):
                milestone.actions_taken = action_num + 1
                milestone.result_summary = self._send_response_completion_summary(
                    milestone=milestone,
                    args=args,
                    result_text=result_str,
                )
                return True, milestone.result_summary

            action_num += 1
            continue_running, next_stall_warning = self._detect_stall(actions)
            if not continue_running:
                print(f"[MilestoneExec] ⚠ Stalled: {next_stall_warning}")
                break
            stall_warning = f"  {next_stall_warning}" if next_stall_warning else "  (none)"

        milestone.actions_taken = action_num
        if action_num >= _HARD_SAFETY_CAP:
            print(f"[MilestoneExec] ⚠ Milestone {milestone.id} hit hard safety cap ({_HARD_SAFETY_CAP})")
        else:
            print(f"[MilestoneExec] ⚠ Milestone {milestone.id} stalled after {action_num} actions")
        milestone.result_summary = f"Stalled after {action_num} actions. Last result: {last_result[:200]}"

        substantive_actions = [
            a for a in actions
            if self._is_substantive_result(a) and self._is_action_relevant_to_milestone(a, milestone)
        ]
        if substantive_actions:
            return True, milestone.result_summary

        return False, milestone.result_summary

    def _stable_args(self, args: dict) -> str:
        """Normalize args for repeat-detection."""
        try:
            return json.dumps(args or {}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            return str(args or {})

    def _is_response_only_milestone(self, milestone: Milestone) -> bool:
        text = f"{milestone.goal or ''} {milestone.success_signal or ''}".lower()
        hint_tools = {str(tool).strip() for tool in (milestone.hint_tools or []) if str(tool).strip()}
        non_response_hints = hint_tools.difference({"send_response"})
        return bool(text) and any(marker in text for marker in _RESPONSE_ONLY_MILESTONE_MARKERS) and not non_response_hints

    def _send_response_completion_summary(self, milestone: Milestone, args: dict, result_text: str) -> str:
        modal = str((args or {}).get("modal", "")).strip().lower()
        if modal in {"cards", "products"}:
            return "Product modal sent to user."
        if modal:
            return f"{modal.title()} modal sent to user."
        if result_text:
            return str(result_text).strip()[:500]
        return "Response sent to user."

    def _search_retry_key(self, tool: str, args: dict) -> str:
        if tool != "get_web_information":
            return ""
        target_type = str((args or {}).get("target_type", "")).strip().lower()
        if target_type != "search_results":
            return ""
        query = " ".join(str((args or {}).get("query", "")).strip().lower().split())
        return f"{tool}|{target_type}|{query}"

    def _search_failure_summary(self, tool: str, args: dict, result_text: str) -> str:
        retry_key = self._search_retry_key(tool, args)
        if not retry_key:
            return ""
        error_code = "failed"
        route = ""
        try:
            data = json.loads(result_text)
        except (TypeError, ValueError):
            data = {}
        if isinstance(data, dict):
            error_code = str(data.get("error_code", "") or "failed").strip().lower()
            route = str(data.get("route", "")).strip().lower()
        query = str((args or {}).get("query", "")).strip()
        route_label = f" via {route}" if route else ""
        return (
            f"Repeated failed search blocked for '{query}' "
            f"({error_code or 'failed'}{route_label}). Use a different query or tool."
        )

    def _is_known_empty_json(self, result_text: str) -> bool:
        """Detect structured tool outputs that carry no useful payload."""
        try:
            data = json.loads(result_text)
        except (TypeError, ValueError):
            return False
        if not isinstance(data, dict):
            return False

        if int(data.get("item_count", 1) or 0) == 0:
            return True
        if int(data.get("content_length", 1) or 0) == 0:
            return True
        if int(data.get("total_elements", 1) or 0) == 0:
            return True
        if "items" in data and isinstance(data.get("items"), list) and len(data.get("items", [])) == 0:
            return True
        return False

    def _is_zero_yield_action(self, action: MilestoneAction) -> bool:
        """Zero-yield = tool call succeeded but produced no substantive output."""
        if not action.success:
            return False
        result_text = (action.result or "").strip()
        if len(result_text) < 60:
            return True
        return self._is_known_empty_json(result_text)

    def _detect_stall(self, actions: list[MilestoneAction]) -> tuple[bool, str]:
        """Detect milestone stalls and produce warning text for next LLM turn."""
        warning = ""

        if len(actions) >= 2:
            last = actions[-1]
            prev = actions[-2]
            if last.tool == prev.tool and self._stable_args(last.args) == self._stable_args(prev.args):
                warning = (
                    f"Repeated identical action detected: {last.tool} with same args. "
                    "Choose a different tool or arguments."
                )

        zero_yield_streak = 0
        for action in reversed(actions):
            if self._is_zero_yield_action(action):
                zero_yield_streak += 1
            else:
                break
        if zero_yield_streak >= 3:
            return False, "three consecutive zero-yield actions"

        recent_search_queries: list[str] = []
        recent_search_with_items = 0
        for action in reversed(actions):
            if not action.success:
                break
            try:
                data = json.loads(action.result)
            except (TypeError, ValueError):
                break
            if not isinstance(data, dict):
                break
            target_type = str(data.get("target_type", "")).strip().lower()
            if action.tool != "get_web_information" or target_type != "search_results":
                break
            if int(data.get("item_count", 0) or 0) <= 0:
                break
            recent_search_with_items += 1
            recent_search_queries.append(str(action.args.get("query", "")).strip().lower())

        if recent_search_with_items >= 3:
            return False, "repeated search-result gathering without following a source"
        if recent_search_with_items >= 2:
            warning = (
                "You already have search results with source URLs. Stop searching and open/read one of those sources next."
            )

        repeated_url_reads = 0
        last_url = ""
        last_target_type = ""
        for action in reversed(actions):
            if not action.success or action.tool != "get_web_information":
                break
            try:
                data = json.loads(action.result)
            except (TypeError, ValueError):
                break
            if not isinstance(data, dict):
                break
            target_type = str(data.get("target_type", "")).strip().lower()
            current_url = str(data.get("url", "")).strip().rstrip("/")
            if target_type not in {"page_content", "page_summary", "structured_data"} or not current_url:
                break
            if not last_url:
                last_url = current_url
                last_target_type = target_type
                repeated_url_reads = 1
                continue
            if current_url == last_url and target_type == last_target_type:
                repeated_url_reads += 1
            else:
                break

        if repeated_url_reads >= 2:
            return False, "repeated reading of the same source without new evidence"

        return True, warning

    def _is_substantive_result(self, action: MilestoneAction) -> bool:
        """Substantive result threshold for partial-success fallback."""
        if not action.success:
            return False
        if action.tool in _TRIVIAL_PROGRESS_TOOLS:
            return False
        result_text = (action.result or "").strip()
        result_lower = result_text.lower()
        if any(marker in result_lower for marker in _FAILED_UI_RESULT_MARKERS):
            return False
        if len(result_text) <= 100:
            return False
        if self._is_known_empty_json(result_text):
            return False
        return True

    def _has_observable_action_progress(self, action: MilestoneAction) -> bool:
        if not action.success:
            return False
        if action.tool in _LOW_SIGNAL_ACTION_TOOLS:
            return False
        result_text = (action.result or "").strip()
        result_lower = result_text.lower()
        if any(marker in result_lower for marker in _FAILED_UI_RESULT_MARKERS):
            return False
        if self._is_known_empty_json(result_text):
            return False
        return bool(result_text)

    def _completion_has_evidence(
        self,
        milestone: Milestone,
        actions: list[MilestoneAction],
        deliverable: str,
    ) -> tuple[bool, str]:
        deliverable_text = (deliverable or "").strip()
        goal_text = f"{milestone.goal} {milestone.success_signal}".lower()
        evidence_sensitive_markers = (
            "research", "source", "read", "extract", "listing", "result",
            "link", "compare", "price", "market", "document", "report",
        )
        evidence_sensitive = any(marker in goal_text for marker in evidence_sensitive_markers)

        if not actions:
            # Allow the LLM to declare DONE on turn 1 if it provides a
            # meaningful deliverable — the task may already be satisfied.
            if deliverable_text:
                return True, ""
            return False, "no actions executed yet"

        substantive_actions = [
            action for action in actions
            if self._is_substantive_result(action) and self._is_action_relevant_to_milestone(action, milestone)
        ]
        if substantive_actions:
            return True, ""

        if len(deliverable_text) >= 120:
            if any(
                self._is_action_relevant_to_milestone(action, milestone)
                and self._has_observable_action_progress(action)
                for action in actions
            ):
                return True, ""
        if "http" in deliverable_text and len(deliverable_text) >= 40:
            if any(
                self._is_action_relevant_to_milestone(action, milestone)
                and self._has_observable_action_progress(action)
                for action in actions
            ):
                return True, ""

        successful_nontrivial = [
            action for action in actions
            if (
                action.success
                and action.tool not in _TRIVIAL_PROGRESS_TOOLS
                and not self._is_zero_yield_action(action)
                and self._is_action_relevant_to_milestone(action, milestone)
            )
        ]
        if successful_nontrivial:
            return True, ""

        if deliverable_text and len(deliverable_text) >= 20:
            if any(
                self._is_action_relevant_to_milestone(action, milestone)
                and self._has_observable_action_progress(action)
                for action in actions
            ):
                return True, ""

        if evidence_sensitive:
            return False, "success signal not backed by extracted evidence yet"

        return False, "insufficient evidence for milestone completion"

    def _parse_decision(self, text: str) -> dict:
        """Parse the LLM's JSON decision."""
        cleaned = text.strip()
        if cleaned.startswith("```"):
            first_nl = cleaned.index("\n") if "\n" in cleaned else len(cleaned)
            cleaned = cleaned[first_nl + 1:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            start = cleaned.find("{")
            end = cleaned.rfind("}") + 1
            if start >= 0 and end > start:
                try:
                    return json.loads(cleaned[start:end])
                except json.JSONDecodeError:
                    pass
            print(f"[MilestoneExec] ⚠ Could not parse decision JSON")
            return {"done": False, "tool": "", "args": {}, "reasoning": "Parse error"}


# ═══════════════════════════════════════════════════════════════
#  Factory / Singleton
# ═══════════════════════════════════════════════════════════════

_instance: Optional[MilestoneExecutor] = None


def get_milestone_executor(
    provider: LLMProvider,
    tool_declarations: list[dict],
) -> MilestoneExecutor:
    """Get or create the milestone executor singleton."""
    global _instance
    if _instance is None or _instance.provider is not provider:
        _instance = MilestoneExecutor(provider, tool_declarations)
    else:
        _instance._tool_declarations = tool_declarations
        _instance._build_tool_list()
    return _instance
