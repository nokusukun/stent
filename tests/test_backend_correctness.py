import asyncio
from datetime import datetime, timedelta
from typing import List

import pytest

from stent.backend.sqlite import SQLiteBackend
from stent.core import ExecutionRecord, TaskRecord, RetryPolicy
from tests.utils import cleanup_test_backend


def _make_execution(exec_id: str) -> ExecutionRecord:
    now = datetime.now()
    return ExecutionRecord(
        id=exec_id,
        root_function="tests.durable",
        state="pending",
        args=b"{}",
        kwargs=b"{}",
        retries=0,
        created_at=now,
        started_at=None,
        completed_at=None,
        expiry_at=None,
        progress=[],
        tags=[],
        priority=0,
        queue=None,
    )


def _make_task(exec_id: str, task_id: str, step_name: str = "tests.step") -> TaskRecord:
    now = datetime.now()
    return TaskRecord(
        id=task_id,
        execution_id=exec_id,
        step_name=step_name,
        kind="activity",
        parent_task_id=None,
        state="pending",
        args=b"{}",
        kwargs=b"{}",
        retries=0,
        created_at=now,
        tags=[],
        priority=0,
        queue=None,
        retry_policy=RetryPolicy(),
    )


class FailingSQLiteBackend(SQLiteBackend):
    def __init__(self, db_path: str):
        super().__init__(db_path)
        self.fail_next_task_insert = False

    async def _insert_task(self, db, task: TaskRecord):  # type: ignore[override]
        if self.fail_next_task_insert:
            self.fail_next_task_insert = False
            raise RuntimeError("forced failure")
        await super()._insert_task(db, task)


@pytest.mark.asyncio
async def test_create_execution_with_root_task_is_atomic(tmp_path):
    backend = FailingSQLiteBackend(str(tmp_path / "atomic.sqlite"))
    await backend.init_db()

    record = _make_execution("exec-atomic")
    task = _make_task(record.id, "task-atomic")

    backend.fail_next_task_insert = True
    with pytest.raises(RuntimeError):
        await backend.create_execution_with_root_task(record, task)

    assert await backend.get_execution(record.id) is None
    assert await backend.get_task(task.id) is None

    await cleanup_test_backend(backend)


@pytest.mark.asyncio
async def test_sqlite_claim_next_task_multi_worker(tmp_path):
    backend = SQLiteBackend(str(tmp_path / "claim.sqlite"))
    await backend.init_db()

    exec_id = "exec-claim"
    await backend.create_execution(_make_execution(exec_id))

    tasks: List[TaskRecord] = [
        _make_task(exec_id, f"task-{idx}") for idx in range(50)
    ]
    await backend.create_tasks(tasks)

    async def worker(worker_idx: int):
        claimed: List[str] = []
        while True:
            task = await backend.claim_next_task(worker_id=f"worker-{worker_idx}")
            if not task:
                break
            claimed.append(task.id)
            task.state = "completed"
            task.completed_at = datetime.now()
            await backend.update_task(task)
        return claimed

    results = await asyncio.gather(*(worker(i) for i in range(6)))
    claimed_ids = [cid for worker_claims in results for cid in worker_claims]

    assert len(claimed_ids) == len(set(claimed_ids))
    assert set(claimed_ids) == {task.id for task in tasks}

    await cleanup_test_backend(backend)


@pytest.mark.asyncio
async def test_sqlite_concurrency_limits_respected(tmp_path):
    backend = SQLiteBackend(str(tmp_path / "limits.sqlite"))
    await backend.init_db()

    exec_id = "exec-limit"
    await backend.create_execution(_make_execution(exec_id))
    task_one = _make_task(exec_id, "task-one", step_name="only.step")
    task_two = _make_task(exec_id, "task-two", step_name="only.step")
    await backend.create_tasks([task_one, task_two])

    limits = {"only.step": 1}
    first_claim = await backend.claim_next_task(worker_id="worker-a", concurrency_limits=limits)
    assert first_claim is not None

    # Second claim should be blocked until the running task completes
    blocked_claim = await backend.claim_next_task(worker_id="worker-b", concurrency_limits=limits)
    assert blocked_claim is None

    first_claim.state = "completed"
    first_claim.completed_at = datetime.now()
    await backend.update_task(first_claim)

    second_claim = await backend.claim_next_task(worker_id="worker-c", concurrency_limits=limits)
    assert second_claim is not None

    await cleanup_test_backend(backend)


@pytest.mark.asyncio
async def test_sqlite_claim_respects_scheduled_for(tmp_path):
    backend = SQLiteBackend(str(tmp_path / "scheduled.sqlite"))
    await backend.init_db()

    exec_id = "exec-scheduled"
    await backend.create_execution(_make_execution(exec_id))

    task = _make_task(exec_id, "task-scheduled")
    task.scheduled_for = datetime.now() + timedelta(seconds=0.4)
    await backend.create_task(task)

    before_due = await backend.claim_next_task(worker_id="worker-a")
    assert before_due is None

    await asyncio.sleep(0.45)

    after_due = await backend.claim_next_task(worker_id="worker-a")
    assert after_due is not None
    assert after_due.id == "task-scheduled"

    await cleanup_test_backend(backend)


@pytest.mark.asyncio
async def test_dead_letter_round_trip(tmp_path):
    backend = SQLiteBackend(str(tmp_path / "dlq.sqlite"))
    await backend.init_db()

    exec_id = "exec-dlq"
    await backend.create_execution(_make_execution(exec_id))
    task = _make_task(exec_id, "task-dlq")
    await backend.create_task(task)

    await backend.move_task_to_dead_letter(task, "boom")
    records = await backend.list_dead_tasks()
    assert len(records) == 1
    record = await backend.get_dead_task(task.id)
    assert record is not None
    assert record.task.execution_id == exec_id
    deleted = await backend.delete_dead_task(task.id)
    assert deleted
    assert await backend.list_dead_tasks() == []

    await cleanup_test_backend(backend)


@pytest.mark.asyncio
async def test_sqlite_count_apis(tmp_path):
    backend = SQLiteBackend(str(tmp_path / "count.sqlite"))
    await backend.init_db()

    pending_exec = _make_execution("exec-pending")
    completed_exec = _make_execution("exec-completed")
    completed_exec.state = "completed"
    completed_exec.completed_at = datetime.now()

    await backend.create_execution(pending_exec)
    await backend.create_execution(completed_exec)

    assert await backend.count_executions() == 2
    assert await backend.count_executions(state="pending") == 1
    assert await backend.count_executions(state="completed") == 1

    dead_task = _make_task("exec-pending", "dead-task")
    await backend.move_task_to_dead_letter(dead_task, "boom")

    assert await backend.count_dead_tasks() == 1

    await cleanup_test_backend(backend)
