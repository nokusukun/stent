# Configuration Reference

This document provides a complete reference for all Stent configuration options.

## Stent Constructor

```python
from stent import Stent

executor = Stent(
    backend=backend,
    serializer="json",
    notification_backend=None,
    poll_min_interval=0.1,
    poll_max_interval=5.0,
    poll_backoff_factor=2.0,
    function_registry=None,
    metrics=None,
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `backend` | `Backend` | Required | Database backend for persistence |
| `serializer` | `Serializer \| "json" \| "pickle"` | `"json"` | Serialization format for arguments and results |
| `notification_backend` | `NotificationBackend \| None` | `None` | Real-time notification backend (e.g., Redis) |
| `poll_min_interval` | `float` | `0.1` | Minimum polling interval in seconds |
| `poll_max_interval` | `float` | `5.0` | Maximum polling interval in seconds |
| `poll_backoff_factor` | `float` | `2.0` | Backoff multiplier when no tasks are available |
| `function_registry` | `FunctionRegistry \| None` | `None` | Custom function registry (uses global default if None) |
| `metrics` | `MetricsRecorder \| None` | `None` | Metrics recorder for monitoring |

## @Stent.durable Decorator

```python
@Stent.durable(
    cached=False,
    retry_policy=None,
    tags=None,
    priority=0,
    queue=None,
    idempotent=False,
    idempotency_key_func=None,
    version=None,
    max_concurrent=None,
)
async def my_function():
    ...
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `cached` | `bool` | `False` | Cache results based on arguments |
| `retry_policy` | `RetryPolicy \| None` | `None` | Retry behavior on failure (uses defaults if None) |
| `tags` | `List[str] \| None` | `None` | Tags for worker filtering |
| `priority` | `int` | `0` | Task priority (higher = more important) |
| `queue` | `str \| None` | `None` | Queue name for routing |
| `idempotent` | `bool` | `False` | Store results for idempotent replay |
| `idempotency_key_func` | `Callable[..., str] \| None` | `None` | Custom function to generate idempotency keys |
| `version` | `str \| None` | `None` | Version string for cache invalidation |
| `max_concurrent` | `int \| None` | `None` | Maximum concurrent executions of this function |

## RetryPolicy

```python
from stent import RetryPolicy

policy = RetryPolicy(
    max_attempts=3,
    backoff_factor=2.0,
    initial_delay=1.0,
    max_delay=60.0,
    jitter=0.1,
    retry_for=(Exception,),
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `max_attempts` | `int` | `3` | Maximum number of attempts (including first) |
| `backoff_factor` | `float` | `2.0` | Multiplier for exponential backoff |
| `initial_delay` | `float` | `1.0` | Delay before first retry (seconds) |
| `max_delay` | `float` | `60.0` | Maximum delay between retries (seconds) |
| `jitter` | `float` | `0.1` | Random jitter factor (0.1 = ±10%) |
| `retry_for` | `Tuple[Type[Exception], ...]` | `(Exception,)` | Exception types to retry |

### Retry Delay Calculation

```
delay = min(initial_delay * (backoff_factor ^ attempt), max_delay)
delay = delay ± (delay * jitter)
```

## executor.dispatch()

```python
exec_id = await executor.dispatch(
    fn,
    *args,
    expiry=None,
    max_duration=None,
    delay=None,
    tags=None,
    priority=0,
    queue=None,
    **kwargs,
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `fn` | `Callable` | Required | Durable function to execute |
| `*args` | | | Positional arguments for the function |
| `expiry` | `str \| timedelta \| None` | `None` | Maximum execution time (deprecated, use `max_duration`) |
| `max_duration` | `str \| timedelta \| None` | `None` | Maximum execution time |
| `delay` | `str \| dict \| timedelta \| None` | `None` | Delay before starting execution |
| `tags` | `List[str] \| None` | `None` | Override function tags |
| `priority` | `int` | `0` | Override function priority |
| `queue` | `str \| None` | `None` | Override function queue |
| `**kwargs` | | | Keyword arguments for the function |

### Duration Formats

```python
# String format
delay="30s"       # 30 seconds
delay="5m"        # 5 minutes
delay="2h"        # 2 hours
delay="1d"        # 1 day

# Dict format
delay={"minutes": 5, "seconds": 30}

# timedelta
from datetime import timedelta
delay=timedelta(hours=1)
```

## executor.serve()

```python
await executor.serve(
    worker_id=None,
    queues=None,
    tags=None,
    max_concurrency=10,
    lease_duration=timedelta(minutes=5),
    heartbeat_interval=None,
    poll_interval=1.0,
    poll_interval_max=None,
    poll_backoff_factor=None,
    cleanup_interval=3600.0,
    retention_period=timedelta(days=7),
    lifecycle=None,
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `worker_id` | `str \| None` | `None` | Unique worker identifier (auto-generated if None) |
| `queues` | `List[str] \| None` | `None` | Only process tasks from these queues |
| `tags` | `List[str] \| None` | `None` | Only process tasks with these tags |
| `max_concurrency` | `int` | `10` | Maximum concurrent tasks per worker |
| `lease_duration` | `timedelta` | `5 minutes` | How long a task is leased before reclaim |
| `heartbeat_interval` | `timedelta \| None` | `lease/2` | How often to renew lease (None to disable) |
| `poll_interval` | `float` | `1.0` | Polling interval when no tasks (seconds) |
| `poll_interval_max` | `float \| None` | `None` | Maximum polling interval (uses executor default) |
| `poll_backoff_factor` | `float \| None` | `None` | Backoff factor for polling (uses executor default) |
| `cleanup_interval` | `float \| None` | `3600.0` | How often to run cleanup (seconds, None to disable) |
| `retention_period` | `timedelta` | `7 days` | How long to keep completed executions |
| `lifecycle` | `WorkerLifecycle \| None` | `None` | Handle for coordinating worker lifecycle |

## Backends

### SQLiteBackend

```python
backend = Stent.backends.SQLiteBackend(path)
await backend.init_db()
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `path` | `str` | Path to SQLite database file |

Special paths:
- `":memory:"` - In-memory database (for testing)

### PostgresBackend

```python
backend = Stent.backends.PostgresBackend(dsn)
await backend.init_db()
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `dsn` | `str` | PostgreSQL connection string |

DSN format:
```
postgresql://user:password@host:port/database?sslmode=require
```

## Notification Backends

### RedisBackend

```python
notification_backend = Stent.notifications.RedisBackend(url)
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `url` | `str` | Redis connection URL |

URL format:
```
redis://localhost:6379
redis://:password@host:6379/0
rediss://host:6379  # SSL
```

## Serializers

### Built-in Serializers

```python
# JSON (default) - safe, portable
executor = Stent(backend=backend, serializer="json")

# Pickle - supports more Python types
executor = Stent(backend=backend, serializer="pickle")
```

### Custom Serializer

```python
from stent.utils.serialization import Serializer

class CustomSerializer(Serializer):
    def dumps(self, obj: Any) -> bytes:
        # Serialize object to bytes
        ...
    
    def loads(self, data: bytes) -> Any:
        # Deserialize bytes to object
        ...

executor = Stent(backend=backend, serializer=CustomSerializer())
```

## Metrics

### MetricsRecorder Protocol

```python
from stent.metrics import MetricsRecorder

class CustomMetrics(MetricsRecorder):
    def task_claimed(self, queue: str | None, step_name: str, kind: str) -> None:
        """Called when a task is claimed by a worker."""
        ...
    
    def task_completed(
        self, queue: str | None, step_name: str, kind: str, duration_s: float
    ) -> None:
        """Called when a task completes successfully."""
        ...
    
    def task_failed(
        self, queue: str | None, step_name: str, kind: str, reason: str, retrying: bool
    ) -> None:
        """Called when a task fails."""
        ...
    
    def dead_lettered(
        self, queue: str | None, step_name: str, kind: str, reason: str
    ) -> None:
        """Called when a task is moved to DLQ."""
        ...
    
    def lease_renewed(self, task_id: str, success: bool) -> None:
        """Called when a lease renewal is attempted."""
        ...

executor = Stent(backend=backend, metrics=CustomMetrics())
```

## Environment Variables

Stent doesn't read environment variables directly, but here's a common pattern:

```python
import os

# Database
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///stent.db")
REDIS_URL = os.environ.get("REDIS_URL")

# Worker settings
WORKER_CONCURRENCY = int(os.environ.get("WORKER_CONCURRENCY", "10"))
WORKER_QUEUES = os.environ.get("WORKER_QUEUES", "").split(",") or None
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "1.0"))

# Setup
if DATABASE_URL.startswith("postgresql"):
    backend = Stent.backends.PostgresBackend(DATABASE_URL)
else:
    backend = Stent.backends.SQLiteBackend(DATABASE_URL.replace("sqlite:///", ""))

notification_backend = None
if REDIS_URL:
    notification_backend = Stent.notifications.RedisBackend(REDIS_URL)

executor = Stent(
    backend=backend,
    notification_backend=notification_backend,
)

await executor.serve(
    max_concurrency=WORKER_CONCURRENCY,
    queues=WORKER_QUEUES,
    poll_interval=POLL_INTERVAL,
)
```

## CLI Configuration

The `stent` CLI uses environment variables or command-line arguments:

```bash
# Using environment variables
export STENT_DATABASE_URL="postgresql://localhost/stent"
stent executions list

# Using command-line arguments
stent --database "sqlite:///stent.db" executions list
```

### CLI Commands

```bash
# List executions
stent executions list [--state STATE] [--limit N]

# Show execution details
stent executions show <execution_id>

# List tasks for an execution
stent tasks list <execution_id>

# Dead Letter Queue
stent dlq list [--limit N]
stent dlq show <task_id>
stent dlq replay <task_id> [--queue QUEUE]
stent dlq discard <task_id>
```

## Full Configuration Example

```python
from datetime import timedelta
from stent import Stent, RetryPolicy
from stent.metrics import MetricsRecorder

# Custom metrics
class PrometheusMetrics(MetricsRecorder):
    # ... implementation
    pass

# Initialize
async def create_executor():
    # Backend
    backend = Stent.backends.PostgresBackend(
        "postgresql://user:pass@localhost:5432/stent"
    )
    await backend.init_db()
    
    # Notifications
    notification_backend = Stent.notifications.RedisBackend(
        "redis://localhost:6379"
    )
    
    # Metrics
    metrics = PrometheusMetrics()
    
    # Create executor
    executor = Stent(
        backend=backend,
        serializer="json",
        notification_backend=notification_backend,
        poll_min_interval=0.1,
        poll_max_interval=5.0,
        poll_backoff_factor=2.0,
        metrics=metrics,
    )
    
    return executor

# Define functions with full configuration
@Stent.durable(
    cached=True,
    retry_policy=RetryPolicy(
        max_attempts=5,
        initial_delay=1.0,
        backoff_factor=2.0,
        max_delay=60.0,
        jitter=0.1,
        retry_for=(ConnectionError, TimeoutError)
    ),
    tags=["api", "external"],
    priority=10,
    queue="high-priority",
    idempotent=True,
    version="1.0",
    max_concurrent=5,
)
async def api_call(endpoint: str, data: dict) -> dict:
    ...

# Start worker with full configuration
async def start_worker(executor: Stent):
    lifecycle = executor.create_worker_lifecycle(name="worker-1")
    
    await executor.serve(
        lifecycle=lifecycle,
        worker_id="worker-1",
        queues=["high-priority", "default"],
        tags=["api"],
        max_concurrency=20,
        lease_duration=timedelta(minutes=5),
        heartbeat_interval=timedelta(minutes=2),
        poll_interval=0.5,
        poll_interval_max=10.0,
        poll_backoff_factor=1.5,
        cleanup_interval=3600.0,
        retention_period=timedelta(days=14),
    )

# Dispatch with full options
async def dispatch_workflow(executor: Stent):
    exec_id = await executor.dispatch(
        my_workflow,
        arg1, arg2,
        kwarg1="value",
        max_duration="1h",
        delay="5m",
        tags=["urgent"],
        priority=100,
        queue="high-priority",
    )
    return exec_id
```

## See Also

- [Getting Started](getting-started.md) - Quick start guide
- [Deployment](deployment.md) - Production deployment
- [API Reference](api-reference/stent.md) - Full API documentation
