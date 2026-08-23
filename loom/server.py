
import asyncio
import json
import threading
import uuid
from dataclasses import dataclass, field, asdict

import websockets

import orchestrator
import memory
from injection import InjectionQueue
from paths import PROJECT_ROOT

SESSIONS_DIR = PROJECT_ROOT / "sessions"
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


def _start_session(task: str, model_key: str | None, loop: asyncio.AbstractEventLoop) -> Session:
    session_id = str(uuid.uuid4())
    session = Session(session_id, task)
    session.loop = loop
    with SESSIONS_LOCK:
        SESSIONS[session_id] = session

    mem = memory.Memory(SESSIONS_DIR / session_id / "SHARED_MEMORY.md")

    def run():
        try:
            final = orchestrator.run_orchestrator(
                task, verbose=False, model_key=model_key,
                event_sink=session.handle_event, mem=mem,
                injection_queue=session.injection_queue,
            )
            with session._lock:
                session.state.final_text = final
                session.state.status = "done"
        except Exception as e:
            with session._lock:
                session.state.status = "failed"
                session.state.final_text = str(e)
        session.persist()
        session._broadcast("session_ended", {"task_id": None, "status": session.state.status})

    threading.Thread(target=run, daemon=True).start()
    return session


async def handler(ws) -> None:
    attached: Session | None = None
    async for raw in ws:
        msg = json.loads(raw)
        msg_type = msg.get("type")

        if msg_type == "start_session":
            session = _start_session(msg["task"], msg.get("model_key"), asyncio.get_running_loop())
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

        else:
            await ws.send(json.dumps({"type": "error", "message": f"unknown message type {msg_type!r}"}))

    if attached is not None:
        attached.clients.discard(ws)


async def main(host: str = "localhost", port: int = 8765) -> None:
    async with websockets.serve(handler, host, port):
        print(f"server.py listening on ws://{host}:{port}")
        await asyncio.Future()  

if __name__ == "__main__":
    asyncio.run(main())
