import unittest
import asyncio
import os
import logging
from senpuki import Senpuki, Result, RetryPolicy
from senpuki.registry import registry
from tests.utils import get_test_backend, cleanup_test_backend

logging.basicConfig(level=logging.DEBUG)

# Mock "LLM" Generation
@Senpuki.durable()
async def generate_text(prompt: str) -> str:
    # Simulate processing time
    # print(f"Executing generate_text for: {prompt}")
    await asyncio.sleep(0.5)
    return f"Generated text for: {prompt}"

@Senpuki.durable(cached=True)
async def get_cached_data(key: str) -> str:
    # print(f"Executing get_cached_data for: {key}")
    await asyncio.sleep(0.2) # Simulate work
    return f"Data for {key} from actual computation"

# Mock "Email" Notification (Side Effect)
SENT_EMAILS = []

@Senpuki.durable(idempotent=True) # Idempotent to avoid sending twice on retries of parent
async def send_email(recipient: str, body: str):
    SENT_EMAILS.append((recipient, body))
    return True

@Senpuki.durable()
async def llm_workflow(prompt: str, email: str) -> Result[str, Exception]:
    # Step 1: Generate
    text = await generate_text(prompt)
    
    # Step 2: Notify
    await send_email(email, text)
    
    return Result.Ok("Workflow Completed")

@Senpuki.durable()
async def parent_durable_task(value: int) -> Result[int, Exception]:
    # Calls another durable task
    res = await child_durable_task(value * 2)
    return Result.Ok(res + 1)

@Senpuki.durable()
async def child_durable_task(value: int) -> int:
    return value + 10

@Senpuki.durable()
async def step_sleep():
    await asyncio.sleep(0.5)
    return "slept"

@Senpuki.durable()
async def slow_chain_workflow():
    await step_sleep()
    await step_sleep()
    await step_sleep()
    return "done"

class TestScenarios(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = get_test_backend(f"scenarios_{os.getpid()}")
        await self.backend.init_db()
        self.executor = Senpuki(backend=self.backend)
        self.worker_task = asyncio.create_task(self.executor.serve(poll_interval=0.1))
        SENT_EMAILS.clear()

    async def asyncTearDown(self):
        self.worker_task.cancel()
        try:
            await self.worker_task
        except asyncio.CancelledError:
            pass
        await cleanup_test_backend(self.backend)

    async def test_llm_email_scenario(self):
        prompt = "Write a poem about Python"
        recipient = "user@example.com"
        
        exec_id = await self.executor.dispatch(llm_workflow, prompt, recipient)
        
        # Wait for result
        result = await self._wait_for_result(exec_id)
        
        self.assertTrue(result.ok)
        self.assertEqual(result.value, "Workflow Completed")
        
        # Verify side effects
        self.assertEqual(len(SENT_EMAILS), 1)
        self.assertEqual(SENT_EMAILS[0][0], recipient)
        self.assertIn("Generated text for: Write a poem about Python", SENT_EMAILS[0][1])

        # Verify progress tracking
        state = await self.executor.state_of(exec_id)
        steps = [p.step for p in state.progress]
        self.assertTrue(any("generate_text" in s for s in steps), f"generate_text not found in steps: {steps}")
        self.assertTrue(any("send_email" in s for s in steps), f"send_email not found in steps: {steps}")

    async def _wait_for_result(self, exec_id):
        while True:
            state = await self.executor.state_of(exec_id)
            if state.state in ("completed", "failed", "timed_out"):
                break
            await asyncio.sleep(0.1)
        return await self.executor.result_of(exec_id)