# Stent

Distributed durable functions for Python. Write reliable, stateful workflows using async/await.

```bash
pip install stent
```

## Quick Example

```python
import asyncio
from stent import Stent

@Stent.durable
async def process_order(order_id: str) -> dict:
    await asyncio.sleep(1)  # Simulate work
    return {"order_id": order_id, "status": "processed"}

@Stent.durable
async def order_workflow(order_ids: list[str]) -> list:
    results = []
    for order_id in order_ids:
        result = await process_order(order_id)
        results.append(result)
    return results

async def main():
    backend = Stent.backends.SQLiteBackend("workflow.db")
    async with Stent.bootstrap(backend, serve=True) as executor:
        result = await order_workflow(["ORD-001", "ORD-002"])
        print(result)

asyncio.run(main())
```

## Why Stent?

| Feature | Temporal | Celery | Prefect | Airflow | **Stent** |
|---------|----------|--------|---------|---------|-------------|
| Durable Execution | Yes | No | Partial | No | **Yes** |
| Setup Complexity | High | Medium | Medium | High | **Very Low** |
| Infrastructure | Server cluster | Broker | Server | Multi-component | **SQLite/Postgres** |
| Native Async | Yes | No | Yes | Limited | **Yes** |

Stent fills the gap between simple task queues (Celery) and enterprise platforms (Temporal):

- **vs Temporal**: Same durability guarantees, fraction of the infrastructure
- **vs Celery/Dramatiq**: True workflow durability, not just task retries
- **vs Prefect/Airflow**: Application workflows, not batch data pipelines

See [full comparison](docs/comparison.md) for details.

## Features

- **Durable Execution** - Workflow state survives crashes and restarts
- **Auto-dispatch** - Durable functions work like normal `await` calls via `Stent.use()` or `Stent.bootstrap()`
- **Automatic Retries** - Configurable retry policies with exponential backoff
- **Distributed Workers** - Scale horizontally across multiple processes
- **Parallel Execution** - Fan-out/fan-in with `fn.map()`, `fn.starmap()`, and `asyncio.gather`
- **Rate Limiting** - Control concurrent executions per function
- **External Signals** - Coordinate workflows with external events
- **Dead Letter Queue** - Inspect and replay failed tasks
- **Cancellation** - Cancel running or pending executions with `executor.cancel()`
- **Idempotency & Caching** - Prevent duplicate work
- **Multiple Backends** - SQLite (dev) or PostgreSQL (production)
- **Testing Fixtures** - Built-in pytest fixtures via `stent.testing`
- **OpenTelemetry** - Distributed tracing support

## Key Concepts

```python
from stent import Stent, RetryPolicy

# Decorator works with or without parens
@Stent.durable
async def my_activity(data: dict) -> dict:
    ...

# Configurable options
@Stent.durable(
    retry_policy=RetryPolicy(max_attempts=5, initial_delay=1.0),
    queue="high_priority",
    max_concurrent=10,
    idempotent=True,
)
async def my_configured_activity(data: dict) -> dict:
    ...

# Durable sleep (doesn't block workers) — asyncio.sleep >= 1s auto-promotes
await asyncio.sleep(30)  # becomes durable inside execution context

# Parallel execution via .map()
results = await my_activity.map([item1, item2, item3])

# External signals
payload = await Stent.wait_for_signal("approval")
await executor.send_signal(exec_id, "approval", {"approved": True})

# Cancellation
await executor.cancel(exec_id)
```

## Setup

```python
# One-liner with bootstrap (recommended)
async with Stent.bootstrap(backend, serve=True) as executor:
    result = await my_workflow()

# Or manual setup
backend = Stent.backends.SQLiteBackend("stent.db")
await backend.init_db()
executor = Stent.use(backend=backend)
worker = asyncio.create_task(executor.serve())
await executor.wait_until_ready()
```

## Backends

```python
# SQLite (development)
backend = Stent.backends.SQLiteBackend("stent.db")

# PostgreSQL (production)
backend = Stent.backends.PostgresBackend("postgresql://user:pass@host/db")

# Optional: Redis for low-latency notifications
executor = Stent(
    backend=backend,
    notification_backend=Stent.notifications.RedisBackend("redis://localhost")
)
```

## Result Type

```python
from stent import Result

result = Result.Ok(42)
result.unwrap()        # 42
result.unwrap_or(0)    # 42
result.map(str)        # Result.Ok("42")

err = Result.Error("oops")
err.unwrap_or(0)       # 0
bool(err)              # False
```

## Testing

```python
# conftest.py
pytest_plugins = ["stent.testing"]

# test_my_workflow.py
async def test_workflow(stent_worker):
    result = await my_durable_fn(42)
    assert result == 84
```

## CLI

```bash
stent list                    # List executions
stent show <exec_id>          # Show execution details
stent dlq list                # List dead-lettered tasks
stent dlq replay <task_id>    # Replay failed task
```

## Documentation

Full documentation available in [`docs/`](docs/):

- [Getting Started](docs/getting-started.md) | [Core Concepts](docs/core-concepts.md) | [Comparison](docs/comparison.md)
- **Guides**: [Durable Functions](docs/guides/durable-functions.md) | [Orchestration](docs/guides/orchestration.md) | [Error Handling](docs/guides/error-handling.md) | [Parallel Execution](docs/guides/parallel-execution.md) | [Signals](docs/guides/signals.md) | [Workers](docs/guides/workers.md) | [Monitoring](docs/guides/monitoring.md)
- **Patterns**: [Saga](docs/patterns/saga.md) | [Batch Processing](docs/patterns/batch-processing.md)
- **Reference**: [API](docs/api-reference/stent.md) | [Configuration](docs/configuration.md) | [Deployment](docs/deployment.md)

## Examples

See [`examples/`](examples/) for complete workflows:
- `simple_flow.py` - Basic workflow
- `saga_trip_booking.py` - Saga pattern with compensation
- `batch_processing.py` - Fan-out/fan-in
- `media_pipeline.py` - Complex multi-stage pipeline

## Requirements

- Python 3.12+
- `aiosqlite` or `asyncpg` (backend)
- `redis` (optional, for notifications)

## License

MIT
