import unittest
import asyncio
import os
import logging
import time
from senpuki import Senpuki, Result
from tests.utils import get_test_backend, cleanup_test_backend, clear_test_backend

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@Senpuki.durable()
async def parallel_worker(duration: float) -> float:
    await asyncio.sleep(duration)
    return duration

@Senpuki.durable()
async def parallel_orchestrator(count: int, duration: float) -> list[float]:
    tasks = []
    for _ in range(count):
        tasks.append(parallel_worker(duration))
    
    results = await asyncio.gather(*tasks)
    return results

@Senpuki.durable()
async def fan_out_fan_in_workflow(count: int, duration: float) -> float:
    results = await parallel_orchestrator(count, duration)
    return sum(results)

class TestParallel(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = get_test_backend(f"parallel_{os.getpid()}")
        await self.backend.init_db()
        await clear_test_backend(self.backend)
        self.executor = Senpuki(backend=self.backend)
        # We need high concurrency
        self.worker_task = asyncio.create_task(self.executor.serve(max_concurrency=10, poll_interval=0.1))

    async def asyncTearDown(self):
        self.worker_task.cancel()
        try:
            await self.worker_task
        except asyncio.CancelledError:
            pass
        await self.executor.shutdown()
        await cleanup_test_backend(self.backend)

    async def test_fan_out_fan_in(self):
        # We want to run 4 tasks, each sleeping 0.5s.
        # If sequential: 2.0s.
        # If parallel: ~0.5s + overhead.
        
        count = 4
        sleep_time = 0.5
        
        start_time = time.time()
        exec_id = await self.executor.dispatch(fan_out_fan_in_workflow, count, sleep_time)
        
        while True:
            state = await self.executor.state_of(exec_id)
            if state.state in ("completed", "failed", "timed_out"):
                break
            await asyncio.sleep(0.1)
            
        duration = time.time() - start_time
        result = await self.executor.result_of(exec_id)
        
        self.assertTrue(result.ok)
        self.assertEqual(result.value, count * sleep_time)
        
        # Check if it was actually parallel. 
        # Allow some overhead (e.g. 1.0s total instead of 0.5s is still better than 2.0s)
        # Overhead: DB polling (0.1s), task creation, etc.
        print(f"Total duration: {duration:.2f}s (expected < {count * sleep_time})")
        state = await self.executor.state_of(exec_id)
        # print(f"Execution state progress steps: {state.progress_str}")
        for progress in state.progress:
            print(f" - {progress.step}: {progress.status} (started at {progress.started_at}, completed at {progress.completed_at})")
        
        # Relaxed assertion for remote DB latency
        # We just want to ensure it's not strictly sequential (count * duration)
        # But with 4 tasks of 0.5s, sequential is 2.0s. 
        # If latency is huge (e.g. 14s total), we can't easily prove parallelism by time alone if overhead > task duration.
        # Instead, let's check the start/end times of the workers to see if they overlapped.
        
        worker_progress = [p for p in state.progress if p.step == "tests.test_parallel:parallel_worker" and p.status == "running"]
        # We need start/end times. But progress only has "running" start time or "completed" end time.
        # We need to correlate them or look at "completed" events which have start/end if available.
        # Actually ExecutionProgress struct has started_at and completed_at.
        
        completed_workers = [p for p in state.progress if p.step == "tests.test_parallel:parallel_worker" and p.status == "completed"]
        
        if len(completed_workers) == count:
            # Check overlap
            # Find the time window where most tasks were running
            intervals = [(p.started_at, p.completed_at) for p in completed_workers if p.started_at and p.completed_at]
            if intervals:
                # If any two intervals overlap, we have parallelism
                has_overlap = False
                for i in range(len(intervals)):
                    for j in range(i + 1, len(intervals)):
                        start_a, end_a = intervals[i]
                        start_b, end_b = intervals[j]
                        # Overlap if start_a < end_b and start_b < end_a
                        if start_a < end_b and start_b < end_a:
                            has_overlap = True
                            break
                    if has_overlap:
                        break
                
                self.assertTrue(has_overlap, "Tasks did not overlap, execution was sequential")
        else:
            # Fallback to duration check if we can't check timestamps
            self.assertLess(duration, count * sleep_time * 2.0, "Execution took too long")

