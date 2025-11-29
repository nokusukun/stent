import unittest
import asyncio
import os
from datetime import datetime, timedelta
import dfns # import the package to access dfns.sleep
from dfns.executor import DFns, Backends
from dfns.registry import registry
from dfns.utils.time import parse_duration

# Define some tasks
@DFns.durable()
async def quick_task(x: int):
    return x * 2

@DFns.durable()
async def sleeping_workflow():
    await dfns.sleep("1s")
    return "done"

class TestScheduling(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db_path = f"test_scheduling_{os.getpid()}.sqlite"
        self.backend = Backends.SQLiteBackend(self.db_path)
        await self.backend.init_db()
        self.dfns = DFns(backend=self.backend)

    async def asyncTearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    async def test_parse_duration_extensions(self):
        self.assertEqual(parse_duration("2d8h"), timedelta(days=2, hours=8))
        self.assertEqual(parse_duration("1m30s"), timedelta(minutes=1, seconds=30))
        self.assertEqual(parse_duration({"minutes": 5}), timedelta(minutes=5))

    async def test_schedule_delayed_execution(self):
        # Schedule to run in 2 seconds
        exec_id = await self.dfns.schedule("2s", quick_task, 10)
        
        # Immediately check state
        state = await self.dfns.state_of(exec_id)
        self.assertEqual(state.state, "pending")
        
        # Start a worker in background
        worker_task = asyncio.create_task(self.dfns.serve(poll_interval=0.1))
        
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
            
            # Wait another 1.5 seconds (total 2.5s)
            await asyncio.sleep(1.5)
            
            # Should be done now
            result = await self.dfns.result_of(exec_id)
            self.assertEqual(result.or_raise(), 20)
            
        finally:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass

    async def test_dfns_sleep(self):
        # We need to register sleeping_workflow manually if it's not picked up? 
        # Decorator handles it.
        
        start = datetime.now()
        exec_id = await self.dfns.dispatch(sleeping_workflow)
        
        worker_task = asyncio.create_task(self.dfns.serve(poll_interval=0.1))
        
        try:
            result = await self.dfns.wait_for(exec_id, expiry=5.0)
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
