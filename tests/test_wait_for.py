import unittest
import asyncio
import os
import logging
from datetime import datetime, timedelta
from typing import Any, cast
from stent import Stent, Result
from stent.core import TaskRecord, RetryPolicy, ExecutionRecord
import stent.executor as executor_module
from stent.executor import ExpiryError, _original_sleep
from tests.utils import get_test_backend, cleanup_test_backend, clear_test_backend

logger = logging.getLogger(__name__)

@Stent.durable()
async def quick_task():
    return "quick"

@Stent.durable()
async def slow_task(duration: float):
    await _original_sleep(duration)  # real sleep to simulate slow work
    return "slow"

class TestWaitFor(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = get_test_backend(f"waitfor_{os.getpid()}")
        await self.backend.init_db()
        await clear_test_backend(self.backend)
        self.executor = Stent(backend=self.backend)
        self.worker_task = asyncio.create_task(self.executor.serve(poll_interval=0.1))

    async def asyncTearDown(self):
        self.worker_task.cancel()
        try:
            await self.worker_task
        except asyncio.CancelledError:
            pass
        await self.executor.shutdown()
        await cleanup_test_backend(self.backend)

    async def test_wait_success(self):
        exec_id = await self.executor.dispatch(quick_task)
        
        # Should block until done
        result = await self.executor.wait_for(exec_id, expiry=10.0) # Increased expiry
        
        self.assertTrue(result.ok)
        self.assertEqual(result.value, "quick")

    async def test_wait_for_timeout(self):
        # We need a longer task than the wait expiry
        @Stent.durable()
        async def slow_task_impl():
            await _original_sleep(5.0)  # real sleep to simulate slow work
            return "slow"
            
        exec_id = await self.executor.dispatch(slow_task_impl)
        
        with self.assertRaises(ExpiryError):
            await self.executor.wait_for(exec_id, expiry=0.5) # Allow 0.5s for dispatch overhead

        # Clean up by waiting properly
        await self.executor.wait_for(exec_id, expiry=10.0)

    async def test_wait_for_already_completed(self):
        exec_id = await self.executor.dispatch(quick_task)
        # Wait manually first
        await self.executor.wait_for(exec_id, expiry=5.0)
    
        # Now call wait_for, should return immediately
        start = asyncio.get_running_loop().time()
        result = await self.executor.wait_for(exec_id, expiry=5.0)
        end = asyncio.get_running_loop().time()
    
        self.assertTrue(result.ok)
        self.assertLess(end - start, 1.0) # Should be instant-ish, but allow 1.0 for remote DB roundtrip



class _PollingDummyBackend:
    def __init__(self):
        self.calls = 0

    async def get_task(self, task_id: str):
        self.calls += 1
        if self.calls >= 4:
            return TaskRecord(
                id=task_id,
                execution_id="exec",
                step_name="test",
                kind="activity",
                parent_task_id=None,
                state="completed",
                args=b"",
                kwargs=b"",
                retries=0,
                created_at=datetime.now(),
                tags=[],
                priority=0,
                queue=None,
                retry_policy=RetryPolicy(),
                result=b"done",
            )
        return None


class TestAdaptivePolling(unittest.IsolatedAsyncioTestCase):
    async def test_waiter_backoff_without_notifications(self):
        backend = _PollingDummyBackend()
        executor = Stent(
            backend=cast(Any, backend),
            poll_min_interval=0.01,
            poll_max_interval=0.04,
            poll_backoff_factor=2.0,
        )
        executor.notification_backend = None

        recorded = []
        original_sleep = executor_module._original_sleep

        async def fake_sleep(delay, result=None):
            recorded.append(delay)
            await asyncio.sleep(0)

        executor_module._original_sleep = cast(Any, fake_sleep)
        try:
            task = await executor._wait_for_task_internal("poll-task")
            self.assertEqual(task.id, "poll-task")
        finally:
            executor_module._original_sleep = original_sleep

        self.assertEqual(recorded, [0.01, 0.02, 0.04])


class _NotificationTimeoutBackend:
    async def notify_task_completed(self, task_id: str):
        return None

    async def notify_task_updated(self, task_id: str, state: str):
        return None

    async def subscribe_to_task(self, task_id: str, *, expiry: float | None = None):
        if expiry is not None:
            try:
                async with asyncio.timeout(expiry):
                    await asyncio.sleep(expiry + 0.05)
            except asyncio.TimeoutError:
                pass
        if False:
            yield {"task_id": task_id, "state": "pending"}

    async def notify_execution_updated(self, execution_id: str, state: str):
        return None

    async def subscribe_to_execution(self, execution_id: str, *, expiry: float | None = None):
        if expiry is not None:
            try:
                async with asyncio.timeout(expiry):
                    await asyncio.sleep(expiry + 0.05)
            except asyncio.TimeoutError:
                pass
        if False:
            yield {"execution_id": execution_id, "state": "running"}


class _AlwaysPendingBackend:
    async def get_task(self, task_id: str):
        return TaskRecord(
            id=task_id,
            execution_id="exec-timeout",
            step_name="test",
            kind="activity",
            parent_task_id=None,
            state="pending",
            args=b"",
            kwargs=b"",
            retries=0,
            created_at=datetime.now(),
            tags=[],
            priority=0,
            queue=None,
            retry_policy=RetryPolicy(),
        )

    async def get_execution(self, execution_id: str):
        return ExecutionRecord(
            id=execution_id,
            root_function="tests.timeout",
            state="running",
            args=b"{}",
            kwargs=b"{}",
            retries=0,
            created_at=datetime.now(),
            started_at=datetime.now(),
            completed_at=None,
            expiry_at=None,
            progress=[],
            tags=[],
            priority=0,
            queue=None,
        )


class _CancelledExecutionBackend:
    async def get_execution(self, execution_id: str):
        return ExecutionRecord(
            id=execution_id,
            root_function="tests.cancelled",
            state="cancelled",
            args=b"{}",
            kwargs=b"{}",
            retries=0,
            created_at=datetime.now(),
            started_at=datetime.now() - timedelta(seconds=1),
            completed_at=datetime.now(),
            expiry_at=None,
            progress=[],
            tags=[],
            priority=0,
            queue=None,
            result=None,
            error=None,
        )


class TestNotificationTimeoutAndCancelled(unittest.IsolatedAsyncioTestCase):
    async def test_wait_for_task_timeout_with_notifications_raises_expiry(self):
        executor = Stent(
            backend=cast(Any, _AlwaysPendingBackend()),
            notification_backend=cast(Any, _NotificationTimeoutBackend()),
        )

        with self.assertRaises(ExpiryError):
            await executor._wait_for_task_internal("task-timeout", expiry=0.05)

    async def test_wait_for_timeout_with_notifications_raises_expiry(self):
        executor = Stent(
            backend=cast(Any, _AlwaysPendingBackend()),
            notification_backend=cast(Any, _NotificationTimeoutBackend()),
        )

        with self.assertRaises(ExpiryError):
            await executor.wait_for("exec-timeout", expiry=0.05)

    async def test_cancelled_result_of_and_wait_for_are_consistent(self):
        executor = Stent(backend=cast(Any, _CancelledExecutionBackend()))

        result_direct = await executor.result_of("exec-cancelled")
        result_wait = await executor.wait_for("exec-cancelled", expiry=1.0)

        self.assertFalse(result_direct.ok)
        self.assertFalse(result_wait.ok)
        self.assertEqual(str(result_direct.error), "Execution cancelled")
        self.assertEqual(str(result_wait.error), "Execution cancelled")
