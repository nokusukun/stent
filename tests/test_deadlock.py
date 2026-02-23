import unittest
import asyncio
import os
import logging
from stent import Stent, Result
from tests.utils import get_test_backend, cleanup_test_backend, clear_test_backend

logging.basicConfig(level=logging.INFO)

@Stent.durable()
async def leaf_activity():
    return "done"

@Stent.durable()
async def deadlocking_orchestrator():
    # Schedules a leaf task and waits for it
    return await leaf_activity()

class TestDeadlock(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = get_test_backend(f"deadlock_{os.getpid()}")
        await self.backend.init_db()
        await clear_test_backend(self.backend)
        self.executor = Stent(backend=self.backend)

    async def asyncTearDown(self):
        await self.executor.shutdown()
        await cleanup_test_backend(self.backend)

    async def test_single_concurrency_deadlock(self):
        # We start a worker with concurrency=1
        # If the orchestrator holds the slot while waiting, the leaf activity can never run.
        worker_task = asyncio.create_task(self.executor.serve(max_concurrency=1, poll_interval=0.1))
        
        try:
            exec_id = await self.executor.dispatch(deadlocking_orchestrator)
            
            # If fix works, this returns. If not, it times out.
            # We set a generous expiry for the test
            result = await self.executor.wait_for(exec_id, expiry=30.0)
            
            self.assertTrue(result.ok)
            self.assertEqual(result.value, "done")
            
        finally:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass
            await self.executor.shutdown()

