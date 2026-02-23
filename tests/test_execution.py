import unittest
import asyncio
import os
import shutil
import contextlib
import uuid
from datetime import datetime, timedelta
from stent import Stent, Result, RetryPolicy
from stent.executor import UnregisteredFunctionError, _original_sleep
from stent.registry import registry, FunctionRegistry, FunctionMetadata
from tests.utils import get_test_backend, cleanup_test_backend, clear_test_backend

# Define some test functions globally so pickle/registry can find them
@Stent.durable()
async def simple_task(x: int) -> int:
    return x * 2

@Stent.durable()
async def failing_task():
    raise ValueError("I failed")

@Stent.durable(retry_policy=RetryPolicy(max_attempts=3, initial_delay=0.01, backoff_factor=1.0))
async def retryable_task(succeed_on_attempt: int):
    pass

ATTEMPT_COUNTER = {}
RECOVERY_TEST_STATE = {"first_run": True}
DELAYED_RETRY_COUNTER = {}
FALSEY_IDEMPOTENT_COUNTER = {}

@Stent.durable()
async def recovery_task():
    # If it's the first run (simulated crash), we sleep to allow cancellation
    if RECOVERY_TEST_STATE["first_run"]:
        RECOVERY_TEST_STATE["first_run"] = False
        await _original_sleep(10)  # real sleep to simulate slow work for crash test
    return "recovered"

@Stent.durable(retry_policy=RetryPolicy(max_attempts=4, initial_delay=0.01))
async def stateful_retry_task(exec_id_for_counter: str):
    count = ATTEMPT_COUNTER.get(exec_id_for_counter, 0) + 1
    ATTEMPT_COUNTER[exec_id_for_counter] = count
    if count < 3:
        raise ValueError(f"Fail attempt {count}")
    return count


@Stent.durable(retry_policy=RetryPolicy(max_attempts=2, initial_delay=1.0, backoff_factor=1.0))
async def delayed_retry_task(case_id: str):
    count = DELAYED_RETRY_COUNTER.get(case_id, 0) + 1
    DELAYED_RETRY_COUNTER[case_id] = count
    if count == 1:
        raise ValueError("fail once")
    return count


@Stent.durable(idempotent=True)
async def falsey_idempotent_task(case_id: str, value):
    FALSEY_IDEMPOTENT_COUNTER[case_id] = FALSEY_IDEMPOTENT_COUNTER.get(case_id, 0) + 1
    return value


@Stent.durable()
async def falsey_idempotent_workflow(case_id: str, value):
    return await falsey_idempotent_task(case_id, value)

@Stent.durable(queue="high_priority_queue", tags=["data_processing"])
async def high_priority_data_task(data: str) -> str:
    return f"Processed {data} with high priority"

@Stent.durable(queue="low_priority_queue", tags=["reporting"])
async def low_priority_report_task(report_id: str) -> str:
    return f"Generated report {report_id}"


@Stent.durable(tags=["alpha"])
async def alpha_tagged_task(value: str) -> str:
    return f"alpha:{value}"


@Stent.durable(tags=["beta"])
async def beta_tagged_task(value: str) -> str:
    return f"beta:{value}"


@Stent.durable()
async def untagged_task(value: str) -> str:
    return f"untagged:{value}"

LONG_TASK_INVOCATIONS = 0

@Stent.durable()
async def guarded_long_activity(duration: float) -> int:
    global LONG_TASK_INVOCATIONS
    LONG_TASK_INVOCATIONS += 1
    await asyncio.sleep(duration)
    return LONG_TASK_INVOCATIONS


async def registry_isolated_workflow(value: int) -> int:
    return value + 5

DLQ_REPLAY_ATTEMPTS: dict[str, int] = {}

@Stent.durable(retry_policy=RetryPolicy(max_attempts=1, initial_delay=0.01))
async def flaky_once_task(key: str) -> str:
    attempt = DLQ_REPLAY_ATTEMPTS.get(key, 0)
    DLQ_REPLAY_ATTEMPTS[key] = attempt + 1
    if attempt == 0:
        raise RuntimeError("boom")
    return f"ok-{attempt}"


class TestExecution(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = get_test_backend(f"{os.getpid()}_{id(self)}")
        await self.backend.init_db()
        await clear_test_backend(self.backend)
        self.executor = Stent(backend=self.backend)
        self.worker_task = asyncio.create_task(self.executor.serve(poll_interval=0.1))

    async def asyncTearDown(self):
        self.worker_task.cancel()
        try:
            await self.worker_task
        except asyncio.CancelledError:
            pass
        await self.executor.shutdown()
        await cleanup_test_backend(self.backend)
            
    async def test_simple_execution(self):
        exec_id = await self.executor.dispatch(simple_task, 21)
        result = await self._wait_for_result(exec_id)
        self.assertEqual(result.value, 42)

    async def test_dispatch_requires_registered_function(self):
        async def local_function():
            return "nope"

        with self.assertRaises(UnregisteredFunctionError):
            await self.executor.dispatch(local_function)

    async def test_executor_can_use_custom_registry(self):
        with self.assertRaises(UnregisteredFunctionError):
            await self.executor.dispatch(registry_isolated_workflow, 2)

        custom_registry = FunctionRegistry()
        custom_registry.register(
            FunctionMetadata(
                name=custom_registry.name_for_function(registry_isolated_workflow),
                fn=registry_isolated_workflow,
                cached=False,
                retry_policy=RetryPolicy(),
                tags=[],
                priority=0,
                queue=None,
                idempotent=False,
                idempotency_key_func=None,
                version=None,
            )
        )

        self.worker_task.cancel()
        try:
            await self.worker_task
        except asyncio.CancelledError:
            pass

        custom_executor = Stent(backend=self.backend, function_registry=custom_registry)
        custom_worker = asyncio.create_task(custom_executor.serve(poll_interval=0.05))

        try:
            exec_id = await custom_executor.dispatch(registry_isolated_workflow, 5)
            result = await custom_executor.wait_for(exec_id, expiry=5.0)
            self.assertEqual(result.value, 10)
        finally:
            custom_worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await custom_worker
            await custom_executor.shutdown()
        
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

    async def test_retry_sets_scheduled_for_and_not_lease_expiry(self):
        case_id = str(uuid.uuid4())
        DELAYED_RETRY_COUNTER[case_id] = 0

        exec_id = await self.executor.dispatch(delayed_retry_task, case_id)

        pending_retry_seen = False
        for _ in range(80):
            tasks = await self.backend.list_tasks_for_execution(exec_id)
            root_task = next((t for t in tasks if t.kind == "orchestrator"), None)
            if root_task and root_task.retries == 1 and root_task.state == "pending":
                pending_retry_seen = True
                scheduled_for = root_task.scheduled_for
                self.assertIsNotNone(scheduled_for)
                self.assertIsNone(root_task.lease_expires_at)
                if scheduled_for is None:
                    self.fail("scheduled_for should be set for retry delay")
                self.assertGreater(scheduled_for.timestamp(), datetime.now().timestamp())
                await asyncio.sleep(0.15)
                root_task_after = await self.backend.get_task(root_task.id)
                self.assertIsNotNone(root_task_after)
                if root_task_after is None:
                    self.fail("Task unexpectedly missing")
                self.assertEqual(root_task_after.state, "pending")
                break
            await asyncio.sleep(0.05)

        self.assertTrue(pending_retry_seen, "Did not observe task in delayed retry pending state")

        result = await self.executor.wait_for(exec_id, expiry=10.0)
        self.assertEqual(result.or_raise(), 2)

    async def test_idempotency_reuses_falsey_results(self):
        cases = [
            ("zero", 0),
            ("false", False),
            ("empty-str", ""),
            ("empty-list", []),
            ("empty-dict", {}),
        ]

        for case_id, value in cases:
            FALSEY_IDEMPOTENT_COUNTER[case_id] = 0

            first_exec = await self.executor.dispatch(falsey_idempotent_workflow, case_id, value)
            first_result = await self.executor.wait_for(first_exec, expiry=5.0)
            self.assertEqual(first_result.or_raise(), value)

            second_exec = await self.executor.dispatch(falsey_idempotent_workflow, case_id, value)
            second_result = await self.executor.wait_for(second_exec, expiry=5.0)
            self.assertEqual(second_result.or_raise(), value)

            self.assertEqual(FALSEY_IDEMPOTENT_COUNTER[case_id], 1)

    async def test_replay_dead_letter(self):
        key = str(uuid.uuid4())
        exec_id = await self.executor.dispatch(flaky_once_task, key)

        while True:
            state = await self.executor.state_of(exec_id)
            if state.state in ("failed", "completed"):
                break
            await asyncio.sleep(0.05)

        self.assertEqual(state.state, "failed")
        letters = await self.executor.list_dead_letters()
        self.assertEqual(len(letters), 1)
        replayed_id = await self.executor.replay_dead_letter(letters[0].id)
        self.assertNotEqual(replayed_id, letters[0].id)
        result = await self.executor.wait_for(exec_id, expiry=5.0)
        self.assertTrue(result.ok)
        value = result.or_raise()
        self.assertIsInstance(value, str)
        self.assertIn("ok-", value)
        self.assertEqual(await self.executor.list_dead_letters(), [])

    async def test_queue_and_tags_filtering(self):
        # Stop default worker FIRST to prevent it from stealing tasks
        self.worker_task.cancel()
        try:
            await self.worker_task
        except asyncio.CancelledError:
            pass
            
        # Ensure any background tasks from default worker are done (though none should be there)
        await self.executor.shutdown()

        # Let's create tasks for different queues
        hp_exec_id = await self.executor.dispatch(high_priority_data_task, "important_data")
        lp_exec_id = await self.executor.dispatch(low_priority_report_task, "monthly_report")

        # Start a worker only for high_priority_queue
        hp_executor = Stent(backend=self.backend)
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
        await hp_executor.shutdown()

        # Start a worker for low_priority_queue
        lp_executor = Stent(backend=self.backend)
        lp_worker_task = asyncio.create_task(lp_executor.serve(queues=["low_priority_queue"], poll_interval=0.1))
        
        lp_result = await self._wait_for_result(lp_exec_id)
        self.assertEqual(lp_result.value, "Generated report monthly_report")

        lp_worker_task.cancel()
        try:
            await lp_worker_task
        except asyncio.CancelledError:
            pass
        await lp_executor.shutdown()

    async def test_worker_tags_filtering(self):
        # Stop default worker so only explicitly configured workers consume tasks.
        self.worker_task.cancel()
        try:
            await self.worker_task
        except asyncio.CancelledError:
            pass
        await self.executor.shutdown()

        alpha_exec_id = await self.executor.dispatch(alpha_tagged_task, "A")
        beta_exec_id = await self.executor.dispatch(beta_tagged_task, "B")
        untagged_exec_id = await self.executor.dispatch(untagged_task, "U")

        alpha_executor = Stent(backend=self.backend)
        alpha_worker_task = asyncio.create_task(alpha_executor.serve(tags=["alpha"], poll_interval=0.1))

        try:
            alpha_result = await self._wait_for_result(alpha_exec_id)
            self.assertEqual(alpha_result.value, "alpha:A")

            untagged_result = await self._wait_for_result(untagged_exec_id)
            self.assertEqual(untagged_result.value, "untagged:U")

            beta_state = await self.executor.state_of(beta_exec_id)
            self.assertEqual(beta_state.state, "pending")
        finally:
            alpha_worker_task.cancel()
            try:
                await alpha_worker_task
            except asyncio.CancelledError:
                pass
            await alpha_executor.shutdown()

        beta_executor = Stent(backend=self.backend)
        beta_worker_task = asyncio.create_task(beta_executor.serve(tags=["beta"], poll_interval=0.1))

        try:
            beta_result = await self._wait_for_result(beta_exec_id)
            self.assertEqual(beta_result.value, "beta:B")
        finally:
            beta_worker_task.cancel()
            try:
                await beta_worker_task
            except asyncio.CancelledError:
                pass
            await beta_executor.shutdown()

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
        if target_task:
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

    async def test_lease_renewal_prevents_duplicate_execution(self):
        global LONG_TASK_INVOCATIONS
        LONG_TASK_INVOCATIONS = 0

        # Stop default worker to configure heartbeat workers
        self.worker_task.cancel()
        try:
            await self.worker_task
        except asyncio.CancelledError:
            pass

        lease_duration = timedelta(seconds=0.3)
        heartbeat_interval = timedelta(seconds=0.1)

        worker_exec1 = Stent(backend=self.backend)
        worker_exec2 = Stent(backend=self.backend)
        worker1 = asyncio.create_task(
            worker_exec1.serve(
                lease_duration=lease_duration,
                heartbeat_interval=heartbeat_interval,
                poll_interval=0.05,
            )
        )
        worker2 = asyncio.create_task(
            worker_exec2.serve(
                lease_duration=lease_duration,
                heartbeat_interval=heartbeat_interval,
                poll_interval=0.05,
            )
        )

        try:
            exec_id = await self.executor.dispatch(guarded_long_activity, 0.8)
            result = await self._wait_for_result(exec_id)
            self.assertEqual(result.value, 1)
            self.assertEqual(LONG_TASK_INVOCATIONS, 1)
        finally:
            worker1.cancel()
            worker2.cancel()
            for t in (worker1, worker2):
                try:
                    await t
                except asyncio.CancelledError:
                    pass
            await worker_exec1.shutdown()
            await worker_exec2.shutdown()

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
        if rec_pending is None:
            self.fail("Pending execution should still exist")
        self.assertEqual(rec_pending.state, "pending")


    async def _wait_for_result(self, exec_id):
        while True:
            state = await self.executor.state_of(exec_id)
            if state.state in ("completed", "failed", "timed_out"):
                break
            await asyncio.sleep(0.1)
        return await self.executor.result_of(exec_id)


class TestAutoDispatch(unittest.IsolatedAsyncioTestCase):
    """Tests for the Stent.use() / auto-dispatch feature."""

    async def asyncSetUp(self):
        self.backend = get_test_backend(f"autodispatch_{os.getpid()}_{id(self)}")
        await self.backend.init_db()
        await clear_test_backend(self.backend)

    async def asyncTearDown(self):
        Stent.reset()
        await cleanup_test_backend(self.backend)

    def test_init_stores_default_executor(self):
        self.assertIsNone(Stent._default_executor)
        executor = Stent.use(backend=self.backend)
        self.assertIs(Stent._default_executor, executor)

    def test_reset_clears_default_executor(self):
        Stent.use(backend=self.backend)
        self.assertIsNotNone(Stent._default_executor)
        Stent.reset()
        self.assertIsNone(Stent._default_executor)

    async def test_auto_dispatch_returns_result(self):
        """Calling a durable function with a default executor auto-dispatches and returns the result."""
        executor = Stent.use(backend=self.backend)
        worker = asyncio.create_task(executor.serve(poll_interval=0.05))
        try:
            result = await simple_task(21)
            self.assertEqual(result, 42)
        finally:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
            await executor.shutdown()

    async def test_auto_dispatch_propagates_errors(self):
        """Errors from auto-dispatched functions are re-raised to the caller."""
        executor = Stent.use(backend=self.backend)
        worker = asyncio.create_task(executor.serve(poll_interval=0.05))
        try:
            with self.assertRaises(Exception):
                await failing_task()
        finally:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
            await executor.shutdown()

    async def test_no_default_executor_runs_locally(self):
        """Without a default executor, durable functions still run locally."""
        self.assertIsNone(Stent._default_executor)
        result = await simple_task(10)
        self.assertEqual(result, 20)
