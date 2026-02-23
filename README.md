# Stent

Distributed durable functions for Python. Write reliable, stateful workflows using async/await.

```bash
pip install stent
```

## Quick Example

```python
import asyncio
from stent import Stent, Result

@Stent.durable()
async def process_order(order_id: str) -> dict:
    await asyncio.sleep(1)  # Simulate work
    return {"order_id": order_id, "status": "processed"}

@Stent.durable()
async def order_workflow(order_ids: list[str]) -> Result[list, Exception]:
    results = []
    for order_id in order_ids:
        result = await process_order(order_id)
        results.append(result)
    return Result.Ok(results)

async def main():
    backend = Stent.backends.SQLiteBackend("workflow.db")
    await backend.init_db()
    executor = Stent(backend=backend)
    
    worker = asyncio.create_task(executor.serve())
    
    exec_id = await executor.dispatch(order_workflow, ["ORD-001", "ORD-002"])
    result = await executor.wait_for(exec_id)
    print(result.value)

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
- **Automatic Retries** - Configurable retry policies with exponential backoff
- **Distributed Workers** - Scale horizontally across multiple processes
- **Parallel Execution** - Fan-out/fan-in with `asyncio.gather` and `Stent.map`
- **Rate Limiting** - Control concurrent executions per function
- **External Signals** - Coordinate workflows with external events
- **Dead Letter Queue** - Inspect and replay failed tasks
- **Idempotency & Caching** - Prevent duplicate work
- **Multiple Backends** - SQLite (dev) or PostgreSQL (production)
- **OpenTelemetry** - Distributed tracing support

## Key Concepts

```python
from stent import Stent, RetryPolicy

# Configurable retry policies
@Stent.durable(
    retry_policy=RetryPolicy(max_attempts=5, initial_delay=1.0),
    queue="high_priority",
    max_concurrent=10,  # Rate limiting
    idempotent=True,    # Prevent duplicate execution
)
async def my_activity(data: dict) -> dict:
    ...

# Durable sleep (doesn't block workers)
await Stent.sleep("30m")

# Parallel execution
results = await asyncio.gather(*[process(item) for item in items])
# Or optimized for large batches:
results = await Stent.map(process, items)

# External signals
payload = await Stent.wait_for_signal("approval")
await executor.send_signal(exec_id, "approval", {"approved": True})
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
