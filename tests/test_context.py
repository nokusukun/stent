import asyncio
import os
import unittest

from senpuki import Senpuki
from tests.utils import get_test_backend, cleanup_test_backend, clear_test_backend


@Senpuki.durable()
async def context_bar():
    ctx = Senpuki.context()
    ctx.counters("progress").add(1)
    ctx.counters("abc").add(40)
    ctx.state("phase").set("bar_done")
    await asyncio.sleep(0)


@Senpuki.durable()
async def context_foo():
    ctx = Senpuki.context(
        counters={
            "progress": 0,
            "volume": 30,
        },
        state={
            "owner": "foo",
        },
    )
    ctx.state("status").set("running")
    await context_bar()
    return "ok"


class TestExecutionContext(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = get_test_backend(f"ctx_{os.getpid()}_{id(self)}")
        await self.backend.init_db()
        await clear_test_backend(self.backend)
        self.executor = Senpuki(backend=self.backend)
        self.worker_task = asyncio.create_task(self.executor.serve(poll_interval=0.05))

    async def asyncTearDown(self):
        self.worker_task.cancel()
        try:
            await self.worker_task
        except asyncio.CancelledError:
            pass
        await self.executor.shutdown()
        await cleanup_test_backend(self.backend)

    async def test_context_counters_and_custom_state(self):
        exec_id = await self.executor.dispatch(context_foo)
        result = await self.executor.wait_for(exec_id, expiry=5.0)
        self.assertTrue(result.ok)

        state = await self.executor.state_of(exec_id)
        self.assertEqual(state.counters["progress"], 1)
        self.assertEqual(state.counters["volume"], 30)
        self.assertEqual(state.counters["abc"], 40)

        self.assertEqual(state.custom_state["owner"], "foo")
        self.assertEqual(state.custom_state["status"], "running")
        self.assertEqual(state.custom_state["phase"], "bar_done")
