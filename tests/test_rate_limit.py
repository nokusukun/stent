import unittest
import asyncio
import uuid
import os
from datetime import datetime, timedelta
from dfns import DFns
from dfns.registry import registry
from tests.utils import get_test_backend, cleanup_test_backend

# Globals for pickles
@DFns.durable(max_concurrent=1)
async def limited_task(idx: int):
    # We log to a global list (careful with parallel tests, but IsolatedAsyncioTestCase runs sequentially usually? No, but global var is shared)
    # Better to return timestamps
    await DFns.sleep(timedelta(seconds=0.5))
    return (idx, datetime.now())

@DFns.durable()
async def orchestrator():
    t1 = limited_task(1)
    t2 = limited_task(2)
    t3 = limited_task(3)
    return await DFns.gather(t1, t2, t3)

@DFns.durable(max_concurrent=1)
async def serial_task(idx: int):
    await DFns.sleep(timedelta(seconds=0.2))
    return ("serial", idx, datetime.now())

@DFns.durable(max_concurrent=5)
async def parallel_task(idx: int):
    await DFns.sleep(timedelta(seconds=0.2))
    return ("parallel", idx, datetime.now())

@DFns.durable()
async def mixed_workflow():
    return await DFns.gather(
        serial_task(1), serial_task(2),
        parallel_task(1), parallel_task(2)
    )

class TestRateLimit(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_id = str(uuid.uuid4())
        self.backend = get_test_backend(self.test_id)
        await self.backend.init_db()
        self.executor = DFns(backend=self.backend)
        self.worker_task = asyncio.create_task(self.executor.serve(poll_interval=0.1, max_concurrency=10))

    async def asyncTearDown(self):
        self.worker_task.cancel()
        try:
            await self.worker_task
        except asyncio.CancelledError:
            pass
        await cleanup_test_backend(self.backend)

    async def test_max_concurrent_limit(self):
        exec_id = await self.executor.schedule(timedelta(seconds=0), orchestrator)
        result_wrapper = await self.executor.wait_for(exec_id, expiry=15)
        result = result_wrapper.or_raise()
        
        # Result is list of (idx, timestamp)
        # Sort by idx just in case gather returns in order (it does usually)
        # But we want to check timestamps
        
        # Sort by completion time
        # Handle string timestamps from JSON serialization
        sorted_res = sorted(result, key=lambda x: datetime.fromisoformat(x[1]) if isinstance(x[1], str) else x[1])
        
        start_times = [datetime.fromisoformat(x[1]) if isinstance(x[1], str) else x[1] for x in sorted_res]
        
        # Check gaps. Since each task takes 0.5s and max_concurrent=1,
        # completion times should be at least 0.5s apart.
        for i in range(len(start_times) - 1):
            diff = (start_times[i+1] - start_times[i]).total_seconds()
            print(f"Diff {i}: {diff}")
            # Allow some slack, but should be close to 0.5
            # If parallel, diff would be small (e.g. 0.05)
            self.assertGreaterEqual(diff, 0.4, f"Tasks completed too close together: {diff}s. Expected sequential execution.")

    async def test_mixed_concurrency(self):
        exec_id = await self.executor.schedule(timedelta(0), mixed_workflow)
        result_wrapper = await self.executor.wait_for(exec_id, expiry=15)
        result = result_wrapper.or_raise()
        
        # Convert timestamps
        def convert(item):
            ts = item[2]
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts)
            return [item[0], item[1], ts] # Use list as original might be list
            
        result = [convert(x) for x in result]
        
        serial_res = sorted([x for x in result if x[0] == "serial"], key=lambda x: x[2])
        parallel_res = sorted([x for x in result if x[0] == "parallel"], key=lambda x: x[2])
        
        self.assertEqual(len(serial_res), 2)
        self.assertEqual(len(parallel_res), 2)
        
        # Serial tasks: completion times separated by ~0.2s
        diff_serial = (serial_res[1][2] - serial_res[0][2]).total_seconds()
        print(f"Diff serial: {diff_serial}")
        self.assertGreaterEqual(diff_serial, 0.15)
        
        # Parallel tasks: completion times close (both start at same time, sleep 0.2s, finish at same time)
        diff_parallel = abs((parallel_res[1][2] - parallel_res[0][2]).total_seconds())
        print(f"Diff parallel: {diff_parallel}")
        self.assertLess(diff_parallel, 0.15)