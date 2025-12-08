import unittest
import asyncio
import os
import shutil
from datetime import datetime, timedelta
from dfns import DFns, Result, RetryPolicy
from dfns.registry import registry
from tests.utils import get_test_backend, cleanup_test_backend

# Define some test functions globally so pickle/registry can find them
@DFns.durable()
async def simple_task(x: int) -> int:
    return x * 2

@DFns.durable()
async def failing_task():
    raise ValueError("I failed")

@DFns.durable(retry_policy=RetryPolicy(max_attempts=3, initial_delay=0.01, backoff_factor=1.0))
async def retryable_task(succeed_on_attempt: int):
    pass

ATTEMPT_COUNTER = {}
RECOVERY_TEST_STATE = {"first_run": True}

@DFns.durable()
async def recovery_task():
    # If it's the first run (simulated crash), we sleep to allow cancellation
    if RECOVERY_TEST_STATE["first_run"]:
        RECOVERY_TEST_STATE["first_run"] = False
        await asyncio.sleep(10) 
    return "recovered"

@DFns.durable(retry_policy=RetryPolicy(max_attempts=4, initial_delay=0.01))
async def stateful_retry_task(exec_id_for_counter: str):
    count = ATTEMPT_COUNTER.get(exec_id_for_counter, 0) + 1
    ATTEMPT_COUNTER[exec_id_for_counter] = count
    if count < 3:
        raise ValueError(f"Fail attempt {count}")
    return count

@DFns.durable(queue="high_priority_queue", tags=["data_processing"])
async def high_priority_data_task(data: str) -> str:
    return f"Processed {data} with high priority"

@DFns.durable(queue="low_priority_queue", tags=["reporting"])
async def low_priority_report_task(report_id: str) -> str:
    return f"Generated report {report_id}"


class TestExecution(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = get_test_backend(f"{os.getpid()}_{id(self)}")
        await self.backend.init_db()
        self.executor = DFns(backend=self.backend)
        self.worker_task = asyncio.create_task(self.executor.serve(poll_interval=0.1))

    async def asyncTearDown(self):
        self.worker_task.cancel()
        try:
            await self.worker_task
        except asyncio.CancelledError:
            pass
        await cleanup_test_backend(self.backend)
            
    async def test_simple_execution(self):
        exec_id = await self.executor.dispatch(simple_task, 21)
        result = await self._wait_for_result(exec_id)
        self.assertEqual(result.value, 42)
        
    async def test_failure_execution(self):
        exec_id = await self.executor.dispatch(failing_task)
        
        # Wait for completion
        while True:
            state = await self.executor.state_of(exec_id)
            if state.state in ("completed", "failed", "timed_out"):
                break
            await asyncio.sleep(0.1)
            
        state = await self.executor.state_of(exec_id)
        self.assertEqual(state.state, "failed")
        self.assertIn("I failed", str(state.result) if state.result else str(state))
        
    async def test_retry_logic(self):
        eid = "retry_test_1"
        ATTEMPT_COUNTER[eid] = 0
        
        exec_id = await self.executor.dispatch(stateful_retry_task, eid)
        
        result = await self._wait_for_result(exec_id)
        
        self.assertEqual(result.value, 3)
        self.assertEqual(ATTEMPT_COUNTER[eid], 3)
        
        tasks = await self.backend.list_tasks_for_execution(exec_id)
        root_task = next(t for t in tasks if t.kind == "orchestrator")
        self.assertEqual(root_task.retries, 2)

    async def test_queue_and_tags_filtering(self):
        # Worker is currently serving all queues/tags by default
        # Let's create tasks for different queues
        hp_exec_id = await self.executor.dispatch(high_priority_data_task, "important_data")
        lp_exec_id = await self.executor.dispatch(low_priority_report_task, "monthly_report")

        # Stop default worker
        self.worker_task.cancel()
        try:
            await self.worker_task
        except asyncio.CancelledError:
            pass

        # Start a worker only for high_priority_queue
        hp_executor = DFns(backend=self.backend)
        hp_worker_task = asyncio.create_task(hp_executor.serve(queues=["high_priority_queue"], poll_interval=0.1))
        
        hp_result = await self._wait_for_result(hp_exec_id)
        self.assertEqual(hp_result.value, "Processed important_data with high priority")
        
        # Verify low priority task is still pending
        lp_state = await self.executor.state_of(lp_exec_id)
        self.assertEqual(lp_state.state, "pending")

        hp_worker_task.cancel()
        try:
            await hp_worker_task
        except asyncio.CancelledError:
            pass

        # Start a worker for low_priority_queue
        lp_executor = DFns(backend=self.backend)
        lp_worker_task = asyncio.create_task(lp_executor.serve(queues=["low_priority_queue"], poll_interval=0.1))
        
        lp_result = await self._wait_for_result(lp_exec_id)
        self.assertEqual(lp_result.value, "Generated report monthly_report")

        lp_worker_task.cancel()
        try:
            await lp_worker_task
        except asyncio.CancelledError:
            pass

    async def test_lease_expiration_crash(self):
        RECOVERY_TEST_STATE["first_run"] = True
        
        # 1. Stop default worker
        self.worker_task.cancel()
        try:
            await self.worker_task
        except asyncio.CancelledError:
            pass
            
        # 2. Start worker with short lease
        short_lease = timedelta(seconds=1)
        worker1 = asyncio.create_task(self.executor.serve(lease_duration=short_lease, poll_interval=0.1))
        
        # 3. Dispatch task
        exec_id = await self.executor.dispatch(recovery_task)
        
        # 4. Wait for it to be running
        target_task: asyncio.Task | None = None
        start_wait = asyncio.get_running_loop().time()
        while asyncio.get_running_loop().time() - start_wait < 5:
            tasks = await self.backend.list_tasks_for_execution(exec_id)
            root_task = next((t for t in tasks if t.kind == "orchestrator"), None)
            
            if root_task and root_task.state == "running":
                # Try to find the python task corresponding to this
                current = asyncio.current_task()
                for t in asyncio.all_tasks():
                    if t is current or t is worker1:
                        continue
                    # Check for _handle_task in coroutine name
                    if "handle_task" in str(t) or "handle_task" in repr(t):
                        target_task = t
                        break
                if target_task:
                    break
            await asyncio.sleep(0.05)
            
        self.assertIsNotNone(target_task, "Could not find worker handler task")
        
        # 5. Simulate Crash: Cancel the handler task
        target_task.cancel()
        try:
            await target_task
        except asyncio.CancelledError:
            pass
            
        # Stop worker1 loop too
        worker1.cancel()
        try:
            await worker1
        except asyncio.CancelledError:
            pass
            
        # 6. Verify state is still "running" (simulating crash before update)
        tasks = await self.backend.list_tasks_for_execution(exec_id)
        root_task = next(t for t in tasks if t.kind == "orchestrator")
        self.assertEqual(root_task.state, "running")
        
        # 7. Wait for lease to expire
        await asyncio.sleep(1.5) 
        
        # 8. Start worker2
        worker2 = asyncio.create_task(self.executor.serve(poll_interval=0.1))
        
        # 9. Wait for result
        result = await self._wait_for_result(exec_id)
        self.assertEqual(result.value, "recovered")
        
        worker2.cancel()
        try:
            await worker2
        except asyncio.CancelledError:
            pass

    async def test_cleanup(self):
        # Stop default worker to manually control execution
        self.worker_task.cancel()
        try:
            await self.worker_task
        except asyncio.CancelledError:
            pass
            
        # 1. Run a task to completion
        # We need a worker for this
        worker = asyncio.create_task(self.executor.serve(poll_interval=0.1))
        
        exec_id = await self.executor.dispatch(simple_task, 99)
        result = await self._wait_for_result(exec_id)
        self.assertEqual(result.value, 198)
        
        # 2. Dispatch a task that will stay pending (we'll stop worker)
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
            
        exec_pending_id = await self.executor.dispatch(simple_task, 100)
        
        # 3. Cleanup with future cutoff
        # This should delete the completed task, but NOT the pending one
        cutoff = datetime.now() + timedelta(days=1)
        
        count = await self.backend.cleanup_executions(cutoff)
        self.assertGreaterEqual(count, 1)
        
        # Verify completed is gone
        rec = await self.backend.get_execution(exec_id)
        self.assertIsNone(rec)
        
        # Verify pending is present
        rec_pending = await self.backend.get_execution(exec_pending_id)
        self.assertIsNotNone(rec_pending)
        self.assertEqual(rec_pending.state, "pending")


    async def _wait_for_result(self, exec_id):
        while True:
            state = await self.executor.state_of(exec_id)
            if state.state in ("completed", "failed", "timed_out"):
                break
            await asyncio.sleep(0.1)
        return await self.executor.result_of(exec_id)