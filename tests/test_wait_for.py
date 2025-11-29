from dfns.executor import ExpiryError
import unittest
import asyncio
import os
import logging
from dfns import DFns, Result

logging.basicConfig(level=logging.INFO)

@DFns.durable()
async def quick_task() -> str:
    await asyncio.sleep(0.5)
    return "done"

class TestWaitFor(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db_path = f"test_wait_for_{os.getpid()}.sqlite"
        self.backend = DFns.backends.SQLiteBackend(self.db_path)
        await self.backend.init_db()
        self.executor = DFns(backend=self.backend)
        self.worker_task = asyncio.create_task(self.executor.serve(poll_interval=0.1))

    async def asyncTearDown(self):
        self.worker_task.cancel()
        try:
            await self.worker_task
        except asyncio.CancelledError:
            pass
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    async def test_wait_for_success(self):
        exec_id = await self.executor.dispatch(quick_task)
        
        # Should block until done
        result = await self.executor.wait_for(exec_id, expiry=2.0)
        
        self.assertTrue(result.ok)
        self.assertEqual(result.value, "done")

    async def test_wait_for_expiry(self):
        # We need a longer task than the wait expiry
        @DFns.durable()
        async def slow_task():
            await asyncio.sleep(1.0)
            return "slow"
            
        exec_id = await self.executor.dispatch(slow_task)
        
        with self.assertRaises(ExpiryError):
            await self.executor.wait_for(exec_id, expiry=0.1)

        # Clean up by waiting properly
        await self.executor.wait_for(exec_id, expiry=2.0)

    async def test_wait_for_already_completed(self):
        exec_id = await self.executor.dispatch(quick_task)
        # Wait manually first
        await asyncio.sleep(1.0)
        
        # Now call wait_for, should return immediately
        start = asyncio.get_running_loop().time()
        result = await self.executor.wait_for(exec_id, expiry=1.0)
        end = asyncio.get_running_loop().time()
        
        self.assertTrue(result.ok)
        self.assertLess(end - start, 0.1) # Should be instant-ish

