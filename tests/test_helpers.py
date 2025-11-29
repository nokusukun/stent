import unittest
import asyncio
import os
import logging
from dfns import DFns, Result

logging.basicConfig(level=logging.INFO)

@DFns.durable()
async def noop_task():
    return "done"

class TestHelpers(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db_path = f"test_helpers_{os.getpid()}.sqlite"
        self.backend = DFns.backends.SQLiteBackend(self.db_path)
        await self.backend.init_db()
        self.executor = DFns(backend=self.backend)
        # Don't start worker immediately to test pending state

    async def asyncTearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    async def test_queue_depth(self):
        self.assertEqual(await self.executor.queue_depth(), 0)
        
        # Dispatch a task
        await self.executor.dispatch(noop_task)
        self.assertEqual(await self.executor.queue_depth(), 1) 
        
        # Dispatch another
        await self.executor.dispatch(noop_task, queue="special")
        self.assertEqual(await self.executor.queue_depth(), 2)
        self.assertEqual(await self.executor.queue_depth(queue="special"), 1)

    async def test_list_executions(self):
        ids = []
        for _ in range(5):
            ids.append(await self.executor.dispatch(noop_task))
            
        executions = await self.executor.list_executions(limit=10)
        self.assertEqual(len(executions), 5)
        self.assertEqual(executions[0].state, "pending")
        
        # Start worker to finish them
        task = asyncio.create_task(self.executor.serve(poll_interval=0.1))
        
        for eid in ids:
            await self.executor.wait_for(eid, expiry=2.0)
            
        executions = await self.executor.list_executions(state="completed")
        self.assertEqual(len(executions), 5)
        
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
