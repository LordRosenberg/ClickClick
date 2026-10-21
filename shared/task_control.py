"""Invocation fencing; the disabled path does not install a lease or waiter."""

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
import uuid


class TaskSteered(BaseException):
    """An obsolete cognitive invocation must yield without failing the task."""


@dataclass
class InvocationLease:
    revision: int
    generation: int
    revoked: bool = False
    dispatching: bool = False
    identity: str = field(default_factory=lambda: uuid.uuid4().hex)

    def check(self):
        if self.revoked:
            raise TaskSteered()


current_lease: ContextVar[InvocationLease | None] = ContextVar("task_invocation_lease", default=None)


def check_invocation():
    lease = current_lease.get()
    if lease is not None:
        lease.check()


def begin_dispatch():
    lease = current_lease.get()
    if lease is not None:
        lease.check()
        # No await between checking ownership and acquiring transaction ownership.
        lease.dispatching = True


class SteeringControl:
    def __init__(self):
        self.changed = asyncio.Event()
        self.lease = None
        self.generation = 0

    def notify(self):
        if self.lease is not None and not self.lease.dispatching:
            self.lease.revoked = True
        self.changed.set()

    def acquire(self, revision):
        self.changed.clear()
        self.generation += 1
        self.lease = InvocationLease(revision, self.generation)
        return self.lease
