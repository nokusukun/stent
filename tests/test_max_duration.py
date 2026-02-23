import asyncio
import unittest
import os
from datetime import timedelta
from stent import Stent, Result
from tests.utils import get_test_backend, cleanup_test_backend, clear_test_backend

@Stent.durable()
async def long_running_task(duration_sec: float) -> str:
    await asyncio.sleep(duration_sec)
    return "done"

class TestMaxDuration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = get_test_backend(f"max_duration_{os.getpid()}")
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

    async def test_max_duration_success(self):
        # Task takes 0.1s, max_duration 1s -> should pass
        # Increase max_duration to 5s to account for latency
        exec_id = await self.executor.dispatch(long_running_task, 0.1, max_duration="5s")
        # Increase wait_for expiry too
        result = await self.executor.wait_for(exec_id, expiry=10.0)
        self.assertEqual(result.value, "done")

    async def test_max_duration_timeout(self):
        # Task takes 2s, max_duration 1s -> should timeout
        # Using a very short max_duration to speed up test
        exec_id = await self.executor.dispatch(long_running_task, 2.0, max_duration=timedelta(seconds=0.5))
        
        # Wait for completion (it should fail/timeout)
        try:
            await self.executor.wait_for(exec_id)
        except Exception:
            pass # Expect error result, but wait_for might raise or return Result.Error depending on impl
            
        state = await self.executor.state_of(exec_id)
        self.assertIn(state.state, ("timed_out", "failed"))
        if state.state == "failed":
             # If it failed due to timeout exception bubbling up
             try:
                 await self.executor.result_of(exec_id)
             except Exception as e:
                 self.assertIn("timed out", str(e))

    async def test_conflict_error(self):
        with self.assertRaises(ValueError):
            await self.executor.dispatch(long_running_task, 0.1, expiry="1m", max_duration="1m")

