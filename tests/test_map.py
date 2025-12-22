import unittest
import asyncio
import os
import logging
from senpuki import Senpuki, Result
from tests.utils import get_test_backend, cleanup_test_backend

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@Senpuki.durable()
async def square(x: int) -> int:
    await asyncio.sleep(0.1)
    return x * x

@Senpuki.durable()
async def fail_on_three(x: int) -> int:
    if x == 3:
        raise ValueError("Three is forbidden")
    return x * x

class TestMap(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = get_test_backend(f"map_{os.getpid()}")
        await self.backend.init_db()
        self.executor = Senpuki(backend=self.backend)
        self.worker_task = asyncio.create_task(self.executor.serve(poll_interval=0.1, max_concurrency=10))

    async def asyncTearDown(self):
        self.worker_task.cancel()
        try:
            await self.worker_task
        except asyncio.CancelledError:
            pass
        await cleanup_test_backend(self.backend)

    async def test_map_basic(self):
        @Senpuki.durable()
        async def workflow(items: list[int]) -> list[int]:
            return await Senpuki.map(square, items)

        exec_id = await self.executor.dispatch(workflow, [1, 2, 3, 4, 5])
        
        # Wait for result
        final = await self.executor.wait_for(exec_id, expiry=10.0)
        self.assertTrue(final.ok)
        self.assertEqual(final.value, [1, 4, 9, 16, 25])

    async def test_map_empty(self):
        @Senpuki.durable()
        async def workflow() -> list[int]:
            return await Senpuki.map(square, [])

        exec_id = await self.executor.dispatch(workflow)
        final = await self.executor.wait_for(exec_id, expiry=5.0)
        self.assertTrue(final.ok)
        self.assertEqual(final.value, [])

    async def test_map_failure(self):
        @Senpuki.durable()
        async def workflow(items: list[int]) -> list[int]:
            return await Senpuki.map(fail_on_three, items)

        exec_id = await self.executor.dispatch(workflow, [1, 2, 3])
        
        final = await self.executor.wait_for(exec_id, expiry=5.0)
        self.assertFalse(final.ok)
        self.assertIn("Three is forbidden", str(final.error))

    async def test_gather_alias(self):
         from typing import Any
         @Senpuki.durable()
         async def workflow() -> list[Any]:
             t1 = square(10)
             t2 = square(20)
             return await Senpuki.gather(t1, t2)
         
         exec_id = await self.executor.dispatch(workflow)
         final = await self.executor.wait_for(exec_id, expiry=5.0)
         self.assertTrue(final.ok)
         self.assertEqual(final.value, [100, 400])
