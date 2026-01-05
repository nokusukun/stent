# Senpuki: Distributed Durable Functions for Python

Senpuki is a lightweight, asynchronous, distributed task orchestration library for Python. It allows you to write stateful, reliable workflows ("durable functions") using standard Python async/await syntax. Senpuki handles the complexity of persisting state, retrying failures, and distributing work across a pool of workers.

## Table of Contents

- [Core Concepts](#core-concepts)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Features Guide](#features-guide)
    - [Defining Durable Functions](#defining-durable-functions)
    - [Orchestration & Activities](#orchestration--activities)
    - [Retries & Error Handling](#retries--error-handling)
    - [Idempotency & Caching](#idempotency--caching)
    - [Parallel Execution (Fan-out/Fan-in)](#parallel-execution-fan-outfan-in)
    - [Timeouts & Expirys](#timeouts--expirys)
- [Architecture & Backends](#architecture--backends)
- [Running Workers](#running-workers)
- [Examples](#examples)

---

## Core Concepts

*   **Durable Functions**: Python async functions decorated with `@Senpuki.durable()`. They can be orchestrators (calling other functions) or activities (doing work).
*   **Orchestrator**: A durable function that schedules other durable functions. It sleeps while waiting for sub-tasks to complete, freeing up worker resources.
*   **Activity**: A leaf-node durable function that performs a specific action (e.g., API call, DB operation).
*   **Execution**: A single run of a workflow. It has a unique ID and persistent state.
*   **Worker**: A process that polls the backend storage for pending tasks and executes them.

---

## Installation

```bash
pip install senpuki
```

**Requirements:**
*   Python 3.12+
*   `aiosqlite` (optional, for SQLite backend async support)
*   `asyncpg` (optional, for PostgreSQL backend async support)
*   `redis` (optional, for Redis notification support)

---

## Quick Start

1.  **Define your workflow**:

    ```python
    import asyncio
    from senpuki import Senpuki, Result

    # 1. Define an activity
    @Senpuki.durable()
    async def greet(name: str) -> str:
        await asyncio.sleep(0.1) # Simulate work
        return f"Hello, {name}!"

    # 2. Define an orchestrator
    @Senpuki.durable()
    async def workflow(names: list[str]) -> Result[list[str], Exception]:
        results = []
        for name in names:
            # Call activity (awaiting it schedules it and waits for result)
            res = await greet(name) 
            results.append(res)
        return Result.Ok(results)
    ```

2.  **Run the system**:

    ```python
    async def main():
        # Setup Backend
        backend = Senpuki.backends.SQLiteBackend("senpuki.sqlite")
        await backend.init_db()
        executor = Senpuki(backend=backend)

        # Start a Worker (in background)
        worker = asyncio.create_task(executor.serve())

        # Dispatch Workflow
        exec_id = await executor.dispatch(workflow, ["Alice", "Bob"])
        print(f"Started execution: {exec_id}")

        # Wait for Result
        while True:
            state = await executor.state_of(exec_id)
            if state.state in ("completed", "failed"):
                break
            await asyncio.sleep(0.5)

        result = await executor.result_of(exec_id)
        print(result.value) # ['Hello, Alice!', 'Hello, Bob!']

    if __name__ == "__main__":
        asyncio.run(main())
    ```

---

## Features Guide

### Defining Durable Functions

Use the `@Senpuki.durable` decorator. You can configure retry policies, caching, and queues here.

```python
from senpuki import Senpuki, RetryPolicy

@Senpuki.durable(
    retry_policy=RetryPolicy(max_attempts=3, initial_delay=1.0),
    queue="high_priority",
    tags=["billing"]
)
async def charge_card(amount: int):
    ...
```

Every durable workflow or activity must be registered. Decorate them with `@Senpuki.durable()` (or register metadata manually) before dispatching. Otherwise the executor raises `UnregisteredFunctionError` immediately, so missing registrations are caught up front instead of deep in a worker.

Registrations live in a `FunctionRegistry`. Executors use the shared registry by default, but you can pass a custom one to isolate tests or multi-tenant hosts:

```python
from senpuki.registry import registry

custom_registry = registry.copy()
# modify/add registrations...
executor = Senpuki(backend=backend, function_registry=custom_registry)
```

Only that executor sees `custom_registry`, so other tests or apps keep their own registry state.

### Orchestration & Activities

When a durable function calls another durable function (e.g., `await other_func()`), Senpuki intercepts this call.
*   It persists a **Task** record for the child function.
*   The parent function "sleeps" (suspends) until the child task is completed by a worker.
*   This allows workflows to run over days or weeks without consuming memory while waiting.

### Durable Sleep

Standard `time.sleep` or `asyncio.sleep` blocks the worker process, preventing it from handling other tasks. Senpuki provides a durable sleep that suspends the function and schedules a wake-up task.

```python
# Instead of asyncio.sleep(60)
await Senpuki.sleep(60) # Sleeps for 60 seconds
await Senpuki.sleep("1h 30m") # Supports duration strings
```

During this sleep, the worker is free to process other tasks.

### Retries & Error Handling

Failures happen. Senpuki allows declarative retry policies.

```python
policy = RetryPolicy(
    max_attempts=5,
    backoff_factor=2.0, # Exponential backoff
    initial_delay=1.0,  # First retry after 1s
    max_delay=60.0,     # Cap delay at 60s
    jitter=0.1,         # Add randomness to prevent thundering herd
    retry_for=(ConnectionError, TimeoutError) # Only retry these exceptions
)

@Senpuki.durable(retry_policy=policy)
async def unstable_api_call():
    ...
```

### Idempotency & Caching

To prevent duplicate side-effects (like charging a card twice) or re-doing expensive work:

1.  **Idempotency**: Results are stored permanently. If a task is scheduled again with the same arguments (and version), the stored result is returned immediately without running the function.
2.  **Caching**: Similar to idempotency but implies the result can be reused across different executions if the key matches.

```python
@Senpuki.durable(idempotent=True)
async def send_email(user_id: str, subject: str):
    # Safe to call multiple times; will only execute once per unique arguments
    ...

@Senpuki.durable(cached=True, version="v1")
async def heavy_compute(data_hash: str):
    # Result stored in cache table; subsequent calls return immediately
    ...
```

### Parallel Execution & Batching

You can run tasks in parallel using standard `asyncio.gather`. Senpuki also provides `Senpuki.map` for optimized batch scheduling.

**Using `asyncio.gather` (Fan-out)**
```python
@Senpuki.durable()
async def process_items(items: list[int]):
    tasks = []
    for item in items:
        tasks.append(process_single(item))
    
    # Run all in parallel
    results = await asyncio.gather(*tasks)
    return results
```

**Using `Senpuki.map` (Optimized)**
Use `map` when you have a large list of items. It batches the database operations for scheduling.

```python
@Senpuki.durable()
async def batch_process(items: list[int]):
    # Efficiently schedules process_single for each item
    results = await Senpuki.map(process_single, items)
    return results
```

### Rate Limiting (Concurrency Control)

You can limit the number of concurrent executions for a specific function across the entire cluster. This is useful for protecting external resources or managing heavy workloads.

```python
# Only allow 5 concurrent calls to this function across all workers
@Senpuki.durable(max_concurrent=5)
async def external_api_call(data: dict):
    ...
```

### Timeouts & Expirys

You can set a expiry for the entire execution. If it exceeds this duration, it is cancelled.

```python
exec_id = await executor.dispatch(long_workflow, expiry="1h 30m")
```

Expiry/max-duration timers start when an execution becomes eligible to run. If you also supply a `delay`,
Senpuki waits for that schedule before starting the countdown so your workflow still receives the full
duration once it begins. Without a delay, the timer starts immediately at dispatch time.

---

## Architecture & Backends

Senpuki is backend-agnostic.

### SQLite Backend
Included by default. Stores state in a local SQLite file.
*   **Best for**: Development, testing, single-node deployments, embedded workflows.
*   **Features**: Full persistence, async support.

### Postgres Backend
*   **Best for**: Production environments, concurrent access, high reliability.
*   **Features**: Uses `asyncpg` for high performance.

### Mongo Backend (Planned)
*   **Best for**: Distributed production clusters, high availability.

### Redis (Notifications)
Optional. Uses Redis Pub/Sub to notify orchestrators immediately when a task finishes, reducing polling latency.
*   **Recommended for production**: Adaptive polling prevents hot loops, but Redis keeps latency low and eliminates extra DB load under scale.

---

## Worker Configuration & Deployment

The `executor.serve()` method runs the worker loop. You can configure it to handle specific workloads and perform automatic cleanup.

```python
from datetime import timedelta

async def run_worker():
    backend = Senpuki.backends.SQLiteBackend("prod.db")
    executor = Senpuki(backend=backend)
    
    await executor.serve(
        worker_id="worker-1",      # Unique ID for this worker instance
        queues=["default", "high"],# Only process tasks in these queues
        tags=["billing", "email"], # Only process tasks with these tags
        max_concurrency=50,        # Max concurrent tasks per worker
        poll_interval=0.5,         # Minimum DB polling interval when idle
        poll_interval_max=5.0,     # Maximum backoff when idle
        poll_backoff_factor=2.0,   # Exponential backoff factor
        lease_duration=timedelta(minutes=5), # Task lock duration
        heartbeat_interval=timedelta(minutes=2), # Lease renewal cadence
        cleanup_interval=3600.0,   # Run cleanup every hour
        retention_period=timedelta(days=7)   # Delete executions older than 7 days
    )
```

When Redis notifications aren't configured, Senpuki now uses adaptive polling everywhere it waits on the database. You can tune orchestration waiters globally via the executor constructor:

```python
executor = Senpuki(
    backend=backend,
    poll_min_interval=0.25,
    poll_max_interval=3.0,
    poll_backoff_factor=1.5,
)
```

Workers share the same backoff strategy through `poll_interval`, `poll_interval_max`, and `poll_backoff_factor`. Long-running activities automatically renew their leases every `heartbeat_interval` (defaulting to half of `lease_duration` with a 100 ms floor), so duplicate execution only happens if the worker crashes and the heartbeat stops.

> **Production tip:** Redis notifications are still the recommended way to unblock waiters in real time. Adaptive polling keeps database load under control when Redis is unavailable, but Pub/Sub provides the fastest and most efficient wake-ups.

**Scaling:**
Run multiple worker processes (or containers) pointing to the same database backend. They will automatically coordinate and distribute tasks.

---

## Monitoring & Management

Senpuki provides multiple ways to inspect and manage executions: Programmatic API, CLI, and OpenTelemetry integration.

### Command Line Interface (CLI)

Senpuki comes with a built-in CLI to manage workflows.

```bash
# List recent executions
senpuki list

# List failed executions
senpuki list --state failed

# Show details and progress of a specific execution
senpuki show <execution_id>
```

**Configuration:**
The CLI connects to `senpuki.sqlite` by default. You can specify a different database (SQLite path or Postgres DSN) via the `--db` flag or `SENPUKI_DB` environment variable.

```bash
export SENPUKI_DB="postgresql://user:pass@localhost/senpuki"
senpuki list
```

### OpenTelemetry Instrumentation

You can instrument Senpuki to emit distributed traces for your workflows (compatible with Jaeger, Honeycomb, Datadog, etc.).

```python
from senpuki import Senpuki
from senpuki.telemetry import instrument

# Auto-instrument Senpuki
# This will emit spans for 'senpuki.dispatch' and 'senpuki.execute'
instrument()

executor = Senpuki(backend=...)
```

### Programmatic API

You can also inspect state directly in your code:

```python
# Check status
state = await executor.state_of(exec_id)
print(f"State: {state.state}, Progress: {state.progress_str}")

# Wait for completion (blocking)
result = await executor.wait_for(exec_id, expiry=30.0)

# List recent executions
executions = await executor.list_executions(limit=20, state="failed")

# Check queue depth
pending_count = await executor.queue_depth(queue="default")

# Inspect running activities
running_tasks = await executor.get_running_activities()
```

---

## Advanced Configuration

### Result Type
Senpuki uses a Rust-like `Result` type (`Ok` / `Error`) for robust error handling, although standard exceptions are also supported.

```python
from senpuki import Result

@Senpuki.durable()
async def safe_divide(a: int, b: int) -> Result[float, str]:
    if b == 0:
        return Result.Error("Division by zero")
    return Result.Ok(a / b)

# Usage
res = await safe_divide(10, 0)
if res.ok:
    print(res.value)
else:
    print(f"Error: {res.error}")
```

### Notification Backend (Redis)
By default, workers poll the database. For lower latency, use Redis for real-time notifications.

```python
executor = Senpuki(
    backend=Senpuki.backends.PostgresBackend(dsn),
    notification_backend=Senpuki.notifications.RedisBackend("redis://localhost:6379")
)
```

### Serialization
Senpuki defaults to `json` serialization. You can switch to `pickle` for complex Python objects (e.g., custom classes), or implement a custom serializer.

```python
# Use Pickle
executor = Senpuki(backend=backend, serializer="pickle")

# Custom
class MySerializer:
    def dumps(self, obj) -> bytes: ...
    def loads(self, data: bytes) -> Any: ...

executor = Senpuki(backend=backend, serializer=MySerializer())
```

---

## Examples

See the `examples/` folder for complete code:

1.  **`simple_flow.py`**: Basic parent-child function calls.
2.  **`failing_flow.py`**: Demonstrates automatic retries and Dead Letter Queue (DLQ) behavior.
3.  **`complex_workflow.py`**: A data pipeline showcasing caching, retries, and expirys.
*   `batch_processing.py`: Fan-out/fan-in pattern (processing multiple items in parallel).
*   `saga_trip_booking.py`: Saga pattern with compensation (rollback) logic.
*   `media_pipeline.py`: A complex 5-minute simulation of a media processing pipeline (Validation -> Safety -> Transcode/AI -> Package) with a live progress dashboard.

## Requirements
