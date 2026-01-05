import unittest
import asyncio
import os
import logging
from datetime import timedelta
from senpuki import Senpuki, Result
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

