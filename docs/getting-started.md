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
from stent import Stent

# 1. Define durable functions (parens are optional)
@Stent.durable
async def greet(name: str) -> str:
    return f"Hello, {name}!"

@Stent.durable
async def greeting_workflow(names: list[str]) -> list[str]:
    greetings = []
    for name in names:
        greeting = await greet(name)
        greetings.append(greeting)
    return greetings

# 2. Run everything
async def main():
    backend = Stent.backends.SQLiteBackend("stent.db")

    async with Stent.bootstrap(backend, serve=True) as executor:
        result = await greeting_workflow(["Alice", "Bob", "Charlie"])
        print(result)
        # Output: ['Hello, Alice!', 'Hello, Bob!', 'Hello, Charlie!']

if __name__ == "__main__":
    asyncio.run(main())
```

### What `Stent.bootstrap()` does

`Stent.bootstrap()` is a convenience context manager that:

1. Calls `backend.init_db()` to set up tables
2. Sets up the default executor via `Stent.use()` (enabling auto-dispatch)
3. Optionally starts a background worker (with `serve=True`)
4. Waits for the worker to be ready
5. Cleans everything up on exit

For more control, you can do each step manually:

```python
async def main():
    backend = Stent.backends.SQLiteBackend("stent.db")
    await backend.init_db()
    executor = Stent.use(backend=backend)

    worker_task = asyncio.create_task(executor.serve())
    await executor.wait_until_ready()

    exec_id = await executor.dispatch(greeting_workflow, ["Alice", "Bob", "Charlie"])
    result = await executor.wait_for(exec_id)
    print(result.unwrap())

    worker_task.cancel()
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
    await asyncio.sleep(0.5)
    return {"user_id": user_id, "name": f"User {user_id}", "email": f"{user_id}@example.com"}

@Stent.durable
async def send_notification(user: dict, message: str) -> bool:
    """Send a notification to a user."""
    await asyncio.sleep(0.3)
    print(f"Sent to {user['email']}: {message}")
    return True

@Stent.durable(cached=True)
async def generate_report(data: list[dict]) -> str:
    """Generate a report from data. Results are cached."""
    await asyncio.sleep(1.0)
    return f"Report with {len(data)} items"
```

### Step 2: Define the Orchestrator

The orchestrator coordinates the activities:

```python
@Stent.durable
async def notification_workflow(user_ids: list[str], message: str) -> dict:
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

    return {
        "users_processed": len(users),
        "notifications_sent": sent_count,
        "report": report
    }
```

### Step 3: Run the Workflow

```python
async def main():
    backend = Stent.backends.SQLiteBackend("notification_demo.db")

    async with Stent.bootstrap(backend, serve=True) as executor:
        result = await notification_workflow(
            ["user-1", "user-2", "user-3"],
            "Welcome to Stent!"
        )
        print(f"Result: {result}")

if __name__ == "__main__":
    asyncio.run(main())
```

## Understanding What Happened

When you run the workflow:

1. **Bootstrap**: `Stent.bootstrap()` initialises the DB, sets up auto-dispatch, and starts a worker
2. **Auto-dispatch**: Calling `await notification_workflow(...)` auto-dispatches to the database
3. **Worker picks up**: The worker claims the orchestrator task
4. **Orchestration**: As the orchestrator calls activities, child tasks are created
5. **Persistence**: Each activity's result is stored in the database
6. **Completion**: When all activities complete, the orchestrator returns its result

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
async with Stent.bootstrap(backend, serve=True) as executor:
    ...
```

### Adding Redis for Low-Latency Notifications

```python
async with Stent.bootstrap(
    backend,
    serve=True,
    notification_backend=Stent.notifications.RedisBackend("redis://localhost:6379"),
) as executor:
    ...
```

### Setting Workflow Timeouts

```python
exec_id = await executor.dispatch(
    my_workflow,
    arg1, arg2,
    expiry="1h"  # or max_duration="1h"
)
```

### Scheduling Delayed Execution

```python
exec_id = await executor.dispatch(
    my_workflow,
    arg1,
    delay="30m"
)
```

### Using Durable Sleep

```python
@Stent.durable
async def workflow_with_wait():
    await do_first_step()

    # asyncio.sleep >= 1s automatically becomes durable inside executions
    await asyncio.sleep(3600)  # worker is free during this time

    # Or use explicit durable sleep with string durations
    await Stent.sleep("1h")

    await do_second_step()
```

### Cancelling an Execution

```python
exec_id = await executor.dispatch(long_running_workflow)
# ... later
await executor.cancel(exec_id)
```

### Testing with Fixtures

```python
# conftest.py
pytest_plugins = ["stent.testing"]

# test_my_workflow.py
async def test_my_workflow(stent_worker):
    # stent_worker provides an executor with a running worker
    result = await my_durable_fn(42)
    assert result == expected_value
```
