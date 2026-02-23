# Stent API Reference

The `Stent` class is the main entry point for the library. It provides methods for dispatching workflows, running workers, and managing executions.

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

### Example

```python
from stent import Stent

# Minimal setup
backend = Stent.backends.SQLiteBackend("workflow.db")
await backend.init_db()
executor = Stent(backend=backend)

# Full production setup
executor = Stent(
    backend=Stent.backends.PostgresBackend("postgresql://user:pass@localhost/stent"),
    serializer="pickle",
    notification_backend=Stent.notifications.RedisBackend("redis://localhost:6379"),
    poll_min_interval=0.25,
    poll_max_interval=3.0,
    poll_backoff_factor=1.5,
)
```

---

## Class Attributes

### `Stent.backends`

Factory for creating storage backends.

```python
# SQLite backend
backend = Stent.backends.SQLiteBackend("path/to/db.sqlite")

# PostgreSQL backend
backend = Stent.backends.PostgresBackend("postgresql://user:pass@host:5432/db")
```

### `Stent.notifications`

Factory for creating notification backends.

```python
# Redis notification backend
notifications = Stent.notifications.RedisBackend("redis://localhost:6379")
```

### `Stent.default_registry`

The global function registry. All `@Stent.durable()` decorated functions are registered here by default.

```python
from stent import registry

# Access registered functions
meta = registry.get("module:function_name")
```

---

## Decorator

### `@Stent.durable()`

Decorator to register a function as durable.

```python
@Stent.durable(
    cached: bool = False,
    retry_policy: RetryPolicy | None = None,
    tags: List[str] | None = None,
    priority: int = 0,
    queue: str | None = None,
    idempotent: bool = False,
    idempotency_key_func: Callable[..., str] | None = None,
    version: str | None = None,
    max_concurrent: int | None = None,
)
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

#### Examples

```python
# Basic activity
@Stent.durable()
async def process_item(item_id: str) -> dict:
    return {"id": item_id, "processed": True}

# Activity with retries
@Stent.durable(
    retry_policy=RetryPolicy(
        max_attempts=5,
        initial_delay=1.0,
        backoff_factor=2.0,
        max_delay=60.0,
        retry_for=(ConnectionError, TimeoutError)
    )
)
async def call_external_api(url: str) -> dict:
    response = await http_client.get(url)
    return response.json()

# Cached expensive computation
@Stent.durable(cached=True, version="v1")
async def heavy_computation(data_hash: str) -> bytes:
    # Result is cached - subsequent calls return immediately
    return await compute_expensive_result(data_hash)

# Idempotent operation
@Stent.durable(idempotent=True)
async def charge_customer(customer_id: str, amount: int) -> str:
    # Only executes once per unique arguments
    return await payment_gateway.charge(customer_id, amount)

# Rate-limited function
@Stent.durable(max_concurrent=5)
async def external_api_call(data: dict) -> dict:
    # Max 5 concurrent calls across all workers
    return await api.call(data)

# Custom idempotency key
@Stent.durable(
    idempotent=True,
    idempotency_key_func=lambda order_id, **_: f"process_order:{order_id}"
)
async def process_order(order_id: str, timestamp: str) -> dict:
    # Uses only order_id for idempotency (ignores timestamp)
    ...
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
    max_duration: str | timedelta | None = None,  # Alias for expiry
    delay: str | dict | timedelta | None = None,
    tags: List[str] | None = None,
    priority: int = 0,
    queue: str | None = None,
    **kwargs,
) -> str:
```

#### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `fn` | `Callable` | Required | The durable function to execute |
| `*args` | `Any` | - | Positional arguments for the function |
| `expiry` | `str \| timedelta \| None` | `None` | Maximum execution time |
| `max_duration` | `str \| timedelta \| None` | `None` | Alias for `expiry` |
| `delay` | `str \| dict \| timedelta \| None` | `None` | Delay before execution starts |
| `tags` | `List[str] \| None` | `None` | Tags for this execution |
| `priority` | `int` | `0` | Priority for this execution |
| `queue` | `str \| None` | `None` | Queue for this execution |
| `**kwargs` | `Any` | - | Keyword arguments for the function |

#### Returns

`str` - The execution ID (UUID)

#### Duration String Format

Duration strings support: `s` (seconds), `m` (minutes), `h` (hours), `d` (days), `w` (weeks)

Parsing is strict: the full string must be valid (no trailing text and no embedded spaces).

```python
"30s"      # 30 seconds
"5m"       # 5 minutes
"1h30m"    # 1 hour 30 minutes
"2d8h"     # 2 days 8 hours
"1w"       # 1 week
```

#### Examples

```python
# Basic dispatch
exec_id = await executor.dispatch(my_workflow, arg1, arg2)

# With timeout
exec_id = await executor.dispatch(
    my_workflow, 
    arg1, 
    expiry="1h"  # Must complete within 1 hour
)

# Scheduled for later
exec_id = await executor.dispatch(
    my_workflow,
    arg1,
    delay="30m"  # Start 30 minutes from now
)

# With priority and queue
exec_id = await executor.dispatch(
    my_workflow,
    arg1,
    priority=10,
    queue="high_priority",
    tags=["important", "customer-123"]
)

# Using timedelta
from datetime import timedelta

exec_id = await executor.dispatch(
    my_workflow,
    arg1,
    expiry=timedelta(hours=2),
    delay=timedelta(minutes=15)
)
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
| `poll_interval_max` | `float \| None` | `None` | Maximum polling interval for backoff |
| `poll_backoff_factor` | `float \| None` | `None` | Backoff multiplier |
| `cleanup_interval` | `float \| None` | `3600.0` | Interval for cleanup job (seconds) |
| `retention_period` | `timedelta` | `7 days` | How long to keep completed executions |
| `lifecycle` | `WorkerLifecycle \| None` | `None` | Lifecycle handle for coordination |

#### Examples

```python
# Basic worker
await executor.serve()

# Configured worker
await executor.serve(
    worker_id="worker-1",
    queues=["default", "high_priority"],
    tags=["billing"],
    max_concurrency=50,
    poll_interval=0.5,
    lease_duration=timedelta(minutes=10),
    heartbeat_interval=timedelta(minutes=4),
)

# Worker with lifecycle coordination
lifecycle = executor.create_worker_lifecycle(name="worker-1")
worker_task = asyncio.create_task(executor.serve(lifecycle=lifecycle))

# Wait for worker to be ready
await lifecycle.wait_until_ready()
print("Worker is ready!")

# Graceful shutdown
executor.request_worker_drain(lifecycle)
await lifecycle.wait_until_stopped()
```

---

### `state_of()`

Get the current state of an execution.

```python
async def state_of(self, execution_id: str) -> ExecutionState:
```

#### Returns

`ExecutionState` with the following properties:

| Property | Type | Description |
|----------|------|-------------|
| `id` | `str` | Execution ID |
| `state` | `str` | Current state (pending, running, completed, failed, timed_out, cancelled) |
| `result` | `Result \| None` | Deserialized result (if completed) |
| `started_at` | `datetime \| None` | When execution started |
| `completed_at` | `datetime \| None` | When execution finished |
| `retries` | `int` | Number of retries |
| `progress` | `List[ExecutionProgress]` | Progress entries |
| `tags` | `List[str]` | Tags |
| `priority` | `int` | Priority |
| `queue` | `str \| None` | Queue |
| `counters` | `dict[str, int \| float]` | Execution-scoped counters |
| `custom_state` | `dict[str, Any]` | Execution-scoped custom state |
| `progress_str` | `str` | Human-readable progress string |

#### Example

```python
state = await executor.state_of(exec_id)
print(f"State: {state.state}")
print(f"Progress: {state.progress_str}")
```

---

### `result_of()`

Get the result of a completed execution.

```python
async def result_of(self, execution_id: str) -> Result[Any, Any]:
```

#### Returns

`Result[Any, Any]` - The execution result

#### Raises

- `ValueError` - If execution not found
- `Exception` - If execution is still running

#### Example

```python
result = await executor.result_of(exec_id)
if result.ok:
    print(f"Success: {result.value}")
else:
    print(f"Error: {result.error}")
```

---

### `wait_for()`

Block until an execution completes.

```python
async def wait_for(
    self, 
    execution_id: str, 
    expiry: float | None = None
) -> Result[Any, Any]:
```

#### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `execution_id` | `str` | Required | The execution to wait for |
| `expiry` | `float \| None` | `None` | Timeout in seconds |

#### Returns

`Result[Any, Any]` - The execution result

#### Raises

`ExpiryError` - If timeout is exceeded

#### Example

```python
# Wait indefinitely
result = await executor.wait_for(exec_id)

# Wait with timeout
try:
    result = await executor.wait_for(exec_id, expiry=30.0)
except ExpiryError:
    print("Timed out waiting for execution")
```

---

### `send_signal()`

Send a signal to a running execution.

```python
async def send_signal(
    self, 
    execution_id: str, 
    name: str, 
    payload: Any
) -> None:
```

#### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `execution_id` | `str` | Target execution ID |
| `name` | `str` | Signal name |
| `payload` | `Any` | Signal data (must be serializable) |

#### Example

```python
# Send approval signal
await executor.send_signal(
    exec_id, 
    "approval", 
    {"approved": True, "approver": "admin@example.com"}
)
```

---

### `list_executions()`

List executions with optional filtering.

```python
async def list_executions(
    self, 
    limit: int = 10, 
    offset: int = 0, 
    state: str | None = None
) -> List[ExecutionState]:
```

#### Example

```python
# List recent executions
executions = await executor.list_executions(limit=20)

# List failed executions
failed = await executor.list_executions(state="failed")
```

---

### `queue_depth()`

Get the number of pending tasks in a queue.

```python
async def queue_depth(self, queue: str | None = None) -> int:
```

#### Example

```python
# Check all queues
total_pending = await executor.queue_depth()

# Check specific queue
high_priority_pending = await executor.queue_depth(queue="high_priority")
```

---

### Dead Letter Queue Methods

#### `list_dead_letters()`

```python
async def list_dead_letters(self, limit: int = 50) -> List[DeadLetterRecord]:
```

#### `get_dead_letter()`

```python
async def get_dead_letter(self, task_id: str) -> DeadLetterRecord | None:
```

#### `replay_dead_letter()`

Re-enqueue a dead-lettered task.

```python
async def replay_dead_letter(
    self,
    task_id: str,
    *,
    queue: str | None = None,
    reset_retries: bool = True,
) -> str:  # Returns new task ID
```

#### `discard_dead_letter()`

```python
async def discard_dead_letter(self, task_id: str) -> bool:
```

---

## Static Methods

### `Stent.context()`

Access execution-scoped counters and custom state from inside a durable function.

```python
@classmethod
def context(
    cls,
    *,
    counters: dict[str, int | float] | None = None,
    state: dict[str, Any] | None = None,
) -> ExecutionContext:
```

Notes:
- Must be called within an active durable execution.
- `counters`/`state` initialize defaults if values are not present yet.

```python
@Stent.durable()
async def workflow():
    ctx = Stent.context(counters={"progress": 0}, state={"phase": "start"})
    ctx.counters("progress").add(1)
    ctx.state("phase").set("running")
```

---

### `Stent.sleep()`

Durable sleep that doesn't block the worker.

```python
@staticmethod
async def sleep(duration: str | dict | timedelta) -> None:
```

#### Example

```python
@Stent.durable()
async def workflow_with_wait():
    await do_first_step()
    
    # Worker is free during this sleep
    await Stent.sleep("1h")
    
    await do_second_step()
```

---

### `Stent.map()`

Batch schedule a function for each item in an iterable.

```python
@classmethod
async def map(
    cls, 
    fn: Callable[..., Awaitable[Any]], 
    iterable: Any
) -> List[Any]:
```

More efficient than `asyncio.gather` for large batches as it batches database operations.

#### Example

```python
@Stent.durable()
async def batch_workflow(item_ids: list[str]):
    # Efficiently schedules process_item for each ID
    results = await Stent.map(process_item, item_ids)
    return results
```

---

### `Stent.gather()`

Alias for `asyncio.gather` with support for `return_exceptions`.

```python
@classmethod
async def gather(cls, *tasks, **kwargs) -> List[Any]:
```

---

### `Stent.wait_for_signal()`

Wait for an external signal within a workflow.

```python
@staticmethod
async def wait_for_signal(name: str) -> Any:
```

#### Example

```python
@Stent.durable()
async def approval_workflow(request_id: str):
    await send_approval_request(request_id)
    
    # Block until signal is received
    approval = await Stent.wait_for_signal("approval")
    
    if approval["approved"]:
        await process_approved_request(request_id)
    else:
        await handle_rejection(request_id)
```

---

## WorkerLifecycle

Handle for coordinating worker readiness and shutdown.

```python
@dataclass
class WorkerLifecycle:
    name: str | None = None
    state: Literal["starting", "ready", "draining", "stopped"] = "starting"
```

### Methods

| Method | Description |
|--------|-------------|
| `wait_until_ready()` | Async wait until worker is ready |
| `wait_until_stopped()` | Async wait until worker has stopped |
| `request_drain()` | Signal the worker to stop accepting new tasks |

### Example

```python
# Create lifecycle handle
lifecycle = executor.create_worker_lifecycle(name="worker-1")

# Start worker
worker = asyncio.create_task(executor.serve(lifecycle=lifecycle))

# Wait for readiness (useful for health checks)
await lifecycle.wait_until_ready()

# Later, graceful shutdown
executor.request_worker_drain(lifecycle)
await lifecycle.wait_until_stopped()
```

---

## Exceptions

### `UnregisteredFunctionError`

Raised when attempting to dispatch a function that hasn't been decorated with `@Stent.durable()`.

```python
class UnregisteredFunctionError(RuntimeError):
    function_name: str
```

### `ExpiryError`

Raised when a timeout is exceeded.

```python
class ExpiryError(TimeoutError):
    pass
```
