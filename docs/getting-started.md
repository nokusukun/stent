# Getting Started

This guide will walk you through installing Stent and creating your first durable workflow.

## Installation

Install Stent using pip:

```bash
pip install stent
```

Or using uv:

```bash
uv add stent
```

### Dependencies

Stent automatically installs the following dependencies:

| Package | Purpose |
|---------|---------|
| `aiosqlite` | Async SQLite support |
| `asyncpg` | Async PostgreSQL support |
| `redis` | Redis Pub/Sub notifications |

### Optional Dependencies

For OpenTelemetry tracing support, install:

```bash
pip install opentelemetry-api opentelemetry-sdk
```

## Quick Start

Here's the minimal code to get Stent running:

```python
import asyncio
from stent import Stent, Result

# 1. Define a durable function
@Stent.durable()
async def greet(name: str) -> str:
    return f"Hello, {name}!"

# 2. Define an orchestrator
@Stent.durable()
async def greeting_workflow(names: list[str]) -> Result[list[str], Exception]:
    greetings = []
    for name in names:
        greeting = await greet(name)
        greetings.append(greeting)
    return Result.Ok(greetings)

# 3. Run everything
async def main():
    # Create backend and executor
    backend = Stent.backends.SQLiteBackend("stent.db")
    await backend.init_db()
    executor = Stent(backend=backend)
    
    # Start a worker in the background
    worker_task = asyncio.create_task(executor.serve())
    
    # Dispatch the workflow
    execution_id = await executor.dispatch(greeting_workflow, ["Alice", "Bob", "Charlie"])
    print(f"Started execution: {execution_id}")
    
    # Wait for the result
    result = await executor.wait_for(execution_id)
    print(f"Result: {result.value}")
    # Output: Result: ['Hello, Alice!', 'Hello, Bob!', 'Hello, Charlie!']
    
    # Cleanup
    worker_task.cancel()

if __name__ == "__main__":
    asyncio.run(main())
```

## Your First Workflow

Let's build a more realistic workflow that demonstrates key Stent features.

### Step 1: Define Activities

Activities are the leaf-node functions that do actual work:

```python
import asyncio
from stent import Stent, RetryPolicy

# Activity with retry policy for unreliable operations
@Stent.durable(
    retry_policy=RetryPolicy(
        max_attempts=3,
        initial_delay=1.0,
        backoff_factor=2.0
    )
)
async def fetch_user_data(user_id: str) -> dict:
    """Fetch user data from an external service."""
    # Simulate API call
    await asyncio.sleep(0.5)
    return {"user_id": user_id, "name": f"User {user_id}", "email": f"{user_id}@example.com"}

@Stent.durable()
async def send_notification(user: dict, message: str) -> bool:
    """Send a notification to a user."""
    await asyncio.sleep(0.3)
    print(f"Sent to {user['email']}: {message}")
    return True

@Stent.durable(cached=True)
async def generate_report(data: list[dict]) -> str:
    """Generate a report from data. Results are cached."""
    await asyncio.sleep(1.0)  # Expensive operation
    return f"Report with {len(data)} items"
```

### Step 2: Define the Orchestrator

The orchestrator coordinates the activities:

```python
from stent import Result

@Stent.durable()
async def notification_workflow(user_ids: list[str], message: str) -> Result[dict, Exception]:
    """
    Workflow that fetches user data and sends notifications.
    """
    # Fetch all users
    users = []
    for user_id in user_ids:
        user = await fetch_user_data(user_id)
        users.append(user)
    
    # Send notifications
    sent_count = 0
    for user in users:
        success = await send_notification(user, message)
        if success:
            sent_count += 1
    
    # Generate a report (cached for efficiency)
    report = await generate_report(users)
    
    return Result.Ok({
        "users_processed": len(users),
        "notifications_sent": sent_count,
        "report": report
    })
```

### Step 3: Run the Workflow

```python
import asyncio
import os

async def main():
    # Clean start for demo
    db_path = "notification_demo.db"
    if os.path.exists(db_path):
        os.remove(db_path)
    
    # Initialize
    backend = Stent.backends.SQLiteBackend(db_path)
    await backend.init_db()
    executor = Stent(backend=backend)
    
    # Start worker with configuration
    worker = asyncio.create_task(
        executor.serve(
            worker_id="worker-1",
            max_concurrency=10,
            poll_interval=0.5
        )
    )
    
    # Dispatch workflow
    exec_id = await executor.dispatch(
        notification_workflow,
        ["user-1", "user-2", "user-3"],
        "Welcome to Stent!"
    )
    
    # Monitor progress
    while True:
        state = await executor.state_of(exec_id)
        print(f"State: {state.state}")
        
        if state.state in ("completed", "failed", "timed_out"):
            break
        await asyncio.sleep(0.5)
    
    # Get result
    result = await executor.result_of(exec_id)
    if result.ok:
        print(f"Success: {result.value}")
    else:
        print(f"Failed: {result.error}")
    
    worker.cancel()

if __name__ == "__main__":
    asyncio.run(main())
```

## Understanding What Happened

When you run the workflow:

1. **Dispatch**: `executor.dispatch()` creates an execution record and a root task in the database
2. **Worker picks up**: The worker claims the orchestrator task
3. **Orchestration**: As the orchestrator calls activities, child tasks are created
4. **Persistence**: Each activity's result is stored in the database
5. **Completion**: When all activities complete, the orchestrator returns its result

If the worker crashes at any point:

- The task lease expires after 5 minutes (configurable)
- Another worker (or the same one restarted) claims the task
- Execution continues from where it left off
- Completed activities aren't re-executed (results are persisted)

## Next Steps

- [Core Concepts](core-concepts.md) - Understand the fundamentals
- [Durable Functions Guide](guides/durable-functions.md) - Learn all decorator options
- [Error Handling](guides/error-handling.md) - Configure retries and handle failures
- [Parallel Execution](guides/parallel-execution.md) - Run tasks concurrently
- [Worker Configuration](guides/workers.md) - Set up production workers

## Common Patterns

### Using PostgreSQL

For production, use PostgreSQL instead of SQLite:

```python
backend = Stent.backends.PostgresBackend(
    "postgresql://user:password@localhost:5432/stent"
)
await backend.init_db()
executor = Stent(backend=backend)
```

### Adding Redis for Low-Latency Notifications

```python
executor = Stent(
    backend=backend,
    notification_backend=Stent.notifications.RedisBackend("redis://localhost:6379")
)
```

### Setting Workflow Timeouts

```python
# Workflow must complete within 1 hour
exec_id = await executor.dispatch(
    my_workflow,
    arg1, arg2,
    expiry="1h"  # or max_duration="1h"
)
```

### Scheduling Delayed Execution

```python
# Run workflow 30 minutes from now
exec_id = await executor.dispatch(
    my_workflow,
    arg1,
    delay="30m"
)
```

### Using Durable Sleep

```python
@Stent.durable()
async def workflow_with_wait():
    await do_first_step()
    
    # Durable sleep - worker is free during this time
    await Stent.sleep("1h")
    
    await do_second_step()
```
