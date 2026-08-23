"""
Single source of truth for the project root the agent is sandboxed to.
Computed from this file's own location, not the invocation cwd — otherwise
running `python agent.py` from a different directory silently shifts every
relative path (loom/paths.py -> loom/ -> repo root).
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
