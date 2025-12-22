import asyncio
import os
import logging
from senpuki import Senpuki, Result, RetryPolicy

logging.basicConfig(level=logging.INFO)

@Senpuki.durable(retry_policy=RetryPolicy(max_attempts=2, initial_delay=0.1))
async def fail_always():
    print("Executing fail_always")
    raise ValueError("Something went wrong")

@Senpuki.durable()
async def failing_flow():
    print("Executing failing_flow")
    await fail_always()

async def run():
    db_path = "senpuki_fail.sqlite"
    if os.path.exists(db_path):
        os.remove(db_path)
        
    backend = Senpuki.backends.SQLiteBackend(db_path)
    await backend.init_db()
    
    executor = Senpuki(backend=backend)
    worker_task = asyncio.create_task(executor.serve(poll_interval=0.5))
    
    execution_id = await executor.dispatch(failing_flow)
    print(f"Dispatched: {execution_id}")
    
    while True:
        state = await executor.state_of(execution_id)
        if state.state in ("completed", "failed"):
            print(f"Final State: {state.state}")
            break
        await asyncio.sleep(0.5)
        
    result = await executor.result_of(execution_id)
    print(f"Result: {result}")
    
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass

if __name__ == "__main__":
    asyncio.run(run())
