"""
Two roots, deliberately kept apart.

FOX_HOME    — where Fox itself is installed. Its own config (.env,
              model_preferences.json) and session logs live here, so they
              follow the installation rather than whatever project is open.

workspace() — what the agent is allowed to read and write. Defaults to the
              directory you launched from, overridable per run. This is the
              root file_tools._resolve() enforces containment against.

Collapsing these into one constant is what used to pin Fox to its own source
tree: PROJECT_ROOT was computed from this file's location, and paths.py always
lives at <fox-repo>/loom/paths.py, so it resolved to the Fox repo no matter
where you launched from or what you passed on the command line.

Always read the workspace through workspace(), never by binding it at import:

    WORKDIR = workspace()      # WRONG — captures whatever was set at the
                               # moment this module was first imported, which
                               # is almost always before argv is parsed
    def f(): root = workspace()   # right

That mistake is silent. It doesn't raise; it just keeps using the old root.

.env deliberately stays on FOX_HOME. It's inside the agent's reach today only
because the workspace *is* the Fox repo — if it followed the workspace, an
agent running in any project could read the user's API keys, and search_files
would surface them.
"""

import os
from pathlib import Path

# Fox's own installation directory. Anchored to this file, which is the point.
FOX_HOME = Path(__file__).resolve().parent.parent

# Per-project Fox state lives under this directory name inside the workspace,
# so a project accumulates one predictable thing to gitignore rather than
# scattered files in its working tree.
STATE_DIR_NAME = ".fox"


class WorkspaceError(Exception):
    """The requested workspace can't be used. Raised at startup, never mid-run."""


def _initial_workspace() -> Path:
    """FOX_WORKSPACE if set, else the launch directory. Unvalidated — the cwd
    always exists, and an env var pointing somewhere bad should surface when
    it's used, not crash at import."""
    return Path(os.environ.get("FOX_WORKSPACE") or Path.cwd()).resolve()


_workspace: Path = _initial_workspace()


def workspace() -> Path:
    """The directory the agent is confined to. Call this; don't cache it."""
    return _workspace


def set_workspace(path: str | Path, allow_unsafe: bool = False) -> Path:
    """Validate and install the workspace. Call once at startup, before any
    agent runs. Fails loudly here so a bad path can't surface later as a
    mid-session tool error."""
    global _workspace
    resolved = Path(path).expanduser().resolve()

    if not resolved.exists():
        raise WorkspaceError(f"Workspace does not exist: {resolved}")
    if not resolved.is_dir():
        raise WorkspaceError(f"Workspace is not a directory: {resolved}")
    if not allow_unsafe:
        _reject_dangerous(resolved)

    _workspace = resolved
    return resolved


def _reject_dangerous(path: Path) -> None:
    """Refuse roots whose blast radius is the whole machine. _resolve() would
    happily permit anything underneath these, so the containment check offers
    no protection at all when the root is this wide."""
    if path == Path(path.anchor):
        raise WorkspaceError(
            f"Refusing to use a drive/filesystem root as the workspace ({path}). "
            "Point Fox at a specific project directory, or pass "
            "--allow-unsafe-workspace if you really mean it."
        )
    if path == Path.home().resolve():
        raise WorkspaceError(
            f"Refusing to use your home directory as the workspace ({path}) — "
            "it contains .ssh, .aws and every other project. Point Fox at a "
            "specific project, or pass --allow-unsafe-workspace."
        )


def state_dir() -> Path:
    """Per-project Fox state, inside the project. Not created here — callers
    that write make it, so merely reading a workspace never touches it."""
    return workspace() / STATE_DIR_NAME


def shared_memory_file() -> Path:
    """This project's shared memory log. Per-workspace, so two projects never
    share a log."""
    return state_dir() / "SHARED_MEMORY.md"
