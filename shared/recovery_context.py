"""Invocation provenance and durable effect boundary for recovery-enabled tasks."""

from contextvars import ContextVar
from dataclasses import dataclass, field
import uuid

from shared.recovery_store import RecoveryStore

current_execution = ContextVar("durable_task_execution", default=None)


def check_owner(task_id=None, *, allow_stopped=False):
    context = current_execution.get()
    if context and (task_id is None or context.owner.task_id == task_id):
        from shared.task_control import check_invocation
        check_invocation()
        context.store.check(context.owner, allow_stopped=allow_stopped)


@dataclass
class ExecutionContext:
    store: RecoveryStore
    owner: object
    state: object
    invocation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    messages: list = field(default_factory=list)
    call_id: str = ""
    role: str = "executor"
    round_seq: int = 0
    basis: str = ""
    operation_ids: list = field(default_factory=list)
    local_write: object = None
    sensitive: bool = False
    fact_versions: dict = field(default_factory=dict)
    compound_plan: list = field(default_factory=list)

    def admit_facts(self):
        identities={item["previous_operation_id"] for item in self.state.recovery.get("operations",[])}
        identities.update(row["payload"]["operation_id"] for row in self.store.db.list_agent_records(self.owner.task_id,"recovery_fact")[-4:])
        self.fact_versions={}
        for identity in identities:
            row=self.store.db._conn.execute("SELECT operation_version FROM task_operations WHERE operation_id=?",(identity,)).fetchone()
            if row:
                self.fact_versions[identity]=row[0]

    def check_decision(self):
        for identity,version in self.fact_versions.items():
            row=self.store.db._conn.execute("SELECT operation_version FROM task_operations WHERE operation_id=?",(identity,)).fetchone()
            if row is not None and row[0]!=version:
                from shared.task_control import TaskSteered
                raise TaskSteered()

    def decision(self, messages, call_id, invocation_id, round_seq, role):
        check_owner()
        self.store.check(self.owner)
        self.messages = list(messages)
        self.call_id, self.invocation_id = call_id, invocation_id
        self.round_seq, self.role = round_seq, str(getattr(role, "value", role))

    def prepare_action(self, action):
        check_owner()
        self.check_decision()
        operation_id = uuid.uuid4().hex
        parameters = action.model_dump(mode="json")
        if self.sensitive and parameters.get("text"):
            parameters["text"] = "[REDACTED]"
        messages_ref = self.store.artifacts.save_json("recovery_dialogue",self.messages) if self.store.artifacts else None
        with self.store.db.transaction():
            if self.local_write is not None:
                self.local_write()
            self.store.prepare(
                self.owner, self.state, operation_id=operation_id, kind="device_action",
                args=parameters, invocation_id=self.invocation_id,
                call_id=self.call_id, basis=self.basis, units=0 if action.type == "sleep" else 1,
                parent_id=self.call_id or self.invocation_id, ordinal=len(self.operation_ids),
                intent={"messages_ref": messages_ref, "role": self.role, "round_seq": self.round_seq,
                        "compound_plan":self.compound_plan},
            )
        self.local_write = None
        self.operation_ids.append(operation_id)
        return operation_id

    def flush_local_write(self):
        if self.local_write is not None:
            with self.store.db.transaction():
                self.local_write()
                self.store.publish_manifest(self.owner.task_id,state=self.state)
            self.local_write = None


class DurableDriver:
    """Delegate observations unchanged; persist T1/T2 around actual act only."""

    def __init__(self, driver, context, journal):
        self._driver, self._context, self._journal = driver, context, journal

    def __getattr__(self, name):
        return getattr(self._driver, name)

    async def act(self, action):
        context = current_execution.get() or self._context
        operation_id = context.prepare_action(action)
        envelope = {"operation_id": operation_id, "task_id": context.owner.task_id,
                    "epoch": context.owner.epoch, "device_fence": context.owner.device_fence,
                    "device_key": context.owner.device_key, "action": action.model_dump(mode="json"),
                    "sensitive":context.sensitive}
        remote = getattr(self._driver, "act_durable", None)
        if remote:
            result = await remote(envelope)
        else:
            result = await self._journal.act(envelope, lambda: self._driver.act(action))
        if result.detail.get("execution_result")=="unknown":
            context.store.record_unknown(operation_id,evidence={"status":"unknown_from_driver"})
            from shared.recovery_runtime import InfrastructureInterrupted
            raise InfrastructureInterrupted("driver_returned_unknown_execution")
        else:
            context.store.receipt(operation_id, result.model_dump(mode="json"),
                                  generation=result.detail.get("driver_generation",self._journal.generation))
        return result
