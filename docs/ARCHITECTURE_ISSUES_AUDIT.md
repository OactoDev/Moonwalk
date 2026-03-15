# Moonwalk Architecture Issues Audit

> **Date:** 14 March 2026  
> **Scope:** Full codebase review — agent core, tools, browser layer, providers, multi-agent, runtime state, servers, and tests.

---

## Table of Contents

1. [Architecture-Level Issues](#1-architecture-level-issues)
2. [Agent Core (`core_v2.py`) Issues](#2-agent-core-core_v2py-issues)
3. [Planner & Task Planner Issues](#3-planner--task-planner-issues)
4. [Tool Registry & Tool System Issues](#4-tool-registry--tool-system-issues)
5. [Tool Selector Issues](#5-tool-selector-issues)
6. [Browser Layer Issues](#6-browser-layer-issues)
7. [Provider & Router Issues](#7-provider--router-issues)
8. [Multi-Agent System Issues](#8-multi-agent-system-issues)
9. [Memory & State Management Issues](#9-memory--state-management-issues)
10. [Server & Runtime Issues](#10-server--runtime-issues)
11. [Verification System Issues](#11-verification-system-issues)
12. [Legacy Code & Tech Debt](#12-legacy-code--tech-debt)
13. [Test Coverage Gaps](#13-test-coverage-gaps)
14. [Security & Reliability Concerns](#14-security--reliability-concerns)
15. [Recommendations Summary](#15-recommendations-summary)

---

## 1. Architecture-Level Issues

### 1.1 God Object — `MoonwalkAgentV2` (~2,400 lines)

**Severity: HIGH**

`core_v2.py` is a monolithic 2,359-line class that owns routing, planning, execution, verification, research synthesis, browser recovery, working memory orchestration, plan gating, conversation management, perception, and WebSocket streaming. This violates single-responsibility and makes the file extremely difficult to reason about, test in isolation, or extend.

**Specific concerns:**
- `_execute_step()` is ~250 lines with deeply nested try/except blocks
- `_execute_milestone_plan()` is ~200 lines with 8+ closure-captured variables and a nested `_AwaitReplySignal` exception class defined inline
- The `run()` method is ~200 lines of branching logic for pending plans, pending executions, plan modifications, routing, planning, gating, and execution
- Research logging (`_log_research_content`, `_extract_research_text`, `_collect_research_snippets`, `_synthesize_research_body`, `_build_research_stream_lines`, `_emit_research_stream`) adds ~250 lines of inline content-extraction logic that should be a separate service

**Recommendation:** Extract into `AgentExecutor`, `ResearchService`, `PlanGateService`, `BrowserRecoveryService`, etc.

### 1.2 Tight Coupling Between Layers

**Severity: MEDIUM**

Multiple layers reach into each other's internals rather than going through clean interfaces:

- `core_v2.py` imports from `browser.bridge`, `browser.store`, `browser.selector_ai` directly for recovery logic
- `tools/selector.py` imports from `browser.bridge`, `browser.store`, `browser.interpreter_ai`, `runtime_state` — the tool selector shouldn't need direct browser bridge access
- `tools/gworkspace_tools.py` directly calls `browser_bridge.queue_action()`, `browser_store.get_snapshot()` — mixing browser internals with tool implementations
- `agent/milestone_executor.py` imports from `tools.selector` for `expand_milestone_hint_tools` — planning infrastructure leaks into execution

### 1.3 Two Parallel State Systems

**Severity: MEDIUM**

There are two overlapping state tracking systems:
1. `RuntimeStateStore` (`runtime_state.py`) — canonical dataclass-based store with `OSState`, `BrowserState`, `RequestState`, `SessionState`
2. `_TOOL_GATEWAY_CONTEXT` (a `contextvars.ContextVar` dict in `selector.py`) — ad-hoc gateway context

Both track `active_app`, `browser_url`, connection state, etc. They are synced manually via `set_tool_gateway_context()` calls scattered across `core_v2.py`, but can drift out of sync. The `_sync_live_browser_context()` function in `selector.py` tries to paper over this by re-reading both, but it's fragile.

### 1.4 Version Labeling Confusion

**Severity: LOW**

Comments and docstrings reference "V2" and "V3" inconsistently:
- `planner.py` calls milestones "V3 planning unit" in the comment header
- `task_planner.py` docstring says "Task Planner V2" but the planning prompt section says "Milestone Planning Prompt (V3)"
- `core_v2.py` says "Sense-Plan-Act-Verify (SPAV) architecture" in the docstring but the actual architecture is milestone-first with LLM micro-loops, not classical SPAV
- The `create_agent()` factory still accepts a `version` parameter that is ignored

---

## 2. Agent Core (`core_v2.py`) Issues

### 2.1 System Prompt Bloat

**Severity: MEDIUM**

`SYSTEM_PROMPT_V2` is ~250 lines of detailed instructions covering tool usage rules, modal types, Google Workspace rules, image workflows, plan approval flows, and response calibration. Two versions exist (`SYSTEM_PROMPT_V2` and `SYSTEM_PROMPT_V2_COMPACT`) but the compact version immediately overwrites the full version:

```python
SYSTEM_PROMPT_V2 = SYSTEM_PROMPT_V2_COMPACT
```

This means the full 250-line prompt is defined but never used — dead code consuming tokens of context.

### 2.2 Recursive `_execute_step()` Retry

**Severity: MEDIUM**

On verification failure with `should_retry`, the step is retried via recursive call:

```python
return await self._execute_step(step, world_state, ws_callback, ...)
```

If `max_retries` is misconfigured or the retry logic doesn't converge, this could stack overflow. Should use an iterative retry loop instead.

### 2.3 Silently Swallowed Exceptions

**Severity: MEDIUM**

Numerous `except Exception: pass` blocks throughout `core_v2.py`:
- Browser refresh after navigation (`except Exception: pass`)
- Post-web-search snapshot refresh (`except Exception: pass`)
- Browser URL tracking during navigation recovery (`except Exception: pass`)
- `send_response` at the end of milestone execution (`except Exception as e:` with only a print)
- Pre-read browser domain mismatch detection (`except Exception: pass`)

These mask real failures and make debugging extremely difficult.

### 2.4 Closure Variable Mutation in `_execute_milestone_plan`

**Severity: MEDIUM**

The `tool_executor` closure mutates `milestone_step_result_idx` and `last_await_payload` via `nonlocal`, and `last_tool_was_ui_mutating` is assigned inside the closure but read outside it. This creates a subtle bug:

```python
last_tool_was_ui_mutating = tool in _UI_MUTATING_TOOLS
```

This is a *local assignment* inside the `tool_executor` function — but the `milestone_env_perceiver` closure reads the outer scope variable. The `nonlocal` keyword is missing for `last_tool_was_ui_mutating`, so the assignment creates a new local variable in `tool_executor` and the outer closure always sees the original `False` value. **Passive visual injection after UI-mutating tools never actually triggers.**

### 2.5 `_pending_plan` and `_pending_execution` Concurrency

**Severity: LOW**

These are set on the agent instance (`self._pending_plan`, `self._pending_execution`) with no locking. If two `run()` calls are concurrent (e.g., WebSocket messages arrive rapidly), the second could clear or overwrite the first's pending state.

### 2.6 `StopAsyncIteration` Used as Flow Control

**Severity: LOW**

`send_response` in benchmark mode raises `StopAsyncIteration` to signal completion, which is caught in `_execute_step()` and at the end of `_execute_milestone_plan()`. Using exceptions for control flow is an anti-pattern and makes the code harder to follow.

---

## 3. Planner & Task Planner Issues

### 3.1 Duplicate Shortcut Checks

**Severity: LOW**

Both `create_plan()` and `create_milestone_plan()` in `task_planner.py` check for media shortcuts and repeat message shortcuts. If called sequentially (which they are — `create_plan` → `create_milestone_plan`), the same shortcuts are evaluated twice.

### 3.2 `should_use_milestones()` is Always True

**Severity: LOW**

```python
def should_use_milestones(self, ...) -> bool:
    """Milestone planning is now universal for all requests."""
    return True
```

This method exists, accepts three parameters, and always returns `True`. Dead code that adds confusion.

### 3.3 `_hard_safety_clarification_prompt` is Trivially Bypassable

**Severity: MEDIUM**

The safety gate checks for exact string patterns like `"rm -rf /"` and `"delete production database"`. These are easily bypassed with minor rewording (e.g., `"remove recursive force /"`, `"drop the production db"`). A more robust approach using intent analysis would be better.

### 3.4 JSON Parsing is Fragile

**Severity: MEDIUM**

`_parse_milestone_response()` strips markdown code fences with a simple `startswith("```")` check and tries to extract JSON with `find("{")`/`rfind("}")`. This can fail if:
- The model returns nested objects where the first `{` is inside a string
- The model outputs multiple JSON blocks
- The model uses JSONL or array format

### 3.5 Replanning Creates Unbounded Milestone IDs

**Severity: LOW**

`replan_remaining()` assigns `next_id = max(m.id for m in plan.milestones) + 1`. After multiple replan cycles, IDs grow unboundedly while the plan's milestone list gets replaced. Milestone references in `depends_on` could become stale if they reference old IDs that were removed.

### 3.6 `TaskPlanner` Inherits from `LegacyTaskPlannerCompatMixin`

**Severity: LOW**

The active planner inherits from a legacy compatibility mixin that adds ~480 lines of step-plan methods (`_try_template`, `_normalize_step_args`, `_renumber_steps`, etc.) that are "not part of the active milestone runtime." This increases the cognitive load and attack surface of the class.

---

## 4. Tool Registry & Tool System Issues

### 4.1 No Tool Timeout or Resource Limits

**Severity: HIGH**

`ToolRegistry.execute()` calls `await tool.func(**clean_args)` with no timeout. A misbehaving tool (e.g., `run_shell` with a hanging command, `web_scrape` on a slow server) blocks the entire agent indefinitely. Only the `_osascript` helper has a 5-second timeout.

### 4.2 Error Return as String, Not Exception

**Severity: MEDIUM**

Tool errors are returned as string messages (`f"Error: Unknown tool '{name}'"`, `f"Error executing {name}: {e}"`). This forces every caller to parse strings for errors rather than using structured error handling. The `contracts.py` module defines `error_envelope` and `success_envelope` for structured responses, but only some tools use them.

### 4.3 Registry is a Global Singleton

**Severity: MEDIUM**

```python
registry = ToolRegistry()
```

All tools register on a single global instance. This makes:
- Testing difficult (can't create isolated registries)
- Plugin systems impossible (can't have environment-specific tool sets)
- Parallel agent execution risky (shared mutable state)

### 4.4 `_REASONING_EXEMPT_TOOLS` is Hardcoded

**Severity: LOW**

The set of reasoning-exempt tools (`send_response`, `await_reply`) is a module-level frozen set. Adding new exempt tools requires modifying the registry source code rather than being configurable per-tool via the decorator.

### 4.5 Inconsistent Tool Error Formats

**Severity: MEDIUM**

Different tools return errors in different formats:
- `browser_tools.py`: Uses `_error_payload()` → structured JSON with `error_code`, `message`, `session_id`
- `mac_tools.py`: Returns plain strings like `"ERROR: App not found"` or `"AppleScript error: ..."`
- `cloud_tools.py`: Returns `"ERROR: Server returned status {code}"` or `"ERROR fetching URL: {e}"`
- `file_tools.py`: Returns `"ERROR: File not found: {path}"`
- `gworkspace_tools.py`: Mixed — some use structured payloads, some return plain strings

The verifier has to handle all these formats, leading to fragile string-matching logic.

---

## 5. Tool Selector Issues

### 5.1 Massive File Size

**Severity: MEDIUM**

`selector.py` is 2,176 lines — it contains the `ToolSelector` class, the `get_web_information` gateway tool registration, search result handling, route policy integration, browser snapshot management, and dozens of helper functions. This is doing far too much for a "selector."

### 5.2 `get_web_information` is a Meta-Tool Inside the Selector

**Severity: HIGH**

The `get_web_information` tool is *registered* inside `selector.py` (not in `cloud_tools.py` or `browser_tools.py`). It's a ~400-line gateway that:
- Parses target types and normalizes them
- Makes route policy decisions (browser ACI vs background fetch)
- Executes web searches
- Follows search results using LLM-assisted selection
- Falls back between multiple execution strategies
- Handles search result parsing, structured extraction, and readability

This is a full compound operation masquerading as a single tool. It should be extracted into its own module.

### 5.3 Sticky Route State with No Cleanup

**Severity: LOW**

```python
_ROUTE_STICKY_STATE: dict[str, dict] = {}
```

Module-level mutable state that caches route decisions with a TTL (`_STICKY_ROUTE_TTL_S = 300.0`). No mechanism to clear this on agent reset, session change, or error. Stale routes can cause incorrect behavior after context changes.

### 5.4 `_ABSTRACT_WEB_INFO_TOOLS` / `_ALWAYS_AVAILABLE_TOOLS` Duplication

**Severity: LOW**

Multiple frozen sets define overlapping tool categories across `selector.py`, `core_v2.py`, `milestone_executor.py`, and `verifier.py`. Tool classification is not centralized — each module maintains its own categorization, leading to drift.

---

## 6. Browser Layer Issues

### 6.1 Snapshot Stale Check is Only Time-Based

**Severity: MEDIUM**

`_snapshot_health()` only checks age (`time.time() - snapshot.timestamp`). A snapshot can be "fresh" (< 5s old) but completely stale if the user navigated to a new page. The auto-refresh logic in `_execute_step()` tries to handle this via domain comparison, but it only fires for read tools, not for click or type operations.

### 6.2 `_lookup_snapshot()` Falls Back to Global Snapshot

**Severity: MEDIUM**

```python
def _lookup_snapshot(session_id: str = ""):
    snapshot = browser_store.get_snapshot(session_id or None)
    if snapshot:
        return snapshot
    global_snap = browser_store.get_snapshot(None)
    if global_snap:
        return global_snap
    return None
```

If a session-specific lookup fails, it falls back to *any* available snapshot. This can return a snapshot from a completely different tab or browser window, leading to tools operating on the wrong page context.

### 6.3 Bridge Authentication is a Static Token

**Severity: MEDIUM**

```python
self._session_token = os.environ.get("MOONWALK_BROWSER_BRIDGE_TOKEN", "") or "dev-bridge-token"
```

The default token is a hardcoded `"dev-bridge-token"`. In development/local mode, any local process can connect to the bridge and inject browser actions.

### 6.4 No Bridge Disconnection Detection

**Severity: MEDIUM**

`BrowserBridge.is_connected()` only checks if `_connected_session_id` is set. There is no heartbeat or timeout mechanism. If the Chrome extension disconnects (crash, tab close, extension update), the bridge still reports as connected, and actions get queued indefinitely with no delivery.

### 6.5 ACI Tools Hide Raw Browser Tools

**Severity: LOW**

`milestone_executor.py` hides 12 raw browser tools from the LLM prompt:

```python
_RAW_BROWSER_TOOLS: frozenset[str] = frozenset({
    "browser_snapshot", "browser_find", "browser_click_match", ...
})
```

But these tools remain callable via `tool_executor`. The LLM can't see them but the ACI tools internally compose them. This creates an invisible dependency where ACI tools silently fail if the underlying raw tools are broken, with no LLM-visible error path.

---

## 7. Provider & Router Issues

### 7.1 Gemini-Only Provider Lock-In

**Severity: HIGH**

Despite having `LLMProvider` as an abstract base class and an `OllamaProvider` in the providers directory, the entire system is hardcoded for Gemini:
- `router.py` imports and instantiates only `GeminiProvider`
- `_classify_with_router()` directly uses `google.genai.types`
- Model names are Gemini-specific (`gemini-3-flash-preview`, `gemini-3.1-pro-preview-customtools`)
- The routing prompt is calibrated for Gemini's strength/weakness profile
- `gemini.py` has Gemini-specific thinking config (`ThinkingConfig`)

Adding a non-Gemini provider would require significant refactoring of the router.

### 7.2 Router Initialization Race Condition

**Severity: MEDIUM**

```python
async def initialize(self):
    if self._initialized:
        return
    self._initialized = True
    ...
```

This isn't thread-safe. If two `route()` calls happen concurrently before initialization, both will enter the initialization block. The `_initialized = True` set before the actual initialization completes means a second caller could see `_initialized = True` and proceed with `None` providers.

### 7.3 `_fallback` Provider is Undeclared

**Severity: LOW**

In `ModelRouter.__init__()`, `self._fallback` is not declared. It's only set in `initialize()`. If `route()` is called and initialization fails before the fallback assignment, accessing `self.fallback` (the `@property`) will raise `AttributeError`.

### 7.4 No Rate Limiting or Token Counting

**Severity: MEDIUM**

No mechanism to:
- Track token usage across requests
- Implement rate limiting for API calls
- Budget tokens across milestone executions
- Detect and handle quota exhaustion gracefully

The `_HARD_SAFETY_CAP = 50` actions per milestone in `milestone_executor.py` limits actions but not API calls or tokens.

---

## 8. Multi-Agent System Issues

### 8.1 Parallel Execution Shares Mutable State

**Severity: HIGH**

When milestones execute in parallel via `SubAgentManager._execute_parallel()`, all parallel tasks share:
- The same `tool_executor` closure (which mutates `milestone_step_result_idx`, `last_await_payload`, `last_tool_was_ui_mutating`)
- The same `WorkingMemory` instance on the agent
- The same `browser_bridge` and `browser_store` singletons
- The same `_TOOL_GATEWAY_CONTEXT` contextvars state

Two parallel milestones could simultaneously navigate to different URLs, overwrite each other's browser state, and corrupt the snapshot. The `RemoteExecutor` is described as providing "isolated state," but it only isolates the milestone tracking — not the underlying tool execution environment.

### 8.2 `execute_fn` Closure Creates Deep Dependency Chain

**Severity: MEDIUM**

The `milestone_execute_fn` closure defined inside `_execute_milestone_plan()` captures 15+ variables from the outer scope. This closure is passed through `SubAgentManager.dispatch()` → `RemoteExecutor.execute()` → back to the closure. If any captured variable changes between closure creation and execution (common in parallel paths), behavior is undefined.

### 8.3 `_AwaitReplySignal` Exception Propagation Through `asyncio.gather`

**Severity: MEDIUM**

When parallel milestones run via `asyncio.gather`, if one milestone raises `_AwaitReplySignal`, `SubAgentManager._execute_parallel()` detects it via duck-typing:

```python
for result in results:
    if isinstance(result, Exception) and _is_suspend_signal(result):
        raise result
```

But `_is_suspend_signal` uses `hasattr` checks. If any other exception happens to have `suspended_milestone_id` and `await_payload` attributes, it would be misidentified as a suspend signal. The remaining parallel tasks get abandoned with no cleanup.

---

## 9. Memory & State Management Issues

### 9.1 Session Persistence is JSON on Disk with No Locking

**Severity: MEDIUM**

`ConversationMemory._save_session()` writes JSON to `~/.moonwalk/sessions/{id}.json` with no file locking. If two agent processes run concurrently (e.g., during development), they can corrupt session files.

### 9.2 Research Snippet Deduplication is Weak

**Severity: LOW**

`_collect_research_snippets()` deduplicates by:
1. Lowercasing and truncating content to 800 chars
2. Exact source URL match

This misses near-duplicate content from the same source (e.g., a page read at different scroll positions) and allows semantically identical content from different URLs to accumulate.

### 9.3 Working Memory Has No Session Boundary

**Severity: MEDIUM**

`WorkingMemory` tracks actions, entities, URLs, and research snippets for the "current session," but there's no clear mechanism to reset it when a new session starts. The `ConversationMemory` has `_check_timeout()` for auto-clearing, but `WorkingMemory` does not. Stale working memory from a previous task can pollute a new unrelated task.

### 9.4 `UserProfile.extract_facts()` Runs on Every Request

**Severity: LOW**

```python
extracted = self.user_profile.extract_facts(user_text)
```

This is called on every `run()` invocation. Without seeing the implementation, if it does any NLP or pattern matching on every input, it's wasted compute for action-oriented requests like "open Spotify."

### 9.5 `_trim()` Mutates First Turn In-Place

**Severity: LOW**

When conversation history exceeds `max_turns`, `_trim()` prepends a `[SYSTEM SUMMARY]` marker to the first remaining turn's text:

```python
compressed_msg = f"[SYSTEM SUMMARY: {dropped_count} older turns...]\\n\\n{first_text}"
self._turns[0]["parts"][0]["text"] = compressed_msg
```

This modifies the user's original message, which could confuse the LLM if it relies on the exact user input for intent parsing.

---

## 10. Server & Runtime Issues

### 10.1 Voice Pipeline Blocking Logic

**Severity: MEDIUM**

In `VoiceAssistant`, the audio processing and agent execution happen in the same async loop. `run_agent_text()` blocks until the agent returns. During this time, audio processing is paused. If the agent takes 30+ seconds (common for research tasks), the user gets no feedback and cannot interrupt.

### 10.2 No Request Queuing or Cancellation

**Severity: MEDIUM**

There's no mechanism to:
- Queue incoming requests while the agent is busy
- Cancel an in-progress request from the user (other than saying "cancel" after it completes)
- Timeout a stalled request

### 10.3 Hardcoded Port Numbers

**Severity: LOW**

The browser bridge server uses hardcoded `BRIDGE_HOST`/`BRIDGE_PORT` from `browser_bridge_server.py`. The WebSocket server port is also hardcoded. These should be configurable via environment variables.

### 10.4 `RuntimeStateStore` Has No Observers

**Severity: LOW**

`RuntimeStateStore` is a passive dataclass store. Components poll it for state. An event/observer pattern would allow reactive updates (e.g., automatically updating tool gateway context when browser state changes).

---

## 11. Verification System Issues

### 11.1 Regex-Based Error Detection Has False Positives

**Severity: MEDIUM**

```python
ERROR_PATTERNS = [
    r"^error", r"failed", r"exception", r"not found",
    r"permission denied", r"cannot", r"couldn't", r"unable to", ...
]
```

These patterns match against tool results. A `read_file` command reading a log file containing the word "error" would be flagged as a failure. The `content_tools` exclusion set mitigates this for some tools, but any new content-returning tool not added to this set will have false positive failures.

### 11.2 Verification is Per-Tool, Not Per-Goal

**Severity: MEDIUM**

The verifier checks if individual tool calls succeeded, not whether they made progress toward the milestone goal. A tool call can "succeed" (open a URL) but produce no useful result (the URL leads to a 404 page that returns a 200 status). There's no semantic verification of whether the milestone's `success_signal` was actually met — that's left entirely to the LLM's judgment.

### 11.3 `verify_with_visual` Adds Latency for Every UI-Mutating Tool

**Severity: LOW**

Visual verification captures a screenshot after every UI-mutating tool call. This adds ~0.5-1s per action. For multi-step UI workflows (e.g., filling a form with 10 fields), this adds 5-10s of overhead.

### 11.4 Missing Verifiers for Several Tools

**Severity: LOW**

The verifier's `_verifiers` dict only covers ~35 tools. Tools like `gsheets_create`, `gsheets_write`, `gsheets_append_rows`, `gslides_create`, `gmail_send`, `gcal_create_event`, `browser_switch_tab`, `save_image`, `clipboard_ops`, `window_manager` have no specific verifier and fall back to generic string-matching.

---

## 12. Legacy Code & Tech Debt

### 12.1 Legacy Files Still Present

**Severity: LOW**

- `agent/legacy_planner.py` (497 lines) — "no longer part of the active V2 runtime"
- `agent/legacy_task_planner.py` (480 lines) — "not part of the active milestone runtime"
- `ExecutionStep` in `planner.py` (40+ lines) — only used by the legacy path and benchmarks
- `ExecutionPlan` in `legacy_planner.py` — "kept for compatibility only"
- `PlanTemplates` in `legacy_planner.py` — used only by `LegacyTaskPlannerCompatMixin`

These add ~1,000 lines of code that create confusion about what the active runtime actually uses.

### 12.2 Dead `max_actions` Field

**Severity: LOW**

```python
max_actions: int = 0  # Deprecated (kept for backward compatibility only)
```

Still present on `Milestone` and referenced in comments. Should be removed with a migration.

### 12.3 Multiple Unused Imports and Definitions

**Severity: LOW**

- `SYSTEM_PROMPT_V2` (the full version) is defined but immediately overwritten by `SYSTEM_PROMPT_V2_COMPACT`
- Several `frozenset` definitions are duplicated across files (browser app names, search result hosts, etc.)
- `_osascript` helper in `registry.py` — should be in a utilities module

### 12.4 `.ppn` Wake Word Files in Multiple Directories

**Severity: LOW**

`hey_moonwalk.ppn` exists in:
- `backend/hey_moonwalk.ppn`
- `backend/agent/hey_moonwalk.ppn`
- `backend/servers/hey_moonwalk.ppn`

Only one should be the canonical copy.

---

## 13. Test Coverage Gaps

### 13.1 No Integration Tests for the Full Agent Loop

**Severity: HIGH**

There are 169 formal test functions, but none test the full `agent.run()` → routing → planning → execution → verification → response pipeline end-to-end with a mocked LLM. All tests are unit tests that test individual components.

### 13.2 No Tests for `gworkspace_tools.py`'s Browser Automation Path

**Severity: MEDIUM**

`test_gworkspace_tools.py` has 5 tests, but they only test the helper functions and GDocs API path. The browser automation fallback (which is the primary execution path for users without OAuth tokens) is untested.

### 13.3 No Tests for `cloud_tools.py`

**Severity: MEDIUM**

`fetch_web_content` and `web_scrape` have zero test coverage. Their regex-based HTML parsing is fragile and should have regression tests for various HTML structures.

### 13.4 No Tests for `local_server.py`

**Severity: MEDIUM**

The WebSocket server, voice pipeline, audio processing, and conversation mode logic have no automated tests. The `test_server.py` and `test_server2.py` are manual scripts, not pytest tests.

### 13.5 Script-Style Tests Should Be Converted

**Severity: LOW**

15 test files are script-style (using `asyncio.run(main())` instead of pytest fixtures). These don't run in CI, don't report failures properly, and don't participate in coverage measurement.

### 13.6 No Tests for Provider Fallback and Error Paths

**Severity: MEDIUM**

The `ModelRouter` fallback logic (primary model fails → fallback model), API retry logic (3 retries in `gemini.py`), and provider unavailability paths have no test coverage.

---

## 14. Security & Reliability Concerns

### 14.1 `run_shell` Has No Sandboxing

**Severity: HIGH**

The `run_shell` tool executes arbitrary shell commands with the user's full permissions. There is no sandboxing, no allowlist/denylist of commands, and only a trivial `_hard_safety_clarification_prompt` check for exact strings like `"rm -rf /"`.

### 14.2 `_chrome_js` Executes Arbitrary JavaScript

**Severity: MEDIUM**

`gworkspace_tools.py`'s `_chrome_js()` executes arbitrary JavaScript in Chrome via AppleScript. The input is escaped for AppleScript but not sanitized for JS injection. If attacker-controlled content reaches this function, it could exfiltrate browser cookies or session tokens.

### 14.3 File Operations Have No Path Restrictions

**Severity: MEDIUM**

`read_file` and `write_file` can access any path the user has permissions for. There's no restriction to prevent the agent from reading `~/.ssh/id_rsa`, writing to `/etc/hosts`, or modifying system configuration files.

### 14.4 No Input Validation on Tool Arguments

**Severity: MEDIUM**

Tool functions receive LLM-generated arguments directly. While `clean_args` strips the `reasoning` key, there's no schema validation that the required arguments are present, correctly typed, or within acceptable ranges.

### 14.5 OAuth Tokens Stored in Plain Text

**Severity: LOW**

```python
_TOKEN_PATH = os.path.expanduser("~/.moonwalk/gcloud_token.json")
```

Google OAuth2 tokens are stored in a plain JSON file with default filesystem permissions. No encryption, no expiry enforcement, no token rotation.

---

## 15. Recommendations Summary

### Critical (Fix Now)
1. **Add tool execution timeouts** — wrap every `await tool.func()` with `asyncio.wait_for()`
2. **Fix `last_tool_was_ui_mutating` nonlocal bug** — add `nonlocal` declaration in `tool_executor`
3. **Add basic sandboxing for `run_shell`** — at minimum, a denylist of dangerous commands
4. **Add integration tests** for the full agent loop with mocked LLM

### High Priority (Fix Soon)
5. **Break up `core_v2.py`** — extract research, browser recovery, plan gating, and execution into separate modules
6. **Unify error formats** — all tools should return structured envelopes via `contracts.py`
7. **Fix parallel milestone state sharing** — clone or isolate mutable state per parallel branch
8. **Abstract provider layer** — make the router provider-agnostic so non-Gemini providers work

### Medium Priority (Plan For)
9. **Consolidate state systems** — merge `_TOOL_GATEWAY_CONTEXT` into `RuntimeStateStore`
10. **Add token counting and rate limiting** to the provider layer
11. **Add browser bridge heartbeat/disconnect detection**
12. **Convert script-style tests to pytest**
13. **Add path restrictions for file tools**
14. **Clean up legacy code** — remove `legacy_planner.py`, `legacy_task_planner.py`, and dead `ExecutionPlan`

### Low Priority (Tech Debt)
15. **Normalize version labels** — pick V2 or V3, document the transition clearly
16. **Deduplicate tool category sets** — centralize `_BROWSER_APPS`, `_UI_MUTATING_TOOLS`, etc.
17. **Remove dead `.ppn` copies** — keep one canonical wake word file
18. **Make tool reasoning exemption configurable** per-tool via the decorator
19. **Add request queuing and cancellation** to the server

---

*This audit covers the Python backend only. The Electron frontend (`main.js`, `renderer/`), Chrome extension (`chrome_extension/`), and benchmark harness (`benchmarks/`) were not reviewed in depth.*
