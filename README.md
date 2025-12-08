# DFns: Distributed Durable Functions for Python

DFns is a lightweight, asynchronous, distributed task orchestration library for Python. It allows you to write stateful, reliable workflows ("durable functions") using standard Python async/await syntax. DFns handles the complexity of persisting state, retrying failures, and distributing work across a pool of workers.

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

*   **Durable Functions**: Python async functions decorated with `@DFns.durable()`. They can be orchestrators (calling other functions) or activities (doing work).
*   **Orchestrator**: A durable function that schedules other durable functions. It sleeps while waiting for sub-tasks to complete, freeing up worker resources.
*   **Activity**: A leaf-node durable function that performs a specific action (e.g., API call, DB operation).
*   **Execution**: A single run of a workflow. It has a unique ID and persistent state.
*   **Worker**: A process that polls the backend storage for pending tasks and executes them.

---

## Installation

```bash
pip install dfns
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
    from dfns import DFns, Result

    # 1. Define an activity
    @DFns.durable()
    async def greet(name: str) -> str:
        await asyncio.sleep(0.1) # Simulate work
        return f"Hello, {name}!"

    # 2. Define an orchestrator
    @DFns.durable()
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
        backend = DFns.backends.SQLiteBackend("dfns.sqlite")
        await backend.init_db()
        executor = DFns(backend=backend)

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

Use the `@DFns.durable` decorator. You can configure retry policies, caching, and queues here.

```python
from dfns import DFns, RetryPolicy

@DFns.durable(
    retry_policy=RetryPolicy(max_attempts=3, initial_delay=1.0),
    queue="high_priority",
    tags=["billing"]
)
async def charge_card(amount: int):
    ...
```

### Orchestration & Activities

When a durable function calls another durable function (e.g., `await other_func()`), DFns intercepts this call.
*   It persists a **Task** record for the child function.
*   The parent function "sleeps" (suspends) until the child task is completed by a worker.
*   This allows workflows to run over days or weeks without consuming memory while waiting.

### Retries & Error Handling

Failures happen. DFns allows declarative retry policies.

```python
policy = RetryPolicy(
    max_attempts=5,
    backoff_factor=2.0, # Exponential backoff
    jitter=0.1,         # Add randomness to prevent thundering herd
    retry_for=(ConnectionError, ExpiryError) # Only retry these exceptions
)

@DFns.durable(retry_policy=policy)
async def unstable_api_call():
    ...
```

If the function fails after all retries, the Execution is marked as `failed`, and the error is propagated to the parent orchestrator (if any), which can catch it using standard `try/except`.

### Idempotency & Caching

To prevent duplicate side-effects (like charging a card twice) or re-doing expensive work:

1.  **Idempotency**: Results are stored permanently. If a task is scheduled again with the same arguments (and version), the stored result is returned immediately without running the function.
2.  **Caching**: Similar to idempotency but implies the result can be reused across different executions if the key matches.

```python
@DFns.durable(idempotent=True)
async def send_email(user_id: str, subject: str):
    # Safe to call multiple times; will only execute once per unique arguments
    ...

@DFns.durable(cached=True, version="v1")
async def heavy_compute(data_hash: str):
    # Result stored in cache table; subsequent calls return immediately
    ...
```

### Parallel Execution (Fan-out/Fan-in)

Use standard `asyncio.gather` to run tasks in parallel. DFns schedules them all, and the worker pool executes them concurrently.

```python
@DFns.durable()
async def batch_processor(items: list[int]):
    tasks = []
    for item in items:
        # Schedule all tasks
        tasks.append(process_item(item))
    
    # Wait for all to complete
    results = await asyncio.gather(*tasks)
    return sum(results)
```

### Timeouts & Expirys

You can set a expiry for the entire execution. If it exceeds this duration, it is cancelled.

```python
exec_id = await executor.dispatch(long_workflow, expiry="1h 30m")
```

---

## Architecture & Backends

DFns is backend-agnostic.

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

---

## Running Workers

The `executor.serve()` method runs the worker loop. In production, you typically run this in a separate process or container.

```python
# worker.py
async def run_worker():
    backend = DFns.backends.SQLiteBackend("prod.db")
    executor = DFns(backend=backend)
    
    # Consume only specific queues
    await executor.serve(
        queues=["default", "high_priority"],
        max_concurrency=50
    )
```

You can scale horizontally by running multiple worker instances pointing to the same database.

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
