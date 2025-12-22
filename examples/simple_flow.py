import asyncio
import os
import logging
from senpuki import Senpuki, Result, RetryPolicy

logging.basicConfig(level=logging.INFO)

# Define functions
@Senpuki.durable()
async def add(x: int, y: int) -> int:
    print(f"Executing add({x}, {y})")
    await asyncio.sleep(0.5)
    return x + y

@Senpuki.durable()
async def main_flow(start_val: int) -> Result[int, Exception]:
    print(f"Executing main_flow({start_val})")
    # Calling durable function directly
    r1 = await add(start_val, 10)
    # Chain another
    r2 = await add(r1, 5)
    return Result.Ok(r2)

async def run():
    db_path = "senpuki.sqlite"
    if os.path.exists(db_path):
        os.remove(db_path)
        
    backend = Senpuki.backends.SQLiteBackend(db_path)
    # We need to initialize the DB. The interface doesn't expose init_db strictly 
    # but SQLiteBackend has it.
    await backend.init_db()
    
    executor = Senpuki(backend=backend)
    
    # Start worker
    print("Starting worker...")
    worker_task = asyncio.create_task(executor.serve(poll_interval=0.5))
    
    # Dispatch
    print("Dispatching workflow...")
    execution_id = await executor.dispatch(main_flow, 100)
    print(f"Dispatched execution ID: {execution_id}")
    
    # Wait for result
    while True:
        state = await executor.state_of(execution_id)
        print(f"Execution State: {state.state}")
        print(f"Progress: {state.progress_str}")
        
        if state.state in ("completed", "failed"):
            break
        await asyncio.sleep(1)
        
    result = await executor.result_of(execution_id)
    print(f"Final Result: {result}")
    
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass

if __name__ == "__main__":
    asyncio.run(run())
