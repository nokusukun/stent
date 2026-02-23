import unittest
import asyncio
import os
import logging
from stent import Stent, Result, RetryPolicy
from tests.utils import get_test_backend, cleanup_test_backend, clear_test_backend

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@Stent.durable()
async def square(x: int) -> int:
    await asyncio.sleep(0.1)
    return x * x

@Stent.durable(retry_policy=RetryPolicy(max_attempts=3, initial_delay=0.1))
async def fail_on_three(x: int) -> int:
    if x == 3:
        raise ValueError("Three is forbidden")
    return x * x

@Stent.durable()
async def add(a: int, b: int) -> int:
    return a + b

class TestMap(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = get_test_backend(f"map_{os.getpid()}")
        await self.backend.init_db()
        await clear_test_backend(self.backend)
        self.executor = Stent(backend=self.backend)
        self.worker_task = asyncio.create_task(self.executor.serve(poll_interval=0.1, max_concurrency=10))

    async def asyncTearDown(self):
        self.worker_task.cancel()
        try:
            await self.worker_task
        except asyncio.CancelledError:
            pass
        await self.executor.shutdown()
        await cleanup_test_backend(self.backend)

    async def test_map_basic(self):
        @Stent.durable()
        async def workflow(items: list[int]) -> list[int]:
            return await Stent.map(square, items)
    
        exec_id = await self.executor.dispatch(workflow, [1, 2, 3, 4, 5])
    
        # Wait for result
        final = await self.executor.wait_for(exec_id, expiry=30.0)
        self.assertTrue(final.ok)
        self.assertEqual(final.value, [1, 4, 9, 16, 25])

    async def test_map_empty(self):
        @Stent.durable()
        async def workflow() -> list[int]:
            return await Stent.map(square, [])

        exec_id = await self.executor.dispatch(workflow)
        final = await self.executor.wait_for(exec_id, expiry=5.0)
        self.assertTrue(final.ok)
        self.assertEqual(final.value, [])

    async def test_map_failure(self):
        @Stent.durable(retry_policy=RetryPolicy(max_attempts=1))
        async def workflow(items: list[int]) -> list[int]:
            return await Stent.map(fail_on_three, items)

        exec_id = await self.executor.dispatch(workflow, [1, 2, 3])
        
        final = await self.executor.wait_for(exec_id, expiry=15.0)
        self.assertFalse(final.ok)
        self.assertIn("Three is forbidden", str(final.error))

    async def test_gather_alias(self):
         from typing import Any
         @Stent.durable()
         async def workflow() -> list[Any]:
             t1 = square(10)
             t2 = square(20)
             return await Stent.gather(t1, t2)

         exec_id = await self.executor.dispatch(workflow)
         final = await self.executor.wait_for(exec_id, expiry=15.0)
         self.assertTrue(final.ok)
         self.assertEqual(final.value, [100, 400])

    async def test_dot_map(self):
        """square.map([1, 2, 3]) dispatches each item as a single-arg call."""
        @Stent.durable()
        async def workflow(items: list[int]) -> list[int]:
            return await square.map(items)

        exec_id = await self.executor.dispatch(workflow, [1, 2, 3, 4, 5])
        final = await self.executor.wait_for(exec_id, expiry=30.0)
        self.assertTrue(final.ok)
        self.assertEqual(final.value, [1, 4, 9, 16, 25])

    async def test_dot_map_empty(self):
        @Stent.durable()
        async def workflow() -> list[int]:
            return await square.map([])

        exec_id = await self.executor.dispatch(workflow)
        final = await self.executor.wait_for(exec_id, expiry=5.0)
        self.assertTrue(final.ok)
        self.assertEqual(final.value, [])

    async def test_starmap(self):
        """add.starmap([(1, 2), (3, 4)]) unpacks each tuple as positional args."""
        @Stent.durable()
        async def workflow() -> list[int]:
            return await add.starmap([(1, 2), (3, 4), (10, 20)])

        exec_id = await self.executor.dispatch(workflow)
        final = await self.executor.wait_for(exec_id, expiry=15.0)
        self.assertTrue(final.ok)
        self.assertEqual(final.value, [3, 7, 30])

    async def test_dot_map_local_fallback(self):
        """Without executor context, .map() falls back to local gather."""
        result = await square.map([2, 3, 4])
        self.assertEqual(result, [4, 9, 16])
