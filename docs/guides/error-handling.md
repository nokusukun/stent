# Error Handling & Retries

This guide covers how Stent handles failures, configures retries, and manages the Dead Letter Queue.

## How Failures Work

When a durable function raises an exception:

1. Stent catches the exception
2. Checks if it's retryable (matches `retry_for` types)
3. If retryable and attempts remain:
   - Calculates retry delay
   - Reschedules the task
   - Task returns to "pending" state
4. If not retryable or max attempts reached:
   - Task marked as "failed"
   - Task moved to Dead Letter Queue
   - Parent orchestrator receives the error

```
Exception raised
       |
       v
+----------------+
| Is retryable?  |--No--> Move to DLQ, propagate error
+----------------+
       |
      Yes
       v
+------------------+
| Attempts remain? |--No--> Move to DLQ, propagate error
+------------------+
       |
      Yes
       v
Calculate retry delay
       |
       v
Reschedule task (pending)
```

## Configuring Retry Policies

### Default Policy

Without a retry policy, Stent uses these defaults:

```python
RetryPolicy(
    max_attempts=3,
    backoff_factor=2.0,
    initial_delay=1.0,
    max_delay=60.0,
    jitter=0.1,
    retry_for=(Exception,)  # Retry all exceptions
)
```

### Custom Retry Policy

```python
from stent import Stent, RetryPolicy

@Stent.durable(
    retry_policy=RetryPolicy(
        max_attempts=5,          # Try up to 5 times
        initial_delay=0.5,       # First retry after 0.5s
        backoff_factor=2.0,      # Double delay each retry
        max_delay=30.0,          # Cap at 30 seconds
        jitter=0.2,              # Add 20% randomness
        retry_for=(ConnectionError, TimeoutError)  # Only these types
    )
)
async def flaky_network_call(url: str) -> dict:
    return await http_client.get(url)
```

### Retry Delay Calculation

```
delay = initial_delay * (backoff_factor ^ (attempt - 1))
delay = min(delay, max_delay)
delay = delay ± (delay * jitter)
```

Example with defaults (initial=1, factor=2, max=60):

| Attempt | Base Delay | With Jitter (±10%) |
|---------|------------|-------------------|
| 1 | 1s | 0.9s - 1.1s |
| 2 | 2s | 1.8s - 2.2s |
| 3 | 4s | 3.6s - 4.4s |
| 4 | 8s | 7.2s - 8.8s |
| 5 | 16s | 14.4s - 17.6s |

### Selective Retry by Exception Type

```python
# Only retry on network errors, not validation errors
@Stent.durable(
    retry_policy=RetryPolicy(
        max_attempts=5,
        retry_for=(ConnectionError, TimeoutError, IOError)
    )
)
async def api_call(data: dict) -> dict:
    # ValidationError won't be retried
    if not validate(data):
        raise ValidationError("Invalid data")
    
    # These will be retried
    return await external_api.call(data)
```

### Disable Retries

```python
@Stent.durable(
    retry_policy=RetryPolicy(max_attempts=1)
)
async def no_retry_operation():
    # Fails immediately without retry
    ...
```

## Handling Errors in Orchestrators

### Using Result Type

```python
from stent import Result

@Stent.durable
async def safe_activity(data: dict) -> Result[dict, str]:
    try:
        result = await risky_operation(data)
        return Result.Ok(result)
    except Exception as e:
        return Result.Error(str(e))

@Stent.durable
async def orchestrator_with_result(items: list) -> Result[list, str]:
    results = []
    
    for item in items:
        result = await safe_activity(item)
        if result.ok:
            results.append(result.value)
        else:
            # Handle error without failing the workflow
            await log_error(item, result.error)
    
    if not results:
        return Result.Error("All items failed")
    
    return Result.Ok(results)
```

### Using Try/Except

```python
@Stent.durable
async def orchestrator_with_exceptions(data: dict) -> dict:
    try:
        step1 = await first_step(data)
    except ValidationError as e:
        # Handle specific error type
        await notify_validation_failure(data, str(e))
        raise  # Re-raise to fail the workflow
    
    try:
        step2 = await second_step(step1)
    except TemporaryError as e:
        # Retry manually
        await Stent.sleep("5s")
        step2 = await second_step(step1)
    
    return step2
```

### Partial Failure Handling

```python
import asyncio
from stent import Result

@Stent.durable
async def batch_with_partial_failure(items: list[dict]) -> Result[dict, None]:
    tasks = [process_item(item) for item in items]
    
    # Use return_exceptions to capture all results
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    successes = []
    failures = []
    
    for item, result in zip(items, results):
        if isinstance(result, Exception):
            failures.append({"item": item, "error": str(result)})
        else:
            successes.append(result)
    
    # Always succeed, but report failures
    return Result.Ok({
        "succeeded": len(successes),
        "failed": len(failures),
        "results": successes,
        "errors": failures
    })
```

## Dead Letter Queue

Tasks that fail after all retries are moved to the Dead Letter Queue (DLQ).

### Why Use DLQ?

- **Investigation**: Examine why tasks failed
- **Recovery**: Retry tasks after fixing issues
- **Monitoring**: Alert on DLQ growth
- **Audit**: Track failure history

### Viewing Dead Letters

```python
# List recent dead letters
dead_letters = await executor.list_dead_letters(limit=50)

for record in dead_letters:
    print(f"Task: {record.task.step_name}")
    print(f"Execution: {record.task.execution_id}")
    print(f"Reason: {record.reason}")
    print(f"Retries: {record.task.retries}")
    print(f"Moved at: {record.moved_at}")
    print("---")
```

### Getting Dead Letter Details

```python
record = await executor.get_dead_letter(task_id)

if record:
    task = record.task
    print(f"Function: {task.step_name}")
    print(f"Arguments: {executor.serializer.loads(task.args)}")
    print(f"Error: {executor.serializer.loads(task.error)}")
```

### Replaying Dead Letters

```python
# Replay with default settings
new_task_id = await executor.replay_dead_letter(task_id)

# Replay to a different queue
new_task_id = await executor.replay_dead_letter(
    task_id,
    queue="retry"  # Send to retry queue
)

# Replay without resetting retry count
new_task_id = await executor.replay_dead_letter(
    task_id,
    reset_retries=False
)
```

### Discarding Dead Letters

```python
# Remove from DLQ without replaying
success = await executor.discard_dead_letter(task_id)
```

### CLI for DLQ Management

```bash
# List dead letters
stent dlq list
stent dlq list --limit 100

# Show details
stent dlq show <task_id>

# Replay
stent dlq replay <task_id>
stent dlq replay <task_id> --queue retry
```

## Workflow Timeouts

### Setting Execution Timeout

```python
# Workflow must complete within 1 hour
exec_id = await executor.dispatch(
    my_workflow,
    data,
    expiry="1h"
)

# Using timedelta
from datetime import timedelta
exec_id = await executor.dispatch(
    my_workflow,
    data,
    expiry=timedelta(hours=2)
)
```

### Timeout Behavior

When a workflow times out:

1. Execution state becomes "timed_out"
2. Currently running task is marked failed
3. Task moved to DLQ with timeout reason
4. `wait_for()` returns immediately

```python
result = await executor.wait_for(exec_id)

state = await executor.state_of(exec_id)
if state.state == "timed_out":
    print("Workflow timed out!")
```

### Timeout with Delay

When using both `delay` and `expiry`:

```python
# Start in 30 minutes, then has 1 hour to complete
exec_id = await executor.dispatch(
    my_workflow,
    data,
    delay="30m",
    expiry="1h"
)
# Timeout occurs at: now + 30m + 1h = 1.5 hours from now
```

## Compensation Patterns

### Simple Compensation

```python
@Stent.durable
async def workflow_with_cleanup(data: dict) -> Result[dict, str]:
    resource = None
    
    try:
        # Acquire resource
        resource = await acquire_resource(data)
        
        # Do work
        result = await do_work(resource)
        
        return Result.Ok(result)
    except Exception as e:
        return Result.Error(str(e))
    finally:
        # Always cleanup
        if resource:
            await release_resource(resource)
```

### Saga Pattern

```python
@Stent.durable
async def distributed_transaction(order: dict) -> Result[dict, str]:
    compensations = []
    
    try:
        # Step 1
        inventory = await reserve_inventory(order["items"])
        compensations.append(lambda: release_inventory(inventory["id"]))
        
        # Step 2
        payment = await charge_payment(order["customer_id"], order["total"])
        compensations.append(lambda: refund_payment(payment["id"]))
        
        # Step 3 (last step - no compensation needed)
        shipment = await create_shipment(order["id"], inventory["id"])
        
        return Result.Ok({
            "order_id": order["id"],
            "shipment_id": shipment["id"]
        })
        
    except Exception as e:
        # Run compensations in reverse order
        for compensate in reversed(compensations):
            try:
                await compensate()
            except Exception as comp_error:
                # Log but continue with other compensations
                await log_compensation_error(comp_error)
        
        return Result.Error(f"Transaction failed: {e}")
```

## Best Practices

### 1. Match Retry Policy to Failure Mode

```python
# Transient network errors: aggressive retries
@Stent.durable(
    retry_policy=RetryPolicy(
        max_attempts=10,
        initial_delay=0.1,
        retry_for=(ConnectionError, TimeoutError)
    )
)
async def network_call():
    ...

# Rate limiting: slower retries
@Stent.durable(
    retry_policy=RetryPolicy(
        max_attempts=5,
        initial_delay=60.0,
        backoff_factor=2.0,
        retry_for=(RateLimitError,)
    )
)
async def rate_limited_api():
    ...

# Data errors: no retries
@Stent.durable(
    retry_policy=RetryPolicy(max_attempts=1)
)
async def process_data(data: dict):
    ...
```

### 2. Make Operations Idempotent

```python
@Stent.durable(
    idempotent=True,
    retry_policy=RetryPolicy(max_attempts=5)
)
async def safe_to_retry(order_id: str) -> str:
    # Use idempotency key with external system
    return await payment_api.charge(
        order_id=order_id,
        idempotency_key=f"charge:{order_id}"
    )
```

### 3. Monitor DLQ Growth

```python
async def monitor_dlq():
    while True:
        dead_letters = await executor.list_dead_letters(limit=1)
        dlq_count = len(dead_letters)
        
        if dlq_count > THRESHOLD:
            await alert_team(f"DLQ has {dlq_count} items!")
        
        await asyncio.sleep(60)
```

### 4. Log Context for Debugging

```python
import logging
from stent import install_structured_logging

# Add Stent context to all logs
install_structured_logging(logging.getLogger())

@Stent.durable
async def logged_operation(data: dict) -> dict:
    logger.info("Processing started", extra={"data_id": data["id"]})
    # Logs will include stent_execution_id, stent_task_id
    ...
```

### 5. Test Error Paths

```python
import pytest

@pytest.mark.asyncio
async def test_workflow_handles_failure():
    # Setup with a function that will fail
    @Stent.durable(retry_policy=RetryPolicy(max_attempts=1))
    async def failing_activity():
        raise ValueError("Expected failure")
    
    @Stent.durable
    async def workflow():
        try:
            await failing_activity()
        except ValueError:
            return "handled"
        return "unexpected"
    
    # Run and verify error handling
    exec_id = await executor.dispatch(workflow)
    result = await executor.wait_for(exec_id)
    assert result.value == "handled"
```
