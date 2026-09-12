
import asyncio
import json
import threading
import uuid
from dataclasses import dataclass, field, asdict

import websockets

import agent
import orchestrator
import memory
from config import routing, vault
from injection import InjectionQueue
from approval import ApprovalGate
import permissions
import paths
from paths import FOX_HOME

# FOX_HOME: session transcripts are Fox's own record, not project files,
# and session ids are unique so one directory serves every workspace.
SESSIONS_DIR = FOX_HOME / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)


@dataclass
class SessionState:
    session_id: str
    task: str
    model_key: str | None = None
    status: str = "running"  
    final_text: str = ""
    todos: str = "" 
    tasks: dict = field(default_factory=dict)  
    task_statuses: dict = field(default_factory=dict)
    turns: list = field(default_factory=list)  


class Session:
    def __init__(self, session_id: str, task: str):
        self.session_id = session_id
        self.state = SessionState(session_id=session_id, task=task)
        self.clients: set = set()
        self.injection_queue = InjectionQueue()
        self._lock = threading.Lock()
        self.loop: asyncio.AbstractEventLoop | None = None
        # Emits through handle_event, so an approval request is persisted and
        # broadcast like any other event — a client that attaches mid-wait can
        # replay it from the session log instead of hanging with no context.
        self.approval_gate = ApprovalGate(emit=self.handle_event)
        # Set by _start_session. Kept on the session so a side conversation can
        # share it — that sharing is what "inherits the summary, not the
        # transcript" means in practice.
        self.mem: memory.Memory | None = None

    def persist(self) -> None:
        path = SESSIONS_DIR / f"{self.session_id}.json"
        with self._lock:
            path.write_text(json.dumps(asdict(self.state), indent=2), encoding="utf-8")

    def handle_event(self, event_type: str, payload: dict) -> None:
        with self._lock:
            if event_type == "agent_turn" and payload.get("task_id") is None:
                self.state.model_key = payload.get("model_key")
            if event_type == "todo_updated" and payload.get("task_id") is None:
                self.state.todos = payload.get("checklist", "")
            if event_type == "task_created":
                self.state.tasks[payload["task_id"]] = {
                    "description": payload.get("description", ""),
                    "scope": payload.get("scope", []),
                    "depends_on": payload.get("depends_on", []),
                }
            if event_type == "task_status_changed" and payload.get("task_id") is not None:
                self.state.task_statuses[payload["task_id"]] = payload.get("status")
            self.state.turns.append({"type": event_type, "payload": payload})
        self.persist() 
        self._broadcast(event_type, payload)

    def _broadcast(self, event_type: str, payload: dict) -> None:
        if self.loop is None:
            return
        message = json.dumps({"session_id": self.session_id, "type": event_type, **payload})
        for ws in list(self.clients):
            asyncio.run_coroutine_threadsafe(_safe_send(ws, message), self.loop)


async def _safe_send(ws, message: str) -> None:
    try:
        await ws.send(message)
    except Exception:
        pass  


SESSIONS: dict[str, Session] = {}
SESSIONS_LOCK = threading.Lock()


def _side_conversation_task(question: str, parent: Session) -> str:
    """Wrap a side question in what the main thread has established.

    Deliberately not the parent's transcript: that would be resent in full on
    every turn of the side conversation, which free-tier token budgets can't
    absorb. The todo list and the memory index are already externalised
    summaries of the same progress, so they carry the useful part cheaply.
    The shared Memory instance goes along too, so memory_read can pull a full
    entry on demand rather than everything being force-fed up front."""
    parts = ["[Side conversation. The main task is still running — you are not working on it.",
             "Answer the question; don't continue the main task unless asked.]", ""]
    with parent._lock:
        main_task, todos = parent.state.task, parent.state.todos
    parts.append(f"Main task: {main_task}")
    if todos:
        parts.append(f"\nProgress so far:\n{todos}")
    if parent.mem is not None:
        index = parent.mem.read_index()
        if index and index != "(empty)":
            parts.append(f"\nShared memory index (pull a full entry with memory_read):\n{index}")
    parts.append(f"\nQuestion: {question}")
    return "\n".join(parts)


def _start_session(task: str, model_key: str | None, mode: str, loop: asyncio.AbstractEventLoop,
                   permission_mode: str = permissions.WORKSPACE_WRITE,
                   parent: "Session | None" = None) -> Session:
    """mode="agent" (default) runs a single agent with real bash/file tools —
    a normal coding-harness session, one model, no delegation. mode=
    "orchestrator" is the Phase 5 multi-agent path: plans and delegates,
    never touches files/bash directly itself. Both emit the same event
    shapes, so the rest of Session doesn't need to know which one is running."""
    session_id = str(uuid.uuid4())
    if parent is not None:
        task = _side_conversation_task(task, parent)
    session = Session(session_id, task)
    session.loop = loop
    with SESSIONS_LOCK:
        SESSIONS[session_id] = session

    # A side conversation shares its parent's memory log rather than opening a
    # fresh one, so notes written on either side are visible to both.
    mem = parent.mem if (parent is not None and parent.mem is not None) \
        else memory.Memory(SESSIONS_DIR / session_id / "SHARED_MEMORY.md")
    session.mem = mem

    def run():
        try:
            if mode == "orchestrator":
                final = orchestrator.run_orchestrator(
                    task, verbose=False, model_key=model_key,
                    event_sink=session.handle_event, mem=mem,
                    injection_queue=session.injection_queue,
                    approver=session.approval_gate, mode=permission_mode,
                )
            else:
                # No routing.pick_orchestrator() strong-tier requirement here —
                # a single chatting agent works with whatever key is present.
                chosen_model = model_key or routing.pick_executor()
                # interactive=False only means "don't prompt on stdin" — the
                # explicit approver is what actually reaches the human, via the
                # TUI. Without it this would fall back to DenyApprover and
                # refuse every escalation with nobody ever asked.
                final = agent.run_agent(
                    task, verbose=False, model_key=chosen_model, interactive=False,
                    task_id=None, event_sink=session.handle_event, mem=mem,
                    injection_queue=session.injection_queue, keep_alive=True,
                    approver=session.approval_gate, mode=permission_mode,
                )
            with session._lock:
                session.state.final_text = final
                session.state.status = "done"
        except Exception as e:
            with session._lock:
                session.state.status = "failed"
                session.state.final_text = str(e)
        # Releases any executor thread still parked on an unanswered approval.
        # Without this they'd hold the process open after the session ended.
        session.approval_gate.close()
        session.persist()
        session._broadcast("session_ended", {
            "task_id": None, "status": session.state.status, "final_text": session.state.final_text,
        })

    threading.Thread(target=run, daemon=True).start()
    return session


def _validate_all_keys() -> None:
    """Force a real auth check against every provider that has a key set.
    Blocking network I/O — always call this via asyncio.to_thread, never
    straight from the handler, or one slow provider stalls every session
    sharing this event loop."""
    for entry in vault.list_models():
        if entry.present:
            vault.validate_key(entry.model_key, force=True)


async def handler(ws) -> None:
    # A set, not one session: a client with threads open holds several at
    # once. Tracking only the latest leaked every earlier one's client
    # registration on disconnect, so the server kept broadcasting to a
    # socket that was gone.
    attached: set[Session] = set()
    async for raw in ws:
        msg = json.loads(raw)
        msg_type = msg.get("type")

        if msg_type == "start_session":
            # "mode" picks the runner (agent vs orchestrator); "permission_mode"
            # is the separate sandbox axis (read-only / workspace-write /
            # full-access). Different concepts, unfortunately similar names.
            permission_mode = msg.get("permission_mode") or permissions.WORKSPACE_WRITE
            if permission_mode not in permissions.MODES:
                await ws.send(json.dumps({
                    "type": "error",
                    "message": f"unknown permission_mode {permission_mode!r}; expected one of {list(permissions.MODES)}",
                }))
                continue
            # parent_session_id marks this as a side conversation (a "thread"
            # in the TUI): its own agent and history, but seeded with the
            # parent's progress and sharing the parent's memory log.
            parent = None
            if msg.get("parent_session_id"):
                with SESSIONS_LOCK:
                    parent = SESSIONS.get(msg["parent_session_id"])
                if parent is None:
                    await ws.send(json.dumps({
                        "type": "error",
                        "message": f"unknown parent_session_id {msg['parent_session_id']!r}",
                    }))
                    continue
            session = _start_session(
                msg["task"], msg.get("model_key"), msg.get("mode", "agent"),
                asyncio.get_running_loop(), permission_mode=permission_mode, parent=parent,
            )
            session.clients.add(ws)
            attached.add(session)
            await ws.send(json.dumps({"type": "session_started", "session_id": session.session_id}))

        elif msg_type == "attach_session":
            with SESSIONS_LOCK:
                session = SESSIONS.get(msg["session_id"])
            if session is None:
                await ws.send(json.dumps({"type": "error", "message": f"unknown session_id {msg['session_id']!r}"}))
                continue
            session.clients.add(ws)
            attached.add(session)
            await ws.send(json.dumps({"type": "attached", "session_id": session.session_id, "state": asdict(session.state)}))

        elif msg_type == "human_message":
            with SESSIONS_LOCK:
                session = SESSIONS.get(msg.get("session_id"))
            if session is None:
                await ws.send(json.dumps({"type": "error", "message": f"unknown session_id {msg.get('session_id')!r}"}))
                continue
            session.injection_queue.push(msg.get("task_id"), msg["content"])
            await ws.send(json.dumps({"type": "human_message_queued", "session_id": session.session_id, "task_id": msg.get("task_id")}))

        elif msg_type == "end_session":
            # Explicit, deliberate end — never triggered by a disconnect.
            # A session with nobody attached just sits blocked, waiting,
            # for as long as this server process keeps running.
            with SESSIONS_LOCK:
                session = SESSIONS.get(msg.get("session_id"))
            if session is None:
                await ws.send(json.dumps({"type": "error", "message": f"unknown session_id {msg.get('session_id')!r}"}))
                continue
            session.injection_queue.close(msg.get("task_id"))
            # Also wake anything parked on an approval. A thread blocked in
            # approve() never returns to the agent loop, so closing only the
            # injection queue would leave it waiting forever for an answer
            # from a user who has just left.
            session.approval_gate.close()
            await ws.send(json.dumps({"type": "end_session_queued", "session_id": session.session_id}))

        elif msg_type == "approval_response":
            with SESSIONS_LOCK:
                session = SESSIONS.get(msg.get("session_id"))
            if session is None:
                await ws.send(json.dumps({"type": "error", "message": f"unknown session_id {msg.get('session_id')!r}"}))
                continue
            # Non-blocking: respond() only sets the answer and notifies. The
            # agent thread parked in approve() wakes on its own.
            session.approval_gate.respond(
                msg["request_id"],
                bool(msg.get("approved")),
                always=bool(msg.get("always")),
                tool_name=msg.get("tool_name"),
            )

        elif msg_type == "list_models":
            # The vault is process-global, not per-session, so this is
            # deliberately answerable without an attached session — the TUI
            # can open the Vault panel before any agent is running.
            # validate=True re-authenticates every present key for real;
            # without it, `validated` is whatever the per-session cache holds
            # (null = never checked), which is cheap but not proof.
            if msg.get("validate"):
                await asyncio.to_thread(_validate_all_keys)
            await ws.send(json.dumps({
                "type": "models",
                "models": [asdict(e) for e in vault.list_models()],
            }))

        elif msg_type == "set_model_key":
            # vault.set_key writes .env via python-dotenv (preserving the rest
            # of the file), updates os.environ so it's live without a restart,
            # and drops any cached validation for that key. We then validate
            # for real so the UI can say "saved and works" rather than just
            # "saved" — the distinction that keeps costing us debug cycles.
            try:
                vault.set_key(msg["model_key"], msg["value"])
                await asyncio.to_thread(vault.validate_key, msg["model_key"], True)
            except Exception as e:
                await ws.send(json.dumps({"type": "error", "message": f"could not save key: {e}"}))
                continue
            await ws.send(json.dumps({
                "type": "models",
                "models": [asdict(e) for e in vault.list_models()],
            }))

        else:
            await ws.send(json.dumps({"type": "error", "message": f"unknown message type {msg_type!r}"}))

    for session in attached:
        session.clients.discard(ws)


async def main(host: str = "localhost", port: int = 8765) -> None:
    async with websockets.serve(handler, host, port):
        print(f"server.py listening on ws://{host}:{port}")
        await asyncio.Future()

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fox session server.")
    parser.add_argument("--workspace", default=None,
                        help="Directory the agent may read and write. Defaults to the "
                             "current directory.")
    parser.add_argument("--allow-unsafe-workspace", action="store_true",
                        help="Permit a drive root or your home directory as the workspace.")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    try:
        ws_root = paths.set_workspace(args.workspace or paths.workspace(),
                                      allow_unsafe=args.allow_unsafe_workspace)
    except paths.WorkspaceError as e:
        raise SystemExit(f"error: {e}")

    # Printed prominently because it's inferred from cwd when not passed, and
    # launching from the wrong directory is otherwise invisible until the agent
    # writes somewhere surprising.
    print(f"workspace: {ws_root}")
    print(f"fox home:  {paths.FOX_HOME}")
    asyncio.run(main(args.host, args.port))
