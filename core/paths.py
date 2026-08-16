"""
Single source of truth for the project root the agent is sandboxed to.

Before this, both file_tools.py and sandbox_client.py each defined their own
WORKDIR = "." — which resolves relative to whatever directory the OS process
happened to be launched from, not a fixed location. Run `python agent.py`
from core/ instead of the repo root and every file path silently shifts by
one directory: read_file('PLAN.md') looks for core/PLAN.md, doesn't find it,
and the model burns turns guessing paths instead of getting a real answer.

PROJECT_ROOT is computed from this file's own location instead, so it's the
same regardless of the invocation cwd: core/paths.py -> core/ -> repo root.
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
