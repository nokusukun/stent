"""Tests for DX improvements: bootstrap, Result helpers, cancel, bare decorator, readiness."""
import unittest
import asyncio
import os
from stent import Stent, Result
from stent.executor import UnregisteredFunctionError, _original_sleep
from tests.utils import get_test_backend, cleanup_test_backend, clear_test_backend


# --- bare decorator (no parens) ---

@Stent.durable
async def bare_task(x: int) -> int:
    return x + 1


@Stent.durable()
async def parens_task(x: int) -> int:
    return x + 1


@Stent.durable
async def cancel_target():
    await _original_sleep(30)
    return "should not reach"


class TestBareDurableDecorator(unittest.IsolatedAsyncioTestCase):
    async def test_bare_decorator_runs_locally(self):
        result = await bare_task(5)
        self.assertEqual(result, 6)

    async def test_parens_decorator_runs_locally(self):
        result = await parens_task(5)
        self.assertEqual(result, 6)

    async def test_bare_decorator_dispatches(self):
        backend = get_test_backend(f"bare_{os.getpid()}")
        await backend.init_db()
        await clear_test_backend(backend)
        executor = Stent.use(backend=backend)
        worker = asyncio.create_task(executor.serve(poll_interval=0.05))
        try:
            result = await bare_task(10)
            self.assertEqual(result, 11)
        finally:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
            await executor.shutdown()
            Stent.reset()
            await cleanup_test_backend(backend)


class TestResultHelpers(unittest.TestCase):
    def test_unwrap_ok(self):
        r = Result.Ok(42)
        self.assertEqual(r.unwrap(), 42)

    def test_unwrap_error(self):
        r = Result.Error(ValueError("boom"))
        with self.assertRaises(ValueError):
            r.unwrap()

    def test_or_raise_is_unwrap(self):
        r = Result.Ok("hello")
        self.assertEqual(r.or_raise(), r.unwrap())

    def test_unwrap_or(self):
        self.assertEqual(Result.Ok(10).unwrap_or(0), 10)
        self.assertEqual(Result.Error("err").unwrap_or(0), 0)

    def test_map_ok(self):
        r = Result.Ok(5).map(lambda x: x * 2)
        self.assertTrue(r.ok)
        self.assertEqual(r.value, 10)

    def test_map_error_passthrough(self):
        r = Result.Error("err").map(lambda x: x * 2)
        self.assertFalse(r.ok)
        self.assertEqual(r.error, "err")

    def test_flat_map_ok(self):
        r = Result.Ok(5).flat_map(lambda x: Result.Ok(x * 3))
        self.assertTrue(r.ok)
        self.assertEqual(r.value, 15)

    def test_flat_map_error_passthrough(self):
        r = Result.Error("err").flat_map(lambda x: Result.Ok(x * 3))
        self.assertFalse(r.ok)

    def test_bool(self):
        self.assertTrue(bool(Result.Ok(42)))
        self.assertFalse(bool(Result.Error("nope")))
        # Falsey values are still truthy Results
        self.assertTrue(bool(Result.Ok(0)))
        self.assertTrue(bool(Result.Ok("")))


class TestUnregisteredFunctionError(unittest.TestCase):
    def test_shows_available_functions(self):
        err = UnregisteredFunctionError("my.missing_fn", ["my.task_a", "my.task_b"])
        msg = str(err)
        self.assertIn("my.missing_fn", msg)
        self.assertIn("my.task_a", msg)
        self.assertIn("my.task_b", msg)

    def test_no_available_functions(self):
        err = UnregisteredFunctionError("my.missing_fn")
        msg = str(err)
        self.assertIn("my.missing_fn", msg)
        self.assertNotIn("Registered functions", msg)


class TestBootstrap(unittest.IsolatedAsyncioTestCase):
    async def test_bootstrap_without_serve(self):
        backend = get_test_backend(f"bootstrap_{os.getpid()}")
        async with Stent.bootstrap(backend) as executor:
            self.assertIs(Stent._default_executor, executor)
            # Can dispatch but no worker to process — just test setup/teardown
        self.assertIsNone(Stent._default_executor)

    async def test_bootstrap_with_serve(self):
        backend = get_test_backend(f"bootstrap_serve_{os.getpid()}")
        async with Stent.bootstrap(backend, serve=True, poll_interval=0.05) as executor:
            self.assertIs(Stent._default_executor, executor)
            result = await bare_task(7)
            self.assertEqual(result, 8)
        self.assertIsNone(Stent._default_executor)


class TestCancel(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = get_test_backend(f"cancel_{os.getpid()}_{id(self)}")
        await self.backend.init_db()
        await clear_test_backend(self.backend)
        self.executor = Stent(backend=self.backend)
        self.worker = asyncio.create_task(self.executor.serve(poll_interval=0.05))

    async def asyncTearDown(self):
        self.worker.cancel()
        try:
            await self.worker
        except asyncio.CancelledError:
            pass
        await self.executor.shutdown()
        Stent.reset()
        await cleanup_test_backend(self.backend)

    async def test_cancel_pending_execution(self):
        # Stop worker so task stays pending
        self.worker.cancel()
        try:
            await self.worker
        except asyncio.CancelledError:
            pass

        exec_id = await self.executor.dispatch(bare_task, 1)
        state = await self.executor.state_of(exec_id)
        self.assertEqual(state.state, "pending")

        await self.executor.cancel(exec_id)

        state = await self.executor.state_of(exec_id)
        self.assertEqual(state.state, "cancelled")

    async def test_cancel_already_completed(self):
        exec_id = await self.executor.dispatch(bare_task, 1)
        result = await self.executor.wait_for(exec_id, expiry=5.0)
        self.assertTrue(result.ok)

        # Cancelling a completed execution is a no-op
        await self.executor.cancel(exec_id)
        state = await self.executor.state_of(exec_id)
        self.assertEqual(state.state, "completed")

    async def test_cancel_nonexistent_raises(self):
        with self.assertRaises(ValueError):
            await self.executor.cancel("nonexistent-id")


class TestReadiness(unittest.IsolatedAsyncioTestCase):
    async def test_wait_until_ready(self):
        backend = get_test_backend(f"ready_{os.getpid()}")
        await backend.init_db()
        await clear_test_backend(backend)
        executor = Stent(backend=backend)
        worker = asyncio.create_task(executor.serve(poll_interval=0.05))
        try:
            await asyncio.wait_for(executor.wait_until_ready(), timeout=5.0)
            # If we get here, readiness was signaled
        finally:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
            await executor.shutdown()
            await cleanup_test_backend(backend)


if __name__ == "__main__":
    unittest.main()
