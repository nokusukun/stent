import unittest
import asyncio
import os
from datetime import datetime, timedelta
import stent # import the package to access stent.sleep
from stent.executor import Stent, Backends
from stent.registry import registry
from stent.utils.time import parse_duration
from tests.utils import get_test_backend, cleanup_test_backend, clear_test_backend

# Define some tasks
@Stent.durable()
async def quick_task(x: int):
    return x * 2

@Stent.durable()
async def sleeping_workflow():
    await stent.sleep("1s")
    return "done"

@Stent.durable()
async def asyncio_sleep_workflow():
    await asyncio.sleep(1.5)  # above threshold → becomes durable
    return "slept"

@Stent.durable()
async def short_asyncio_sleep_workflow():
    await asyncio.sleep(0.05)  # below threshold → stays real sleep
    return "quick"

class TestScheduling(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = get_test_backend(f"scheduling_{os.getpid()}")
        await self.backend.init_db()
        await clear_test_backend(self.backend)
        self.stent = Stent(backend=self.backend)

    async def asyncTearDown(self):
        await self.stent.shutdown()
        await cleanup_test_backend(self.backend)

    async def test_parse_duration_extensions(self):
        self.assertEqual(parse_duration("2d8h"), timedelta(days=2, hours=8))
        self.assertEqual(parse_duration("1m30s"), timedelta(minutes=1, seconds=30))
        self.assertEqual(parse_duration({"minutes": 5}), timedelta(minutes=5))

    async def test_schedule_delayed_execution(self):
        # Schedule to run in 2 seconds
        exec_id = await self.stent.schedule("2s", quick_task, 10)
        
        # Immediately check state
        state = await self.stent.state_of(exec_id)
        self.assertEqual(state.state, "pending")
        
        # Start a worker in background
        worker_task = asyncio.create_task(self.stent.serve(poll_interval=0.1))
        
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
            result = await self.stent.wait_for(exec_id, expiry=5.0)
            self.assertEqual(result.or_raise(), 20)
            
        finally:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass

    async def test_stent_sleep(self):
        # We need to register sleeping_workflow manually if it's not picked up? 
        # Decorator handles it.
        
        start = datetime.now()
        exec_id = await self.stent.dispatch(sleeping_workflow)
        
        worker_task = asyncio.create_task(self.stent.serve(poll_interval=0.1))
        
        try:
            result = await self.stent.wait_for(exec_id, expiry=10.0) # Increased expiry
            duration = (datetime.now() - start).total_seconds()
            
            self.assertEqual(result.or_raise(), "done")
            self.assertGreater(duration, 1.0)
            
        finally:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass

    async def test_asyncio_sleep_becomes_durable_above_threshold(self):
        """asyncio.sleep(1.5) inside a durable function should become durable sleep."""
        start = datetime.now()
        exec_id = await self.stent.dispatch(asyncio_sleep_workflow)

        worker_task = asyncio.create_task(self.stent.serve(poll_interval=0.1))
        try:
            result = await self.stent.wait_for(exec_id, expiry=10.0)
            duration = (datetime.now() - start).total_seconds()
            self.assertEqual(result.or_raise(), "slept")
            self.assertGreater(duration, 1.0)

            # Verify a sleep task was created (proof it went through durable path)
            tasks = await self.backend.list_tasks_for_execution(exec_id)
            sleep_tasks = [t for t in tasks if t.step_name == "stent.sleep"]
            self.assertEqual(len(sleep_tasks), 1)
        finally:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass

    async def test_asyncio_sleep_below_threshold_stays_real(self):
        """asyncio.sleep(0.05) inside a durable function should stay as real sleep."""
        exec_id = await self.stent.dispatch(short_asyncio_sleep_workflow)

        worker_task = asyncio.create_task(self.stent.serve(poll_interval=0.1))
        try:
            result = await self.stent.wait_for(exec_id, expiry=5.0)
            self.assertEqual(result.or_raise(), "quick")

            # Verify no sleep task was created (stayed as real asyncio.sleep)
            tasks = await self.backend.list_tasks_for_execution(exec_id)
            sleep_tasks = [t for t in tasks if t.step_name == "stent.sleep"]
            self.assertEqual(len(sleep_tasks), 0)
        finally:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    unittest.main()
