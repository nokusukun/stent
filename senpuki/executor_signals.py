from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable

from senpuki.core import ExecutionProgress, SignalRecord, TaskRecord


def signal_step_name(name: str) -> str:
    return f"signal:{name}"


def deterministic_signal_task_id(execution_id: str, step_name: str) -> str:
    try:
        exec_uuid = uuid.UUID(execution_id)
    except ValueError:
        exec_uuid = uuid.uuid4()
    return str(uuid.uuid5(exec_uuid, step_name))


async def persist_signal_and_wake_waiter(
    *,
    execution_id: str,
    name: str,
    payload_bytes: bytes,
    create_signal: Callable[[SignalRecord], Awaitable[None]],
    list_tasks_for_execution: Callable[[str], Awaitable[list[TaskRecord]]],
    update_task: Callable[[TaskRecord], Awaitable[None]],
    notify_task_completed: Callable[[str], Awaitable[None]] | None,
) -> None:
    signal = SignalRecord(
        execution_id=execution_id,
        name=name,
        payload=payload_bytes,
        created_at=datetime.now(),
        consumed=False,
    )
    await create_signal(signal)

    target_step = signal_step_name(name)
    tasks = await list_tasks_for_execution(execution_id)
    for task in tasks:
        if task.step_name == target_step and task.kind == "signal" and task.state == "pending":
            task.state = "completed"
            task.result = payload_bytes
            task.completed_at = datetime.now()
            await update_task(task)
            if notify_task_completed:
                await notify_task_completed(task.id)
            break


async def resolve_signal_wait(
    *,
    execution_id: str,
    name: str,
    parent_task_id: str | None,
    loads: Callable[[bytes], Any],
    get_task: Callable[[str], Awaitable[TaskRecord | None]],
    get_signal: Callable[[str, str], Awaitable[SignalRecord | None]],
    create_signal: Callable[[SignalRecord], Awaitable[None]],
    create_task: Callable[[TaskRecord], Awaitable[None]],
    append_progress: Callable[[str, ExecutionProgress], Awaitable[None]],
    wait_for_task: Callable[[str], Awaitable[TaskRecord]],
) -> Any:
    step_name = signal_step_name(name)
    task_id = deterministic_signal_task_id(execution_id, step_name)

    existing_task = await get_task(task_id)
    if existing_task:
        if existing_task.state == "completed":
            return loads(existing_task.result) if existing_task.result is not None else None
        completed = await wait_for_task(task_id)
        return loads(completed.result) if completed.result is not None else None

    signal = await get_signal(execution_id, name)
    if signal and not signal.consumed:
        signal.consumed = True
        signal.consumed_at = datetime.now()
        await create_signal(signal)

        task = TaskRecord(
            id=task_id,
            execution_id=execution_id,
            step_name=step_name,
            kind="signal",
            parent_task_id=parent_task_id,
            state="completed",
            args=b"",
            kwargs=b"",
            retries=0,
            created_at=datetime.now(),
            completed_at=datetime.now(),
            tags=["signal"],
            priority=0,
            queue=None,
            retry_policy=None,
            result=signal.payload,
        )
        await create_task(task)
        await append_progress(
            execution_id,
            ExecutionProgress(step=step_name, status="completed", detail="Signal consumed from buffer"),
        )
        return loads(signal.payload)

    pending_task = TaskRecord(
        id=task_id,
        execution_id=execution_id,
        step_name=step_name,
        kind="signal",
        parent_task_id=parent_task_id,
        state="pending",
        args=b"",
        kwargs=b"",
        retries=0,
        created_at=datetime.now(),
        tags=["signal"],
        priority=0,
        queue=None,
        retry_policy=None,
    )
    await create_task(pending_task)
    await append_progress(
        execution_id,
        ExecutionProgress(step=step_name, status="dispatched", detail="Waiting for signal"),
    )

    completed = await wait_for_task(task_id)

    latest_signal = await get_signal(execution_id, name)
    if latest_signal and not latest_signal.consumed:
        latest_signal.consumed = True
        latest_signal.consumed_at = datetime.now()
        await create_signal(latest_signal)

    return loads(completed.result) if completed.result is not None else None
