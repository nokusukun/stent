import unittest
import asyncio
import os
import logging
from datetime import datetime, timedelta
from senpuki import Senpuki, Result
from senpuki.core import TaskRecord, RetryPolicy
import senpuki.executor as executor_module
from senpuki.executor import ExpiryError
from tests.utils import get_test_backend, cleanup_test_backend, clear_test_backend

logger = logging.getLogger(__name__)

@Senpuki.durable()
async def quick_task():
    return "quick"

@Senpuki.durable()
async def slow_task(duration: float):
    await asyncio.sleep(duration)
    return "slow"

class TestWaitFor(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = get_test_backend(f"waitfor_{os.getpid()}")
        await self.backend.init_db()
        await clear_test_backend(self.backend)
        self.executor = Senpuki(backend=self.backend)
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
        @Senpuki.durable()
        async def slow_task_impl():
            await asyncio.sleep(5.0) # Longer sleep to be safe
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
        executor = Senpuki(
            backend=backend,
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

        executor_module._original_sleep = fake_sleep
        try:
            task = await executor._wait_for_task_internal("poll-task")
            self.assertEqual(task.id, "poll-task")
        finally:
            executor_module._original_sleep = original_sleep

        self.assertEqual(recorded, [0.01, 0.02, 0.04])
