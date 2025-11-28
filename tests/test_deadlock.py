import unittest
import asyncio
import os
import logging
from dfns import DFns, Result

logging.basicConfig(level=logging.INFO)

@DFns.durable()
async def leaf_activity():
    return "done"

@DFns.durable()
async def deadlocking_orchestrator():
    # Schedules a leaf task and waits for it
    return await leaf_activity()

class TestDeadlock(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db_path = f"test_deadlock_{os.getpid()}.sqlite"
        self.backend = DFns.backends.SQLiteBackend(self.db_path)
        await self.backend.init_db()
        self.executor = DFns(backend=self.backend)

    async def asyncTearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    async def test_single_concurrency_deadlock(self):
        # We start a worker with concurrency=1
        # If the orchestrator holds the slot while waiting, the leaf activity can never run.
        worker_task = asyncio.create_task(self.executor.serve(max_concurrency=1, poll_interval=0.1))
        
        try:
            exec_id = await self.executor.dispatch(deadlocking_orchestrator)
            
            # If fix works, this returns. If not, it times out.
            # We set a generous timeout for the test
            result = await self.executor.wait_for(exec_id, timeout=5.0)
            
            self.assertTrue(result.ok)
            self.assertEqual(result.value, "done")
            
        finally:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass

