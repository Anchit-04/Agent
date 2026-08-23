"""
Phase 2 file tools for mini-agent.

Four tools that replace bash-only file access:
  - read_file    : read a file (optionally a line range), with line numbers
  - write_file   : create or fully overwrite a file
  - edit_file    : find-and-replace a unique string, exact match first,
                    whitespace-tolerant fallback second
  - search_files : grep-equivalent, implemented in pure Python so it behaves
                    the same on Windows as everywhere else

Import FILE_TOOLS (schemas) and FILE_TOOL_HANDLERS (name -> function) into
your agent loop alongside the existing bash tool.
"""

import os
import re
import json
from pathlib import Path

from paths import PROJECT_ROOT

WORKDIR = PROJECT_ROOT  # single source of truth — see paths.py

# Directories we never want to walk into during search_files — noisy and
# almost never what the agent actually wants to see.
SEARCH_IGNORE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}


class PathTraversalError(Exception):
    """Raised when a requested path resolves outside the WORKDIR sandbox."""


def _resolve(path: str) -> Path:
    """Resolve `path` relative to WORKDIR and verify it stays inside WORKDIR.
    Plain path-joining trusted the input — a '..' or an absolute path could
    escape the sandbox — so this resolves to absolute first, then checks containment."""
    root = Path(WORKDIR).resolve()
    candidate = (root / path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise PathTraversalError(
            f"Path '{path}' resolves outside the sandboxed working directory "
            f"({root}). Refusing to access it."
        )
    return candidate


# --- read_file ---------------------------------------------------------------

def read_file(path: str, offset: int = None, limit: int = None) -> str:
    """Read a file, optionally a line range, with 1-indexed line numbers."""
    try:
        p = _resolve(path)
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return json.dumps({"error": f"File not found: {path}"})
    except IsADirectoryError:
        return json.dumps({"error": f"{path} is a directory, not a file"})
    except PathTraversalError as e:
        return json.dumps({"error": str(e)})

    total = len(lines)
    start = (offset or 1) - 1
    end = start + limit if limit else total
    chunk = lines[start:end]

    numbered = "\n".join(f"{i + start + 1:>5}\t{line}" for i, line in enumerate(chunk))
    return json.dumps({"total_lines": total, "shown": f"{start + 1}-{min(end, total)}", "content": numbered})


# --- write_file ---------------------------------------------------------------

def write_file(path: str, content: str) -> str:
    """Create a new file or fully overwrite an existing one."""
    try:
        p = _resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return json.dumps({"success": True, "path": path, "bytes_written": len(content.encode("utf-8"))})
    except PathTraversalError as e:
        return json.dumps({"error": str(e)})
    except OSError as e:
        return json.dumps({"error": str(e)})


# --- edit_file ---------------------------------------------------------------

def _leading_whitespace(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _apply_indent_delta(new_lines: list, old_indent: str, matched_indent: str) -> list:
    """Shift the replacement's indentation by however much the matched block
    differed from old_str's, so a whitespace-tolerant match doesn't come out mis-indented."""
    delta = len(matched_indent) - len(old_indent)
    if delta == 0:
        return new_lines
    result = []
    for line in new_lines:
        if not line.strip():
            result.append(line)
        elif delta > 0:
            result.append(" " * delta + line)
        else:
            strip_count = min(-delta, len(line) - len(line.lstrip(" ")))
            result.append(line[strip_count:])
    return result


def edit_file(path: str, old_str: str, new_str: str) -> str:
    """Replace old_str with new_str. Tier 1: exact match, must be unique.
    Tier 2 (only if tier 1 finds nothing): whitespace-tolerant line match,
    still must be unique, re-indents the replacement to fit."""
    try:
        p = _resolve(path)
        content = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return json.dumps({"error": f"File not found: {path}"})
    except PathTraversalError as e:
        return json.dumps({"error": str(e)})

    # --- Tier 1: exact match ---
    exact_count = content.count(old_str)
    if exact_count == 1:
        new_content = content.replace(old_str, new_str, 1)
        p.write_text(new_content, encoding="utf-8")
        return json.dumps({"success": True, "match_type": "exact", "path": path})
    if exact_count > 1:
        return json.dumps({
            "error": f"old_str matched {exact_count} times — must be unique. "
                     "Include more surrounding context to disambiguate."
        })

    # --- Tier 2: whitespace-tolerant fallback (only reached if 0 exact matches) ---
    file_lines = content.splitlines()
    old_lines = old_str.splitlines()
    n = len(old_lines)
    norm = lambda s: s.strip()

    matches = [
        i for i in range(len(file_lines) - n + 1)
        if [norm(l) for l in file_lines[i:i + n]] == [norm(l) for l in old_lines]
    ]

    if not matches:
        return json.dumps({
            "error": "old_str not found, even with whitespace-tolerant matching. "
                     "Re-read the file — it may have changed, or old_str may not "
                     "match the actual content."
        })
    if len(matches) > 1:
        return json.dumps({
            "error": f"old_str matched {len(matches)} locations after whitespace "
                     "normalization — must be unique. Include more context."
        })

    start = matches[0]
    matched_indent = _leading_whitespace(file_lines[start])
    old_indent = _leading_whitespace(old_lines[0]) if old_lines else ""
    adjusted_new_lines = _apply_indent_delta(new_str.splitlines(), old_indent, matched_indent)

    new_file_lines = file_lines[:start] + adjusted_new_lines + file_lines[start + n:]
    p.write_text("\n".join(new_file_lines) + ("\n" if content.endswith("\n") else ""), encoding="utf-8")
    return json.dumps({
        "success": True,
        "match_type": "whitespace_tolerant",
        "path": path,
        "note": f"Matched at lines {start + 1}-{start + n} after ignoring whitespace differences.",
    })


# --- list_directory ---------------------------------------------------------

def list_directory(path: str = ".", max_depth: int = 2) -> str:
    """List files and folders under path, up to max_depth, skipping noisy dirs.
    Cross-platform, unlike shelling out to ls/dir/find."""
    try:
        root = _resolve(path)
    except PathTraversalError as e:
        return json.dumps({"error": str(e)})
    if not root.exists():
        return json.dumps({"error": f"Path not found: {path}"})
    if not root.is_dir():
        return json.dumps({"error": f"{path} is a file, not a directory"})

    entries = []
    root_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SEARCH_IGNORE_DIRS]
        depth = len(Path(dirpath).parts) - root_depth
        if depth >= max_depth:
            dirnames[:] = []  # don't descend further
            continue
        rel_dir = Path(dirpath).relative_to(root)
        for d in dirnames:
            entries.append(f"{rel_dir / d}/".lstrip("./"))
        for f in filenames:
            entries.append(str(rel_dir / f).lstrip("./"))

    entries.sort()
    return json.dumps({"path": path, "entries": entries, "count": len(entries)})


# --- search_files ---------------------------------------------------------------

def search_files(pattern: str, path: str = ".") -> str:
    """Regex search across text files under `path`, grep-style, cross-platform."""
    try:
        root = _resolve(path)
    except PathTraversalError as e:
        return json.dumps({"error": str(e)})
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return json.dumps({"error": f"Invalid regex: {e}"})

    results = []
    MAX_RESULTS = 200
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SEARCH_IGNORE_DIRS]
        for fname in filenames:
            fpath = Path(dirpath) / fname
            try:
                text = fpath.read_text(encoding="utf-8")
            except (UnicodeDecodeError, PermissionError):
                continue  # skip binaries / unreadable files
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    results.append(f"{fpath}:{lineno}: {line.strip()}")
                    if len(results) >= MAX_RESULTS:
                        break
            if len(results) >= MAX_RESULTS:
                break
        if len(results) >= MAX_RESULTS:
            break

    if not results:
        return json.dumps({"matches": [], "note": "No matches found."})
    truncated = len(results) >= MAX_RESULTS
    return json.dumps({"matches": results, "truncated": truncated})


# --- Tool schemas + dispatch table -------------------------------------------

FILE_TOOLS = [
    {
        "type": "function",
        "name": "read_file",
        "description": "Read a file's contents with line numbers. Use offset/limit for large files instead of reading the whole thing.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file, relative to the project root."},
                "offset": {"type": "integer", "description": "1-indexed line number to start reading from. Omit to start at line 1."},
                "limit": {"type": "integer", "description": "Max number of lines to return. Omit to read to end of file."},
            },
            "required": ["path"],
        },
    },
    {
        "type": "function",
        "name": "write_file",
        "description": "Create a new file or completely overwrite an existing one with the given content. Use edit_file instead if you only want to change part of an existing file.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file, relative to the project root."},
                "content": {"type": "string", "description": "The full content to write to the file."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "type": "function",
        "name": "edit_file",
        "description": (
            "Replace an exact snippet of text in a file with new text. old_str must "
            "be unique within the file — include enough surrounding context (a few "
            "lines) to disambiguate if the snippet could appear more than once. "
            "Prefer this over write_file for editing existing files."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file, relative to the project root."},
                "old_str": {"type": "string", "description": "The exact text to find and replace. Must match the file's current content."},
                "new_str": {"type": "string", "description": "The text to replace old_str with."},
            },
            "required": ["path", "old_str", "new_str"],
        },
    },
    {
        "type": "function",
        "name": "list_directory",
        "description": "List files and folders under a directory, up to a depth limit. Use this instead of bash ls/dir/find for exploring the project structure.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory to list. Defaults to the project root."},
                "max_depth": {"type": "integer", "description": "How many levels deep to descend. Defaults to 2."},
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "search_files",
        "description": "Search for a regex pattern across all text files under a directory (like grep -r). Returns matching file:line:content entries.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for."},
                "path": {"type": "string", "description": "Directory to search under. Defaults to the project root."},
            },
            "required": ["pattern"],
        },
    },
]

FILE_TOOL_HANDLERS = {
    "read_file": lambda args: read_file(args["path"], args.get("offset"), args.get("limit")),
    "write_file": lambda args: write_file(args["path"], args["content"]),
    "edit_file": lambda args: edit_file(args["path"], args["old_str"], args["new_str"]),
    "list_directory": lambda args: list_directory(args.get("path", "."), args.get("max_depth", 2)),
    "search_files": lambda args: search_files(args["pattern"], args.get("path", ".")),
}