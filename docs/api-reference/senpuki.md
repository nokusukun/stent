# Stent API Reference

The `Stent` class is the main entry point for the library. It provides methods for dispatching workflows, running workers, and managing executions.

## Quick Setup

```python
from stent import Stent

# Recommended: one-liner with bootstrap
backend = Stent.backends.SQLiteBackend("workflow.db")
async with Stent.bootstrap(backend, serve=True) as executor:
    result = await my_workflow()
```

---

## Constructor

```python
from stent import Stent

executor = Stent(
    backend: Backend,
    serializer: Serializer | Literal["json", "pickle"] = "json",
    notification_backend: NotificationBackend | None = None,
    poll_min_interval: float = 0.1,
    poll_max_interval: float = 5.0,
    poll_backoff_factor: float = 2.0,
    function_registry: FunctionRegistry | None = None,
    metrics: MetricsRecorder | None = None,
)
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `backend` | `Backend` | Required | Storage backend (SQLite or PostgreSQL) |
| `serializer` | `Serializer \| "json" \| "pickle"` | `"json"` | Serialization format for arguments and results |
| `notification_backend` | `NotificationBackend \| None` | `None` | Optional Redis backend for low-latency notifications |
| `poll_min_interval` | `float` | `0.1` | Minimum polling interval in seconds |
| `poll_max_interval` | `float` | `5.0` | Maximum polling interval in seconds (for backoff) |
| `poll_backoff_factor` | `float` | `2.0` | Multiplier for exponential backoff |
| `function_registry` | `FunctionRegistry \| None` | `None` | Custom function registry (uses global by default) |
| `metrics` | `MetricsRecorder \| None` | `None` | Custom metrics recorder |

---

## Class Methods

### `Stent.bootstrap()`

Async context manager that sets up everything in one call.

```python
@classmethod
@asynccontextmanager
async def bootstrap(
    cls,
    backend: Backend,
    *,
    serve: bool = False,
    poll_interval: float = 0.1,
    **kwargs,  # Passed to Stent()
) -> AsyncIterator[Stent]:
```

Performs:
1. `await backend.init_db()`
2. `Stent.use(backend=backend, **kwargs)`
3. Optionally starts a worker and waits for readiness
4. On exit: cancels worker, shuts down executor, closes backend, resets default

```python
# Without worker (client-only)
async with Stent.bootstrap(backend) as executor:
    exec_id = await executor.dispatch(my_fn, 42)

# With worker
async with Stent.bootstrap(backend, serve=True) as executor:
    result = await my_fn(42)  # auto-dispatches
```

### `Stent.use()`

Configure a default executor for auto-dispatch.

```python
@classmethod
def use(cls, backend: Backend, **kwargs) -> Stent:
```

After calling this, any `@Stent.durable` function called outside of a worker context will auto-dispatch through this executor and wait for the result.

```python
executor = Stent.use(backend=backend)
worker = asyncio.create_task(executor.serve())
await executor.wait_until_ready()

result = await my_durable_fn(42)  # auto-dispatches
```

### `Stent.reset()`

Clear the default executor. Useful for test teardown.

```python
Stent.reset()
```

---

## Class Attributes

### `Stent.backends`

Factory for creating storage backends.

```python
backend = Stent.backends.SQLiteBackend("path/to/db.sqlite")
backend = Stent.backends.PostgresBackend("postgresql://user:pass@host:5432/db")
```

### `Stent.notifications`

Factory for creating notification backends.

```python
notifications = Stent.notifications.RedisBackend("redis://localhost:6379")
```

### `Stent.default_registry`

The global function registry. All `@Stent.durable` decorated functions are registered here by default.

---

## Decorator

### `@Stent.durable`

Decorator to register a function as durable. Works with or without parentheses.

```python
# Without parens (no options)
@Stent.durable
async def my_task(): ...

# With parens (with options)
@Stent.durable(retry_policy=RetryPolicy(max_attempts=5))
async def my_task(): ...

# With parens (no options) — also works
@Stent.durable()
async def my_task(): ...
```

#### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `cached` | `bool` | `False` | Cache results for reuse across executions |
| `retry_policy` | `RetryPolicy \| None` | `None` | Retry configuration for failures |
| `tags` | `List[str] \| None` | `None` | Tags for worker filtering |
| `priority` | `int` | `0` | Task priority (higher = processed first) |
| `queue` | `str \| None` | `None` | Queue name for routing |
| `idempotent` | `bool` | `False` | Enable idempotency (prevent duplicate execution) |
| `idempotency_key_func` | `Callable[..., str] \| None` | `None` | Custom function to generate idempotency key |
| `version` | `str \| None` | `None` | Version string for cache/idempotency keys |
| `max_concurrent` | `int \| None` | `None` | Max concurrent executions cluster-wide |

#### Returns

`DurableFunction` — an async callable that also exposes `.map()` and `.starmap()` methods.

#### Examples

```python
@Stent.durable
async def process_item(item_id: str) -> dict:
    return {"id": item_id, "processed": True}

# With retry
@Stent.durable(
    retry_policy=RetryPolicy(
        max_attempts=5,
        initial_delay=1.0,
        backoff_factor=2.0,
        retry_for=(ConnectionError, TimeoutError)
    )
)
async def call_api(url: str) -> dict:
    ...

# Rate-limited
@Stent.durable(max_concurrent=5)
async def rate_limited(data: dict) -> dict:
    ...
```

### `DurableFunction.map(iterable)`

Batch-dispatch this function for each item in *iterable*.

```python
results = await process_item.map(["a", "b", "c"])
# Equivalent to: [await process_item("a"), await process_item("b"), ...]
```

### `DurableFunction.starmap(iterable)`

Batch-dispatch with multiple args per call.

```python
@Stent.durable
async def add(a: int, b: int) -> int:
    return a + b

results = await add.starmap([(1, 2), (3, 4), (10, 20)])
# [3, 7, 30]
```

---

## Instance Methods

### `dispatch()`

Start a new workflow execution.

```python
async def dispatch(
    self,
    fn: Callable[..., Awaitable[Any]],
    *args,
    expiry: str | timedelta | None = None,
    max_duration: str | timedelta | None = None,
    delay: str | dict | timedelta | None = None,
    tags: List[str] | None = None,
    priority: int = 0,
    queue: str | None = None,
    **kwargs,
) -> str:
```

#### Returns

`str` - The execution ID (UUID)

#### Duration String Format

`s` (seconds), `m` (minutes), `h` (hours), `d` (days), `w` (weeks). Combinable: `"1h30m"`, `"2d8h"`.

#### Examples

```python
exec_id = await executor.dispatch(my_workflow, arg1, arg2)
exec_id = await executor.dispatch(my_workflow, arg1, expiry="1h")
exec_id = await executor.dispatch(my_workflow, arg1, delay="30m")
```

---

### `wait_for()`

Block until an execution completes.

```python
async def wait_for(self, execution_id: str, expiry: float | None = None) -> Result[Any, Any]:
```

#### Raises

`ExpiryError` (subclass of `TimeoutError`) if timeout is exceeded.

```python
result = await executor.wait_for(exec_id)
value = result.unwrap()

# With timeout
try:
    result = await executor.wait_for(exec_id, expiry=30.0)
except ExpiryError:
    print("Timed out")
```

---

### `cancel()`

Request cancellation of a running or pending execution.

```python
async def cancel(self, execution_id: str) -> None:
```

Sets the execution state to `cancelled` and marks all pending/running tasks as `failed`. No-op if execution is already terminal.

```python
await executor.cancel(exec_id)
```

#### Raises

`ValueError` if execution not found.

---

### `wait_until_ready()`

Block until at least one worker signals readiness.

```python
async def wait_until_ready(self) -> None:
```

```python
worker = asyncio.create_task(executor.serve())
await executor.wait_until_ready()
# Worker is now accepting tasks
```

---

### `serve()`

Run the worker loop to process tasks.

```python
async def serve(
    self,
    *,
    worker_id: str | None = None,
    queues: List[str] | None = None,
    tags: List[str] | None = None,
    max_concurrency: int = 10,
    lease_duration: timedelta = timedelta(minutes=5),
    heartbeat_interval: timedelta | None = None,
    poll_interval: float = 1.0,
    poll_interval_max: float | None = None,
    poll_backoff_factor: float | None = None,
    cleanup_interval: float | None = 3600.0,
    retention_period: timedelta = timedelta(days=7),
    lifecycle: WorkerLifecycle | None = None,
) -> None:
```

#### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `worker_id` | `str \| None` | `None` | Unique worker identifier (auto-generated if not provided) |
| `queues` | `List[str] \| None` | `None` | Only process tasks from these queues |
| `tags` | `List[str] \| None` | `None` | Only process tasks with these tags |
| `max_concurrency` | `int` | `10` | Maximum concurrent tasks |
| `lease_duration` | `timedelta` | `5 minutes` | Task lease duration |
| `heartbeat_interval` | `timedelta \| None` | `None` | Lease renewal interval (default: half of lease_duration) |
| `poll_interval` | `float` | `1.0` | Minimum polling interval |
| `cleanup_interval` | `float \| None` | `3600.0` | Interval for cleanup job (seconds) |
| `retention_period` | `timedelta` | `7 days` | How long to keep completed executions |
| `lifecycle` | `WorkerLifecycle \| None` | `None` | Lifecycle handle for coordination |

---

### `state_of()`

Get the current state of an execution.

```python
async def state_of(self, execution_id: str) -> ExecutionState:
```

Returns `ExecutionState` with `id`, `state`, `result`, `started_at`, `completed_at`, `retries`, `progress`, `tags`, `priority`, `queue`, `counters`, `custom_state`.

---

### `result_of()`

Get the result of a completed execution.

```python
async def result_of(self, execution_id: str) -> Result[Any, Any]:
```

Raises `ValueError` if not found, `Exception` if still running.

---

### `schedule()`

Schedule a function to run after a delay. Shorthand for `dispatch(..., delay=...)`.

```python
async def schedule(self, delay: str | dict | timedelta, fn, *args, **kwargs) -> str:
```

---

### `send_signal()`

Send a signal to a running execution.

```python
async def send_signal(self, execution_id: str, name: str, payload: Any) -> None:
```

---

### `list_executions()`

```python
async def list_executions(self, limit=10, offset=0, state=None) -> List[ExecutionState]:
```

---

### `queue_depth()`

```python
async def queue_depth(self, queue: str | None = None) -> int:
```

---

### Dead Letter Queue Methods

```python
await executor.list_dead_letters(limit=50)
await executor.get_dead_letter(task_id)
await executor.replay_dead_letter(task_id, queue=None, reset_retries=True)
await executor.discard_dead_letter(task_id)
```

---

## Static Methods

### `Stent.context()`

Access execution-scoped counters and custom state from inside a durable function.

```python
@Stent.durable
async def workflow():
    ctx = Stent.context(counters={"progress": 0}, state={"phase": "start"})
    ctx.counters("progress").add(1)
    ctx.state("phase").set("running")
```

### `Stent.sleep()`

Durable sleep that doesn't block the worker.

```python
await Stent.sleep("1h")
```

Note: `asyncio.sleep(n)` where `n >= 1.0` is automatically promoted to durable sleep inside execution context.

### `Stent.map()` (legacy)

Batch-dispatch a function. Prefer `fn.map(items)` instead.

### `Stent.gather()`

Alias for `asyncio.gather`.

### `Stent.wait_for_signal()`

Wait for an external signal within a workflow.

```python
@Stent.durable
async def approval_workflow(request_id: str):
    await send_approval_request(request_id)
    approval = await Stent.wait_for_signal("approval")
    if approval["approved"]:
        await process_approved_request(request_id)
```

---

## WorkerLifecycle

Handle for coordinating worker readiness and shutdown.

```python
lifecycle = executor.create_worker_lifecycle(name="worker-1")
worker = asyncio.create_task(executor.serve(lifecycle=lifecycle))

await lifecycle.wait_until_ready()
# ... later ...
executor.request_worker_drain(lifecycle)
await lifecycle.wait_until_stopped()
```

Or use the simpler `wait_until_ready()` on the executor:

```python
worker = asyncio.create_task(executor.serve())
await executor.wait_until_ready()
```

---

## Exceptions

### `UnregisteredFunctionError`

Raised when dispatching a function not decorated with `@Stent.durable`. The error message includes the list of registered functions to help diagnose typos.

### `ExpiryError`

Subclass of `TimeoutError`. Raised when a timeout is exceeded in `wait_for()`.

---

## Testing

Stent provides pytest fixtures via `stent.testing`:

```python
# conftest.py
pytest_plugins = ["stent.testing"]
```

### Available Fixtures

| Fixture | Description |
|---------|-------------|
| `stent_backend` | Initialised SQLite backend in a temp directory |
| `stent_executor` | Executor wired to the test backend with auto-dispatch enabled |
| `stent_worker` | Executor with a running background worker |

```python
async def test_my_function(stent_worker):
    result = await my_durable_fn(42)
    assert result == 84
```
