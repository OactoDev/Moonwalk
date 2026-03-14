"""
Moonwalk — Tool Registry
=========================
Decorated Python functions that the LLM can invoke by name.
Each tool auto-registers into the global registry and exports
its schema in Gemini function_declarations format.

Every tool declaration automatically includes a `reasoning` argument
so the LLM must explain *why* it is calling the tool.  The reasoning
string is stripped before execution (tools never see it).
"""

import asyncio
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Any, Optional
from tools.contracts import dumps as contract_dumps
from tools.contracts import error_envelope


# ═══════════════════════════════════════════════════════════════
#  Reasoning injection — auto-appended to every tool declaration
# ═══════════════════════════════════════════════════════════════

_REASONING_PROPERTY: dict = {
    "reasoning": {
        "type": "string",
        "description": (
            "One sentence explaining WHY you are calling this tool right now "
            "and what you expect the result to be. REQUIRED."
        ),
    }
}

# Tools that do not need a reasoning argument (communication tools)
_REASONING_EXEMPT_TOOLS: frozenset[str] = frozenset({
    "send_response", "await_reply",
})


# ═══════════════════════════════════════════════════════════════
#  Tool Registry Infrastructure
# ═══════════════════════════════════════════════════════════════

@dataclass
class ToolDef:
    """Metadata for a registered tool."""
    name: str
    description: str
    parameters: dict          # JSON-schema style
    func: Callable            # The actual async function


class ToolRegistry:
    """Holds all available tools and serializes them for Gemini."""

    def __init__(self):
        self._tools: dict[str, ToolDef] = {}

    def register(self, name: str, description: str, parameters: dict):
        """Decorator to register a tool function."""
        def decorator(func: Callable):
            self._tools[name] = ToolDef(
                name=name,
                description=description,
                parameters=parameters,
                func=func,
            )
            return func
        return decorator

    async def execute(self, name: str, args: dict) -> str:
        """Execute a tool by name with given arguments.

        The ``reasoning`` key is silently stripped from *args* so tool
        functions never need to accept it.  Returns result string.
        """
        tool = self._tools.get(name)
        if not tool:
            return contract_dumps(error_envelope(
                "tool.unknown",
                f"Unknown tool '{name}'",
                source="tool.registry",
                details={"tool_name": name},
                flatten_details=True,
            ))
        try:
            clean_args = {k: v for k, v in args.items() if k != "reasoning"}
            result = await tool.func(**clean_args)
            return str(result)
        except Exception as e:
            return contract_dumps(error_envelope(
                "tool.execution_failed",
                f"Error executing {name}: {e}",
                source="tool.registry",
                details={"tool_name": name, "exception_type": type(e).__name__},
                flatten_details=True,
            ))

    def declarations(self, exclude: Optional[set] = None) -> list[dict]:
        """Export tools in Gemini function_declarations format.

        Automatically injects the ``reasoning`` property into every
        non-exempt tool so the LLM is forced to justify each call.

        Args:
            exclude: Optional set of tool names to omit from the list.
                     Excluded tools remain callable via ``execute()``
                     but won't appear in the LLM's schema.
        """
        _exclude = exclude or set()
        decls = []
        for t in self._tools.values():
            if t.name in _exclude:
                continue
            params = dict(t.parameters) if t.parameters else {"type": "object", "properties": {}}
            if t.name not in _REASONING_EXEMPT_TOOLS:
                # Deep-copy properties and inject reasoning
                props = dict(params.get("properties", {}))
                props.update(_REASONING_PROPERTY)
                params = {**params, "properties": props}
                # Add reasoning to required list
                required = list(params.get("required", []))
                if "reasoning" not in required:
                    required.append("reasoning")
                params["required"] = required
            decls.append({
                "name": t.name,
                "description": t.description,
                "parameters": params,
            })
        return decls

    def list_names(self) -> list[str]:
        return list(self._tools.keys())


# ── Global registry instance ──
registry = ToolRegistry()


# ═══════════════════════════════════════════════════════════════
#  Helper: run AppleScript
# ═══════════════════════════════════════════════════════════════

async def _osascript(script: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip()
        return f"AppleScript error: {err}"
    return stdout.decode("utf-8", errors="replace").strip()
