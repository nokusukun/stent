import unittest
import asyncio
import os
import logging
from senpuki import Senpuki, Result, sleep
from senpuki.executor import _original_sleep
from tests.utils import get_test_backend, cleanup_test_backend, clear_test_backend

logger = logging.getLogger(__name__)

@Senpuki.durable()
async def sleep_task():
    await sleep("0.2s")
    return "done"

@Senpuki.durable()
async def noop_task():
    pass

class TestHelpers(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = get_test_backend(f"helpers_{os.getpid()}")
        await self.backend.init_db()
        await clear_test_backend(self.backend) # Ensure clean slate
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

    async def test_asyncio_sleep_not_patched(self):
        self.assertIs(asyncio.sleep, _original_sleep)

        observed = []

        async def third_party_coroutine():
            await asyncio.sleep(0.01)
            observed.append("ok")

        await third_party_coroutine()
        self.assertEqual(observed, ["ok"])

    async def test_queue_depth(self):
        # Cancel default worker to prevent it from consuming tasks
        self.worker_task.cancel()
        try:
            await self.worker_task
        except asyncio.CancelledError:
            pass
            
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
