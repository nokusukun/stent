from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from senpuki.core import ExecutionProgress, ExecutionRecord, TaskRecord
from senpuki.registry import FunctionMetadata
from senpuki.utils.idempotency import default_idempotency_key
from senpuki.utils.time import parse_duration

if TYPE_CHECKING:
    from senpuki.executor import Senpuki


async def execute_map_batch(
    *,
    executor: Senpuki,
    meta: FunctionMetadata,
    iterable: Any,
    exec_id: str,
    parent_id: str,
    permit_holder: Any,
) -> list[Any]:
    tasks_to_create: list[TaskRecord] = []
    results_or_tasks: list[Any | TaskRecord] = []

    args_list = []
    for item in iterable:
        args_list.append(((item,), {}))

    for args, kwargs in args_list:
        key = None
        hit = False

        if meta.idempotent or meta.cached:
            if meta.idempotency_key_func:
                key = meta.idempotency_key_func(*args, **kwargs)
            else:
                key = default_idempotency_key(meta.name, meta.version, args, kwargs, serializer=executor.serializer)

            if key:
                if meta.cached:
                    cached_val = await executor.backend.get_cached_result(key)
                    if cached_val is not None:
                        if exec_id:
                            await executor.backend.append_progress(exec_id, ExecutionProgress(step=meta.name, status="cache_hit"))
                        results_or_tasks.append(executor.serializer.loads(cached_val))
                        hit = True

                if not hit and meta.idempotent:
                    stored_val = await executor.backend.get_idempotency_result(key)
                    if stored_val is not None:
                        if exec_id:
                            await executor.backend.append_progress(exec_id, ExecutionProgress(step=meta.name, status="cache_hit"))
                        results_or_tasks.append(executor.serializer.loads(stored_val))
                        hit = True

        if not hit:
            task = TaskRecord(
                id=str(uuid.uuid4()),
                execution_id=exec_id,
                step_name=meta.name,
                kind="activity",
                parent_task_id=parent_id,
                state="pending",
                args=executor.serializer.dumps(args),
                kwargs=executor.serializer.dumps(kwargs),
                retries=0,
                created_at=datetime.now(),
                tags=meta.tags,
                priority=meta.priority,
                queue=meta.queue,
                retry_policy=meta.retry_policy,
                idempotency_key=key,
                scheduled_for=None,
            )
            tasks_to_create.append(task)
            results_or_tasks.append(task)

    if tasks_to_create:
        await executor.backend.create_tasks(tasks_to_create)
        if exec_id:
            for _ in tasks_to_create:
                await executor.backend.append_progress(exec_id, ExecutionProgress(step=meta.name, status="dispatched"))

    if permit_holder:
        permit_holder.release()

    try:
        async def waiter(item: Any | TaskRecord) -> Any:
            if isinstance(item, TaskRecord):
                completed = await executor._wait_for_task_internal(item.id)
                if completed.state == "failed":
                    if completed.error:
                        err = executor.serializer.loads(completed.error)
                        if isinstance(err, BaseException):
                            raise err
                        raise Exception(str(err))
                    raise Exception("Task failed")

                res = executor.serializer.loads(completed.result) if completed.result is not None else None
                if item.idempotency_key and completed.result is not None:
                    if meta.cached:
                        await executor.backend.set_cached_result(item.idempotency_key, completed.result)
                    if meta.idempotent:
                        await executor.backend.set_idempotency_result(item.idempotency_key, completed.result)
                return res
            return item

        return await asyncio.gather(*[waiter(item) for item in results_or_tasks])
    finally:
        if permit_holder:
            await permit_holder.acquire()


def normalize_dispatch_timing(
    *,
    expiry: str | timedelta | None,
    max_duration: str | timedelta | None,
    delay: str | dict | timedelta | None,
) -> tuple[timedelta | None, timedelta | None]:
    if expiry and max_duration:
        raise ValueError("Cannot provide both 'expiry' and 'max_duration'. Use 'max_duration' as 'expiry' is deprecated.")

    effective_expiry = max_duration if max_duration else expiry
    if isinstance(effective_expiry, str):
        effective_expiry = parse_duration(effective_expiry)

    effective_delay = delay
    if isinstance(effective_delay, (str, dict)):
        effective_delay = parse_duration(effective_delay)

    return effective_expiry, effective_delay


def build_dispatch_records(
    *,
    name: str,
    args_bytes: bytes,
    kwargs_bytes: bytes,
    retry_policy: Any,
    tags: list[str],
    priority: int,
    queue: str | None,
    expiry: timedelta | None,
    delay: timedelta | None,
) -> tuple[str, ExecutionRecord, TaskRecord]:
    scheduled_for = datetime.now() + delay if delay else None
    expiry_at = (datetime.now() + expiry) if expiry else None
    if scheduled_for and expiry:
        expiry_at = scheduled_for + expiry

    execution_id = str(uuid.uuid4())
    execution = ExecutionRecord(
        id=execution_id,
        root_function=name,
        state="pending",
        args=args_bytes,
        kwargs=kwargs_bytes,
        retries=0,
        created_at=datetime.now(),
        started_at=None,
        completed_at=None,
        expiry_at=expiry_at,
        progress=[],
        tags=tags,
        priority=priority,
        queue=queue,
    )
    root_task = TaskRecord(
        id=str(uuid.uuid4()),
        execution_id=execution_id,
        step_name=name,
        kind="orchestrator",
        parent_task_id=None,
        state="pending",
        args=execution.args,
        kwargs=execution.kwargs,
        retries=0,
        created_at=datetime.now(),
        tags=execution.tags,
        priority=priority,
        queue=execution.queue,
        retry_policy=retry_policy,
        scheduled_for=scheduled_for,
    )
    return execution_id, execution, root_task


async def persist_dispatch_records(
    *,
    executor: Senpuki,
    execution: ExecutionRecord,
    root_task: TaskRecord,
) -> None:
    create_with_root = getattr(executor.backend, "create_execution_with_root_task", None)
    if create_with_root is not None and callable(create_with_root):
        await create_with_root(execution, root_task)  # type: ignore[misc]
    else:
        await executor.backend.create_execution(execution)
        await executor.backend.create_task(root_task)


async def schedule_activity_and_wait(
    *,
    executor: Senpuki,
    meta: FunctionMetadata,
    args: tuple,
    kwargs: dict,
    idempotency_key: str | None,
    delay: timedelta | None,
    exec_id: str,
    parent_id: str,
) -> TaskRecord:
    scheduled_for = datetime.now() + delay if delay else None

    task_id = str(uuid.uuid4())
    task = TaskRecord(
        id=task_id,
        execution_id=exec_id,
        step_name=meta.name,
        kind="activity",
        parent_task_id=parent_id,
        state="pending",
        args=executor.serializer.dumps(args),
        kwargs=executor.serializer.dumps(kwargs),
        retries=0,
        created_at=datetime.now(),
        tags=meta.tags,
        priority=meta.priority,
        queue=meta.queue,
        retry_policy=meta.retry_policy,
        idempotency_key=idempotency_key,
        scheduled_for=scheduled_for,
    )

    await executor.backend.create_task(task)
    await executor.backend.append_progress(
        exec_id,
        ExecutionProgress(step=meta.name, status="dispatched", detail=f"Scheduled for {delay}" if delay else None),
    )

    return await executor._wait_for_task(task_id)
