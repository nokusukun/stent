# Core Concepts

This document explains the fundamental concepts in Stent that you need to understand to build effective durable workflows.

## Durable Functions

A **durable function** is a Python async function decorated with `@Stent.durable()`. The decorator transforms the function into a task that can be:

- Persisted to a database
- Distributed across workers
- Retried on failure
- Tracked for progress

```python
from stent import Stent

@Stent.durable()
async def my_durable_function(x: int, y: int) -> int:
    return x + y
```

### Why "Durable"?

Standard Python functions exist only in memory. If your process crashes, all state is lost. Durable functions solve this by:

1. **Persisting state** - Arguments, results, and progress are stored in a database
2. **Enabling recovery** - After a crash, work can resume from where it left off
3. **Supporting long-running workflows** - Functions can "sleep" for hours/days without consuming resources

## Orchestrators vs Activities

Stent distinguishes between two types of durable functions:

### Orchestrators

An **orchestrator** is a durable function that coordinates other durable functions. It defines the workflow logic.

```python
@Stent.durable()
async def order_workflow(order_id: str) -> Result[dict, Exception]:
    # This is an orchestrator - it calls other durable functions
    customer = await fetch_customer(order_id)
    inventory = await check_inventory(order_id)
    
    if inventory["available"]:
        await reserve_inventory(order_id)
        await charge_customer(customer["id"], inventory["price"])
        await send_confirmation(customer["email"])
    
    return Result.Ok({"status": "completed"})
```

Orchestrators:

- Call other durable functions (activities or sub-orchestrators)
- Define the workflow control flow (conditionals, loops)
- Should be deterministic (same inputs produce same execution path)
- "Sleep" while waiting for child tasks

### Activities

An **activity** is a leaf-node durable function that performs actual work.

```python
@Stent.durable(
    retry_policy=RetryPolicy(max_attempts=3)
)
async def send_email(to: str, subject: str, body: str) -> bool:
    # This is an activity - it does actual work
    response = await email_client.send(to=to, subject=subject, body=body)
    return response.success
```

Activities:

- Perform side effects (API calls, database operations, file I/O)
- Are executed by workers
- Can have retry policies for resilience
- Should be idempotent when possible

### How They Work Together

```
dispatch(order_workflow, "ORD-123")
        |
        v
+-------------------+
|  order_workflow   | <-- Orchestrator
|   (Task 1)        |
+-------------------+
        |
        +---> fetch_customer("ORD-123")     <-- Activity (Task 2)
        |
        +---> check_inventory("ORD-123")    <-- Activity (Task 3)
        |
        +---> reserve_inventory("ORD-123")  <-- Activity (Task 4)
        |
        +---> charge_customer(...)          <-- Activity (Task 5)
        |
        +---> send_confirmation(...)        <-- Activity (Task 6)
```

When an orchestrator calls an activity:

1. A new task record is created in the database
2. The orchestrator suspends (frees up the worker)
3. A worker picks up the activity task
4. The activity executes and stores its result
5. The orchestrator resumes with the result

## Executions and Tasks

### Execution

An **execution** represents a single run of a workflow. It's created when you call `executor.dispatch()`.

```python
execution_id = await executor.dispatch(my_workflow, arg1, arg2)
# execution_id is a UUID like "550e8400-e29b-41d4-a716-446655440000"
```

Execution properties:

| Property | Description |
|----------|-------------|
| `id` | Unique identifier (UUID) |
| `root_function` | The dispatched function name |
| `state` | Current state (pending, running, completed, failed, timed_out, cancelled) |
| `args`, `kwargs` | Serialized input arguments |
| `result` | Serialized result (when completed) |
| `error` | Serialized error (when failed) |
| `created_at` | When the execution was created |
| `started_at` | When execution began |
| `completed_at` | When execution finished |
| `expiry_at` | Timeout deadline (if set) |
| `progress` | List of progress entries |
| `tags` | User-defined tags for filtering |
| `priority` | Priority level (higher = processed first) |
| `queue` | Queue name for routing |
| `counters` | Execution-scoped numeric counters |
| `custom_state` | Execution-scoped custom key-value state |

### Task

A **task** represents a single function call within an execution. The root orchestrator is Task 1, and each activity call creates a new task.

```python
# Internally, when you call:
result = await some_activity(arg)

# Stent creates a TaskRecord:
TaskRecord(
    id="task-uuid",
    execution_id="execution-uuid",
    step_name="module:some_activity",
    kind="activity",  # or "orchestrator" or "signal"
    parent_task_id="parent-task-uuid",
    state="pending",
    args=serialized_args,
    kwargs=serialized_kwargs,
    retries=0,
    # ... more fields
)
```

Task properties:

| Property | Description |
|----------|-------------|
| `id` | Unique identifier |
| `execution_id` | Parent execution ID |
| `step_name` | Function name (module:qualname) |
| `kind` | "orchestrator", "activity", or "signal" |
| `parent_task_id` | Parent task (for hierarchy) |
| `state` | pending, running, completed, failed |
| `retries` | Number of retry attempts |
| `worker_id` | Worker currently processing this task |
| `lease_expires_at` | When the worker's lease expires |
| `scheduled_for` | Delayed execution time |
| `idempotency_key` | Key for idempotent operations |

### Execution States

```
                    +----------+
                    | pending  |
                    +----+-----+
                         |
                         v
                    +----+-----+
          +-------->| running  |<--------+
          |         +----+-----+         |
          |              |               |
          |    +---------+---------+     |
          |    |         |         |     |
          |    v         v         v     |
      +---+----+   +-----+---+  +--+-----+--+
      |completed|  | failed  |  | timed_out |
      +---------+  +---------+  +-----------+
                        |
                        v
                   +----+-----+
                   | cancelled|
                   +----------+
```

- **pending**: Waiting to be picked up by a worker
- **running**: Currently being executed
- **completed**: Finished successfully
- **failed**: Failed after all retry attempts
- **timed_out**: Exceeded the expiry deadline
- **cancelling/cancelled**: Cancelled by user

## Result Type

Stent provides a Rust-inspired `Result` type for explicit error handling:

```python
from stent import Result

@Stent.durable()
async def divide(a: int, b: int) -> Result[float, str]:
    if b == 0:
        return Result.Error("Division by zero")
    return Result.Ok(a / b)

# Using the result
result = await divide(10, 2)

if result.ok:
    print(f"Value: {result.value}")  # 5.0
else:
    print(f"Error: {result.error}")
```

### Result API

```python
# Creating results
ok_result = Result.Ok(42)
error_result = Result.Error("Something went wrong")

# Checking status
if result.ok:
    value = result.value
else:
    error = result.error

# Converting to exception (raises if error)
value = result.or_raise()
```

### Why Use Result?

1. **Explicit error handling** - Errors are values, not exceptions
2. **Type safety** - The type system tracks error possibilities
3. **Serialization** - Results serialize cleanly to JSON/pickle
4. **Composability** - Easy to chain and transform

You can still use regular exceptions if you prefer:

```python
@Stent.durable()
async def might_fail():
    if random.random() < 0.5:
        raise ValueError("Random failure")
    return "success"
```

Exceptions are caught and stored in the task's error field.

## Workers

A **worker** is a process that executes tasks. Workers:

1. Poll the database for pending tasks
2. Claim tasks with a lease
3. Execute the task's function
4. Store the result
5. Release the lease

```python
# Start a worker
await executor.serve(
    worker_id="worker-1",
    max_concurrency=10,  # Process up to 10 tasks concurrently
    poll_interval=0.5    # Poll every 0.5 seconds
)
```

### Task Leasing

To prevent duplicate execution, workers acquire **leases** on tasks:

1. Worker claims a task, setting `lease_expires_at`
2. Worker has exclusive access until lease expires
3. For long tasks, worker renews the lease periodically
4. If worker crashes, lease expires and another worker can claim the task

```python
await executor.serve(
    lease_duration=timedelta(minutes=5),     # Lease lasts 5 minutes
    heartbeat_interval=timedelta(minutes=2)  # Renew every 2 minutes
)
```

### Concurrency Control

Workers can limit concurrency:

```python
# Worker-level: max 10 concurrent tasks
await executor.serve(max_concurrency=10)

# Function-level: max 5 concurrent calls cluster-wide
@Stent.durable(max_concurrent=5)
async def rate_limited_api_call():
    ...
```

## Queues and Priorities

### Queues

Queues partition work for different worker pools:

```python
# Assign function to a queue
@Stent.durable(queue="high_priority")
async def important_task():
    ...

# Worker only processes specific queues
await executor.serve(queues=["high_priority", "default"])
```

Use cases:

- Separate heavy vs light workloads
- Dedicate workers to specific task types
- Implement priority lanes

### Priorities

Higher priority tasks are processed first:

```python
# Higher priority function
@Stent.durable(priority=10)
async def urgent_task():
    ...

# Or set at dispatch time
await executor.dispatch(task, priority=10)
```

## Tags

Tags provide flexible filtering:

```python
@Stent.durable(tags=["billing", "customer"])
async def charge_customer():
    ...

# Worker only processes tasks with matching tags
await executor.serve(tags=["billing"])
```

Tag matching is inclusive for untagged tasks: workers configured with `tags=[...]` can claim matching tagged tasks plus untagged tasks.

## Next Steps

- [Defining Durable Functions](guides/durable-functions.md) - Deep dive into the `@durable` decorator
- [Orchestration Patterns](guides/orchestration.md) - Learn workflow composition
- [Error Handling](guides/error-handling.md) - Master retry policies and failure recovery
