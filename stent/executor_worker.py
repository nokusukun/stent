from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from logging import Logger
from typing import TYPE_CHECKING, Any, Callable, TypeVar, cast

from stent.core import ExecutionProgress, ExecutionRecord, RetryPolicy, TaskRecord, compute_retry_delay
from stent.executor_context import ExecutionContext, current_execution_context
from stent.utils.idempotency import default_idempotency_key

if TYPE_CHECKING:
    from stent.executor import Stent

TExpiryError = TypeVar("TExpiryError", bound=BaseException)


async def lease_heartbeat_loop(
    *,
    executor: Stent,
    task_id: str,
    worker_id: str,
    lease_duration: timedelta,
    interval: timedelta,
    stop_event: asyncio.Event,
    logger: Logger,
) -> None:
    timeout = interval.total_seconds()
    while True:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=timeout)
            return
        except asyncio.TimeoutError:
            try:
                renewed = await executor.backend.renew_task_lease(task_id, worker_id, lease_duration)
            except Exception:
                logger.exception("Lease renewal failed for task %s", task_id)
                executor.metrics.lease_renewed(task_id=task_id, success=False)
                return

            executor.metrics.lease_renewed(task_id=task_id, success=renewed)
            if not renewed:
                logger.warning(
                    "Lease renewal lost for task %s on worker %s; allowing reclaim",
                    task_id,
                    worker_id,
                )
                return
        except asyncio.CancelledError:
            return


async def run_claimed_task(
    *,
    executor: Stent,
    task: TaskRecord,
    worker_id: str,
    lease_duration: timedelta,
    heartbeat_interval: timedelta | None,
    expiry_error_type: type[TExpiryError],
    unregistered_error_factory: Callable[[str], Exception],
    logger: Logger,
) -> None:
    execution = None
    heartbeat_task: asyncio.Task[Any] | None = None
    heartbeat_stop: asyncio.Event | None = None
    task_context = ExecutionContext(executor, task.execution_id)
    token_context = current_execution_context.set(task_context)
    try:
        execution = await executor.backend.get_execution(task.execution_id)
        if not execution:
            raise ValueError(f"Execution {task.execution_id} not found")

        if execution.expiry_at and datetime.now() > execution.expiry_at:
            execution.state = "timed_out"
            execution.completed_at = datetime.now()
            task.state = "failed"
            task.error = executor.serializer.dumps(Exception("Execution timed out"))
            task.completed_at = datetime.now()
            await executor.backend.update_execution(execution)
            await executor.backend.update_task(task)
            if executor.notification_backend:
                await executor.notification_backend.notify_task_updated(task.id, "failed")
                await executor.notification_backend.notify_execution_updated(task.execution_id, "timed_out")
            await executor.backend.append_progress(
                task.execution_id,
                ExecutionProgress(step=task.step_name, status="failed", detail="Execution timed out"),
            )
            return

        if execution.state in ("cancelling", "cancelled"):
            task.state = "failed"
            task.error = executor.serializer.dumps(Exception("Execution cancelled"))
            task.completed_at = datetime.now()
            await executor.backend.update_task(task)
            if executor.notification_backend:
                await executor.notification_backend.notify_task_updated(task.id, "failed")
            await executor.backend.append_progress(
                task.execution_id,
                ExecutionProgress(step=task.step_name, status="failed", detail="Execution cancelled"),
            )
            return

        if task.kind == "orchestrator" and execution.state == "pending":
            execution.state = "running"
            execution.started_at = datetime.now()
            await executor.backend.update_execution(execution)
            if executor.notification_backend:
                await executor.notification_backend.notify_execution_updated(task.execution_id, "running")

        args = executor.serializer.loads(task.args)
        kwargs = executor.serializer.loads(task.kwargs)

        meta = executor.registry.get(task.step_name)
        if not meta:
            raise unregistered_error_factory(task.step_name)

        await executor.backend.append_progress(
            task.execution_id,
            ExecutionProgress(step=task.step_name, status="running", started_at=datetime.now()),
        )

        if heartbeat_interval:
            heartbeat_stop = asyncio.Event()
            heartbeat_task = asyncio.create_task(
                executor._lease_heartbeat_loop(
                    task_id=task.id,
                    worker_id=worker_id,
                    lease_duration=lease_duration,
                    interval=heartbeat_interval,
                    stop_event=heartbeat_stop,
                )
            )

        if execution.expiry_at:
            remaining = (execution.expiry_at - datetime.now()).total_seconds()
            if remaining <= 0:
                raise expiry_error_type("Execution timed out before start")
            try:
                async with asyncio.timeout(remaining):
                    result_val = await meta.fn(*args, **kwargs)
            except asyncio.TimeoutError:
                execution.state = "timed_out"
                execution.completed_at = datetime.now()
                await executor.backend.update_execution(execution)

                task.state = "failed"
                task.error = executor.serializer.dumps(Exception("Execution timed out"))
                task.completed_at = datetime.now()
                await executor.backend.update_task(task)

                if executor.notification_backend:
                    await executor.notification_backend.notify_task_updated(task.id, "failed")
                    await executor.notification_backend.notify_execution_updated(task.execution_id, "timed_out")
                await executor.backend.append_progress(
                    task.execution_id,
                    ExecutionProgress(step=task.step_name, status="failed", detail="Execution timed out"),
                )
                return
        else:
            result_val = await meta.fn(*args, **kwargs)

        task.result = executor.serializer.dumps(result_val)
        task.state = "completed"
        task.completed_at = datetime.now()
        await executor.backend.update_task(task)

        await executor.backend.append_progress(
            task.execution_id,
            ExecutionProgress(
                step=task.step_name,
                status="completed",
                started_at=task.started_at,
                completed_at=datetime.now(),
            ),
        )

        if task.kind == "orchestrator":
            execution.result = task.result
            execution.state = "completed"
            execution.completed_at = datetime.now()
            await executor.backend.update_execution(execution)
            if executor.notification_backend:
                await executor.notification_backend.notify_execution_updated(task.execution_id, "completed")

        if executor.notification_backend:
            await executor.notification_backend.notify_task_completed(task.id)

        if task.kind == "orchestrator" and meta.cached:
            key = default_idempotency_key(meta.name, meta.version, args, kwargs, serializer=executor.serializer)
            await executor.backend.set_cached_result(key, task.result)
            logger.debug(f"Cached result for orchestrator {meta.name} with key {key}")

        duration_s = 0.0
        if task.started_at and task.completed_at:
            duration_s = (task.completed_at - task.started_at).total_seconds()
        executor.metrics.task_completed(
            queue=task.queue,
            step_name=task.step_name,
            kind=task.kind,
            duration_s=duration_s,
        )

    except Exception as e:
        retry_policy = task.retry_policy or RetryPolicy()
        attempt = task.retries + 1

        is_retryable = any(isinstance(e, t) for t in retry_policy.retry_for)

        if is_retryable and attempt < retry_policy.max_attempts:
            delay = compute_retry_delay(retry_policy, attempt)
            task.retries = attempt
            task.state = "pending"
            task.scheduled_for = datetime.now() + timedelta(seconds=delay)
            task.worker_id = None
            task.lease_expires_at = None
            task.started_at = None
            await executor.backend.update_task(task)
            await executor.backend.append_progress(
                task.execution_id,
                ExecutionProgress(step=task.step_name, status="failed", detail=str(e)),
            )
            executor.metrics.task_failed(
                queue=task.queue,
                step_name=task.step_name,
                kind=task.kind,
                reason=str(e),
                retrying=True,
            )
        else:
            task.state = "failed"
            task.error = executor.serializer.dumps(e)
            task.completed_at = datetime.now()
            await executor.backend.update_task(task)
            await executor.backend.move_task_to_dead_letter(task, str(e))
            await executor.backend.append_progress(
                task.execution_id,
                ExecutionProgress(step=task.step_name, status="failed", detail=str(e)),
            )
            executor.metrics.task_failed(
                queue=task.queue,
                step_name=task.step_name,
                kind=task.kind,
                reason=str(e),
                retrying=False,
            )
            executor.metrics.dead_lettered(
                queue=task.queue,
                step_name=task.step_name,
                kind=task.kind,
                reason=str(e),
            )
            if execution is None:
                raise ValueError(f"Execution {task.execution_id} not found")

            if task.kind == "orchestrator":
                execution_for_update = cast(ExecutionRecord, execution)
                execution_for_update.state = "failed"
                execution_for_update.error = task.error
                execution_for_update.completed_at = datetime.now()
                await executor.backend.update_execution(execution_for_update)
                if executor.notification_backend:
                    await executor.notification_backend.notify_execution_updated(task.execution_id, "failed")

            if executor.notification_backend:
                await executor.notification_backend.notify_task_updated(task.id, "failed")

    finally:
        try:
            await task_context.flush()
        finally:
            current_execution_context.reset(token_context)
        if heartbeat_stop:
            heartbeat_stop.set()
        if heartbeat_task:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
