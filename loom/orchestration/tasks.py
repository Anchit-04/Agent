
import itertools
from dataclasses import dataclass
from enum import Enum


class Lane(Enum):
    READ = "read"     # never modifies anything; runs in parallel with other readers
    WRITE = "write"   # may modify; needs the single writer slot


class TaskStatus(Enum):
    QUEUED = "queued"
    RUNNING = "running"
    NEEDS_APPROVAL = "needs-approval"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ESCALATED = "escalated"


# The state machine. Key = where a task is, value = where it may go next.
# Terminal states map to the empty set: once there, a task never moves again.
# Phase 3 adds VERIFYING and MERGED — that's new rows here, nothing else.
TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.QUEUED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset({
        TaskStatus.NEEDS_APPROVAL,
        TaskStatus.DONE,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.ESCALATED,
    }),
    TaskStatus.NEEDS_APPROVAL: frozenset({TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.CANCELLED}),
    TaskStatus.DONE: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
    TaskStatus.ESCALATED: frozenset(),
}

_NEEDS_ERROR = frozenset({TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.ESCALATED})


class InvalidTransition(Exception):
    """A status change the state machine doesn't allow, or one missing the
    report/error it requires. Always a bug in the caller, never a model mistake."""


class UnknownTask(KeyError):
    """A task id this run never issued — usually a model mistyping one."""


@dataclass
class Report:
    outcome: str       
    changes: list[str]   
    validation: str       
    blockers: list[str]   


@dataclass
class Task:
    id: str              
    description: str
    lane: Lane
    scope: list[str]             
    model_key: str | None = None 
    status: TaskStatus = TaskStatus.QUEUED
    report: Report | None = None
    error: str | None = None   

    @property
    def is_terminal(self) -> bool:
        return not TRANSITIONS[self.status]

    def transition(self, to: TaskStatus, *, report: Report | None = None,
                   error: str | None = None) -> None:
        if to not in TRANSITIONS[self.status]:
            raise InvalidTransition(f"task {self.id}: {self.status.value} -> {to.value} is not allowed")
        if to is TaskStatus.DONE and report is None:
            raise InvalidTransition(f"task {self.id}: cannot be done without a completion report")
        if to in _NEEDS_ERROR and not error:
            raise InvalidTransition(f"task {self.id}: {to.value} needs an error saying why")

        self.status = to
        if report is not None:
            self.report = report
        if error is not None:
            self.error = error


class TaskRegistry:
    """One per orchestrator run. Hands out ids and looks tasks up.

    Tasks are only created from the orchestrator's own thread, between
    batches, so the counter needs no lock."""

    def __init__(self):
        self._tasks: dict[str, Task] = {}
        self._ids = itertools.count(1)

    def create(self, description: str, lane: Lane, scope: list[str],
               model_key: str | None = None) -> Task:
        task = Task(id=f"t{next(self._ids)}", description=description, lane=lane,
                    scope=list(scope), model_key=model_key)
        self._tasks[task.id] = task
        return task

    def get(self, task_id: str) -> Task:
        """Raises UnknownTask rather than returning None — a mistyped id is an
        error the model gets to see, not an empty result it builds on."""
        try:
            return self._tasks[task_id]
        except KeyError:
            raise UnknownTask(task_id) from None

    def all(self) -> list[Task]:
        """Every task, in creation order."""
        return list(self._tasks.values())
