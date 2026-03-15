"""
Moonwalk — File I/O Tools
===========================
Tools for reading, writing, listing, and editing files
on the user's Mac.
"""

import os

from tools.contracts import error_envelope, dumps as contract_dumps
from tools.registry import registry


# ═══════════════════════════════════════════════════════════════
#  Path Security
# ═══════════════════════════════════════════════════════════════

# Directories that file tools should never read or write.
# Prevents the agent from accessing SSH keys, cloud credentials, etc.
_RESTRICTED_PATHS = [
    os.path.expanduser("~/.ssh"),
    os.path.expanduser("~/.gnupg"),
    os.path.expanduser("~/.aws"),
    os.path.expanduser("~/.kube"),
    os.path.expanduser("~/.config/gcloud"),
    "/etc/shadow",
    "/etc/sudoers",
    "/System",
    "/private/var",
]


def _is_path_restricted(expanded_path: str) -> bool:
    """Check if the resolved path falls within a restricted directory."""
    try:
        real = os.path.realpath(expanded_path)
    except Exception:
        real = expanded_path
    for prefix in _RESTRICTED_PATHS:
        if real.startswith(prefix):
            return True
    return False


# ── 14. read_file ──
@registry.register(
    name="read_file",
    description=(
        "Read the contents of a text file on the user's Mac. Supports pagination "
        "for large files via offset/max_chars, and optional line numbers."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file (e.g. '~/Desktop/notes.txt', '/Users/john/config.json')"
            },
            "offset": {
                "type": "integer",
                "description": "Character offset to start reading from (default 0)"
            },
            "max_chars": {
                "type": "integer",
                "description": "Maximum number of characters to return (default 12000, max 50000)"
            },
            "include_line_numbers": {
                "type": "boolean",
                "description": "If true, prefix returned lines with line numbers"
            }
        },
        "required": ["path"]
    }
)
async def read_file(path: str, offset: int = 0, max_chars: int = 12000, include_line_numbers: bool = False) -> str:
    expanded = os.path.expanduser(path)
    if _is_path_restricted(expanded):
        return contract_dumps(error_envelope("file.restricted", f"Access denied — '{path}' is in a restricted directory."))
    offset = max(0, int(offset))
    max_chars = max(1, min(50000, int(max_chars)))
    if not os.path.exists(expanded):
        return contract_dumps(error_envelope("file.not_found", f"File not found: {expanded}"))
    if os.path.isdir(expanded):
        return contract_dumps(error_envelope("file.is_directory", f"'{expanded}' is a directory, not a file. Use list_directory instead."))
    try:
        with open(expanded, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        size = os.path.getsize(expanded)
        if offset > len(content):
            return contract_dumps(error_envelope("file.invalid_offset", f"offset {offset} is beyond end of file ({len(content)} chars)."))

        window = content[offset: offset + max_chars]
        truncated = " (truncated)" if (offset + len(window)) < len(content) else ""
        header = (
            f"[{os.path.basename(expanded)}, {size} bytes, offset {offset}, "
            f"returned {len(window)} chars{truncated}]"
        )

        if not include_line_numbers:
            return f"{header}\n{window}"

        base_line = content.count("\n", 0, offset) + 1
        numbered_lines = []
        for index, line in enumerate(window.splitlines(), start=base_line):
            numbered_lines.append(f"{index:>5}: {line}")
        numbered = "\n".join(numbered_lines)
        return f"{header}\n{numbered}"
    except Exception as e:
        return contract_dumps(error_envelope("file.read_error", str(e)[:200]))


# ── 15. write_file ──
@registry.register(
    name="write_file",
    description="Create or overwrite a file with the given content. Parent directories are created automatically. Use for: creating scripts, notes, config files, code, or any text file.",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to write (e.g. '~/Desktop/hello.py', '~/notes.txt')"
            },
            "content": {
                "type": "string",
                "description": "The full text content to write to the file"
            }
        },
        "required": ["path", "content"]
    }
)
async def write_file(path: str, content: str) -> str:
    expanded = os.path.expanduser(path)
    if _is_path_restricted(expanded):
        return contract_dumps(error_envelope("file.restricted", f"Access denied — '{path}' is in a restricted directory."))
    try:
        parent_dir = os.path.dirname(expanded)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        with open(expanded, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Wrote {len(content)} bytes to {expanded}"
    except Exception as e:
        return contract_dumps(error_envelope("file.write_error", str(e)[:200]))


# ── 23. list_directory ──
@registry.register(
    name="list_directory",
    description="List the contents of a directory. Returns a JSON-like tree of files and folders (up to 300 entries). Use this instead of 'ls' shell commands for safe, structured exploration.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute or relative path to the directory (e.g. '~/Desktop', './src')"}
        },
        "required": ["path"]
    }
)
async def list_directory(path: str) -> str:
    try:
        full_path = os.path.expanduser(path)
        if _is_path_restricted(full_path):
            return contract_dumps(error_envelope("file.restricted", f"Access denied — '{path}' is in a restricted directory."))
        if not os.path.isdir(full_path):
            return contract_dumps(error_envelope("file.not_directory", f"'{full_path}' is not a valid directory."))
        
        items: list[dict | str] = []
        for i, entry in enumerate(os.scandir(full_path)):
            if i > 300:
                items.append("... [truncated, too many files]")
                break
            items.append({
                "name": entry.name,
                "is_dir": entry.is_dir(),
                "size_bytes": entry.stat().st_size if not entry.is_dir() else 0
            })
            
        # Format cleanly
        out = f"Directory contents of '{full_path}':\n"
        for item in items:
            if isinstance(item, str):
                out += item + "\n"
            else:
                icon = "📁" if item["is_dir"] else "📄"
                size = f"({item['size_bytes']} bytes)" if not item["is_dir"] else ""
                out += f"{icon} {item['name']} {size}\n"
        return out
    except Exception as e:
        return contract_dumps(error_envelope("file.list_error", str(e)[:200]))


# ── 24. replace_in_file ──
@registry.register(
    name="replace_in_file",
    description="Surgically replace a specific block of text in a file. Must provide the EXACT old text including indentation. Far safer and faster than rewriting the entire file with write_file.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path to the file"},
            "old_text": {"type": "string", "description": "The exact existing text to find and replace"},
            "new_text": {"type": "string", "description": "The new text to insert in its place"}
        },
        "required": ["path", "old_text", "new_text"]
    }
)
async def replace_in_file(path: str, old_text: str, new_text: str) -> str:
    try:
        full_path = os.path.expanduser(path)
        if _is_path_restricted(full_path):
            return contract_dumps(error_envelope("file.restricted", f"Access denied — '{path}' is in a restricted directory."))
        if not os.path.isfile(full_path):
            return contract_dumps(error_envelope("file.not_found", f"File not found at '{full_path}'"))
            
        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()
            
        if old_text not in content:
            return contract_dumps(error_envelope("file.text_not_found", "old_text not found in the file. Ensure indentation and line breaks match exactly."))
            
        occurrences = content.count(old_text)
        new_content = content.replace(old_text, new_text)
        
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(new_content)
            
        return f"Successfully replaced {occurrences} occurrence(s) in {full_path}"
    except Exception as e:
        return contract_dumps(error_envelope("file.modify_error", str(e)[:200]))
