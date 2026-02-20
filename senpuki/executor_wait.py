from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Awaitable, Callable

from senpuki.core import ExecutionState, TaskRecord
from senpuki.notifications.base import NotificationBackend

TASK_TERMINAL_STATES = ("completed", "failed")
EXECUTION_TERMINAL_STATES = ("completed", "failed", "timed_out", "cancelled")


async def wait_for_task_terminal(
    *,
    task_id: str,
    get_task: Callable[[str], Awaitable[TaskRecord | None]],
    notification_backend: NotificationBackend | None,
    expiry: float | None,
    poll_min_interval: float,
    poll_max_interval: float,
    poll_backoff_factor: float,
    sleep_fn: Callable[[float], Awaitable[None]],
    expiry_error_factory: Callable[[str], Exception],
) -> TaskRecord:
    if notification_backend:
        it = notification_backend.subscribe_to_task(task_id, expiry=expiry)
        try:
            async for _ in it:
                pass
        except asyncio.TimeoutError as exc:
            raise expiry_error_factory(f"Task {task_id} timed out") from exc

        task = await get_task(task_id)
        if not task:
            raise ValueError(f"Task not found after notification: {task_id}")
        if task.state not in TASK_TERMINAL_STATES:
            if expiry is not None:
                raise expiry_error_factory(f"Task {task_id} timed out")
            raise Exception(f"Task {task_id} is not terminal after notification")
        return task

    start = datetime.now()
    delay = poll_min_interval
    while True:
        task = await get_task(task_id)
        if task and task.state in TASK_TERMINAL_STATES:
            return task
        if expiry and (datetime.now() - start).total_seconds() > expiry:
            raise expiry_error_factory(f"Task {task_id} timed out")
        await sleep_fn(delay)
        delay = min(poll_max_interval, delay * poll_backoff_factor)


async def wait_for_execution_terminal(
    *,
    execution_id: str,
    state_of: Callable[[str], Awaitable[ExecutionState]],
    notification_backend: NotificationBackend | None,
    expiry: float | None,
    sleep_interval: float,
    expiry_error_factory: Callable[[str], Exception],
) -> ExecutionState:
    if notification_backend:
        it = notification_backend.subscribe_to_execution(execution_id, expiry=expiry)
        try:
            async for _ in it:
                pass
        except asyncio.TimeoutError:
            raise expiry_error_factory(f"Timed out waiting for execution {execution_id}")

        state = await state_of(execution_id)
        if state.state not in EXECUTION_TERMINAL_STATES:
            if expiry is not None:
                raise expiry_error_factory(f"Timed out waiting for execution {execution_id}")
            raise Exception(f"Execution {execution_id} is not terminal after notification")
        return state

    start = datetime.now()
    while True:
        state = await state_of(execution_id)
        if state.state in EXECUTION_TERMINAL_STATES:
            return state

        if expiry and (datetime.now() - start).total_seconds() > expiry:
            raise expiry_error_factory(f"Timed out waiting for execution {execution_id}")

        await asyncio.sleep(sleep_interval)
