
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


def _start_session(task: str, model_key: str | None, mode: str, loop: asyncio.AbstractEventLoop) -> Session:
    """mode="agent" (default) runs a single agent with real bash/file tools —
    a normal coding-harness session, one model, no delegation. mode=
    "orchestrator" is the Phase 5 multi-agent path: plans and delegates,
    never touches files/bash directly itself. Both emit the same event
    shapes, so the rest of Session doesn't need to know which one is running."""
    session_id = str(uuid.uuid4())
    session = Session(session_id, task)
    session.loop = loop
    with SESSIONS_LOCK:
        SESSIONS[session_id] = session

    mem = memory.Memory(SESSIONS_DIR / session_id / "SHARED_MEMORY.md")

    def run():
        try:
            if mode == "orchestrator":
                final = orchestrator.run_orchestrator(
                    task, verbose=False, model_key=model_key,
                    event_sink=session.handle_event, mem=mem,
                    injection_queue=session.injection_queue,
                )
            else:
                # No routing.pick_orchestrator() strong-tier requirement here —
                # a single chatting agent works with whatever key is present.
                chosen_model = model_key or routing.pick_executor()
                final = agent.run_agent(
                    task, verbose=False, model_key=chosen_model, interactive=False,
                    task_id=None, event_sink=session.handle_event, mem=mem,
                    injection_queue=session.injection_queue, keep_alive=True,
                )
            with session._lock:
                session.state.final_text = final
                session.state.status = "done"
        except Exception as e:
            with session._lock:
                session.state.status = "failed"
                session.state.final_text = str(e)
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
    attached: Session | None = None
    async for raw in ws:
        msg = json.loads(raw)
        msg_type = msg.get("type")

        if msg_type == "start_session":
            session = _start_session(
                msg["task"], msg.get("model_key"), msg.get("mode", "agent"), asyncio.get_running_loop()
            )
            session.clients.add(ws)
            attached = session
            await ws.send(json.dumps({"type": "session_started", "session_id": session.session_id}))

        elif msg_type == "attach_session":
            with SESSIONS_LOCK:
                session = SESSIONS.get(msg["session_id"])
            if session is None:
                await ws.send(json.dumps({"type": "error", "message": f"unknown session_id {msg['session_id']!r}"}))
                continue
            session.clients.add(ws)
            attached = session
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
            await ws.send(json.dumps({"type": "end_session_queued", "session_id": session.session_id}))

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

    if attached is not None:
        attached.clients.discard(ws)


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
