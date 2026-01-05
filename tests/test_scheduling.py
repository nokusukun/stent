import unittest
import asyncio
import os
from datetime import datetime, timedelta
import senpuki # import the package to access senpuki.sleep
from senpuki.executor import Senpuki, Backends
from senpuki.registry import registry
from senpuki.utils.time import parse_duration
from tests.utils import get_test_backend, cleanup_test_backend, clear_test_backend

# Define some tasks
@Senpuki.durable()
async def quick_task(x: int):
    return x * 2

@Senpuki.durable()
async def sleeping_workflow():
    await senpuki.sleep("1s")
    return "done"

class TestScheduling(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = get_test_backend(f"scheduling_{os.getpid()}")
        await self.backend.init_db()
        await clear_test_backend(self.backend)
        self.senpuki = Senpuki(backend=self.backend)

    async def asyncTearDown(self):
        await self.senpuki.shutdown()
        await cleanup_test_backend(self.backend)

    async def test_parse_duration_extensions(self):
        self.assertEqual(parse_duration("2d8h"), timedelta(days=2, hours=8))
        self.assertEqual(parse_duration("1m30s"), timedelta(minutes=1, seconds=30))
        self.assertEqual(parse_duration({"minutes": 5}), timedelta(minutes=5))

    async def test_schedule_delayed_execution(self):
        # Schedule to run in 2 seconds
        exec_id = await self.senpuki.schedule("2s", quick_task, 10)
        
        # Immediately check state
        state = await self.senpuki.state_of(exec_id)
        self.assertEqual(state.state, "pending")
        
        # Start a worker in background
        worker_task = asyncio.create_task(self.senpuki.serve(poll_interval=0.1))
        
        try:
            # Wait 1 second - should still be pending (not picked up)
            # We can check DB tasks
            await asyncio.sleep(1)
            # We need to verify it wasn't picked up.
            # We can list tasks.
            tasks = await self.backend.list_tasks_for_execution(exec_id)
            # The orchestrator task should be pending.
            # Depending on timing, if serve loop is fast, it checks "scheduled_for".
            # If scheduled_for > now, it won't pick it up.
            task = tasks[0]
            self.assertEqual(task.state, "pending")
            
            # Wait another 2.5 seconds (total 3.5s) to be safe with remote DB
            await asyncio.sleep(2.5)
            
            # Should be done now
            # Use wait_for to be robust
            result = await self.senpuki.wait_for(exec_id, expiry=5.0)
            self.assertEqual(result.or_raise(), 20)
            
        finally:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass

    async def test_senpuki_sleep(self):
        # We need to register sleeping_workflow manually if it's not picked up? 
        # Decorator handles it.
        
        start = datetime.now()
        exec_id = await self.senpuki.dispatch(sleeping_workflow)
        
        worker_task = asyncio.create_task(self.senpuki.serve(poll_interval=0.1))
        
        try:
            result = await self.senpuki.wait_for(exec_id, expiry=10.0) # Increased expiry
            duration = (datetime.now() - start).total_seconds()
            
            self.assertEqual(result.or_raise(), "done")
            self.assertGreater(duration, 1.0)
            
        finally:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass

if __name__ == "__main__":
    unittest.main()
