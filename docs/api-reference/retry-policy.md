# RetryPolicy API Reference

The `RetryPolicy` dataclass configures how Stent handles failures and retries for durable functions.

## Import

```python
from stent import RetryPolicy
```

## Definition

```python
@dataclass
class RetryPolicy:
    max_attempts: int = 3
    backoff_factor: float = 2.0
    initial_delay: float = 1.0
    max_delay: float = 60.0
    jitter: float = 0.1
    retry_for: tuple[type[BaseException], ...] = (Exception,)
```

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `max_attempts` | `int` | `3` | Maximum number of attempts (including initial) |
| `backoff_factor` | `float` | `2.0` | Multiplier for exponential backoff |
| `initial_delay` | `float` | `1.0` | Delay before first retry (seconds) |
| `max_delay` | `float` | `60.0` | Maximum delay between retries (seconds) |
| `jitter` | `float` | `0.1` | Random jitter factor (0-1) to prevent thundering herd |
| `retry_for` | `tuple[type[BaseException], ...]` | `(Exception,)` | Exception types to retry on |

## Retry Delay Calculation

The delay between retries is calculated as:

```
delay = initial_delay * (backoff_factor ^ (attempt - 1))
delay = min(delay, max_delay)
delay = delay + random(-delay * jitter, delay * jitter)
```

### Example Delays

With default settings (`initial_delay=1.0`, `backoff_factor=2.0`, `max_delay=60.0`):

| Attempt | Base Delay | With Max Cap |
|---------|------------|--------------|
| 1 | 1.0s | 1.0s |
| 2 | 2.0s | 2.0s |
| 3 | 4.0s | 4.0s |
| 4 | 8.0s | 8.0s |
| 5 | 16.0s | 16.0s |
| 6 | 32.0s | 32.0s |
| 7 | 64.0s | 60.0s (capped) |

## Usage Examples

### Basic Retry Policy

```python
from stent import Stent, RetryPolicy

@Stent.durable(
    retry_policy=RetryPolicy(max_attempts=3)
)
async def flaky_operation():
    # Will retry up to 3 times with default backoff
    ...
```

### Aggressive Retries for Transient Errors

```python
@Stent.durable(
    retry_policy=RetryPolicy(
        max_attempts=10,
        initial_delay=0.5,
        backoff_factor=1.5,
        max_delay=30.0,
        jitter=0.2
    )
)
async def network_call():
    # Quick initial retries, slower backoff, more jitter
    ...
```

### Selective Retry by Exception Type

```python
@Stent.durable(
    retry_policy=RetryPolicy(
        max_attempts=5,
        retry_for=(ConnectionError, TimeoutError, IOError)
    )
)
async def external_api_call():
    # Only retries on specific network errors
    # ValueError, KeyError, etc. will fail immediately
    ...
```

### No Retries

```python
@Stent.durable(
    retry_policy=RetryPolicy(max_attempts=1)
)
async def critical_operation():
    # No retries - fails immediately on error
    ...
```

### Long-Running with Conservative Retries

```python
@Stent.durable(
    retry_policy=RetryPolicy(
        max_attempts=3,
        initial_delay=60.0,      # Start with 1 minute
        backoff_factor=3.0,       # Triple each time
        max_delay=3600.0,         # Cap at 1 hour
        jitter=0.3
    )
)
async def batch_import():
    # For operations where you want significant delays between retries
    # Delays: ~1min, ~3min, ~9min (capped at 1hr)
    ...
```

## Retry Behavior

### What Happens on Failure

1. Function raises an exception
2. Stent checks if exception type matches `retry_for`
3. If retryable and attempts remaining:
   - Task state set to "pending"
   - Retry delay calculated
   - Task scheduled for retry after delay
4. If not retryable or max attempts reached:
   - Task state set to "failed"
   - Task moved to Dead Letter Queue

### Exception Matching

The `retry_for` parameter uses `isinstance()` checking:

```python
# This will retry on any Exception subclass
retry_for=(Exception,)

# This will retry on connection-related errors
retry_for=(ConnectionError, TimeoutError, OSError)

# This will retry on HTTP errors (if using httpx/aiohttp)
retry_for=(httpx.HTTPStatusError, httpx.ConnectError)
```

### Dead Letter Queue

When a task exhausts all retries, it's moved to the Dead Letter Queue (DLQ):

```python
# List failed tasks
dead_letters = await executor.list_dead_letters()

for record in dead_letters:
    print(f"Task: {record.task.step_name}")
    print(f"Reason: {record.reason}")
    print(f"Retries: {record.task.retries}")

# Replay a dead-lettered task
new_task_id = await executor.replay_dead_letter(
    task_id,
    queue="retry",      # Optionally change queue
    reset_retries=True  # Reset retry counter
)
```

## Common Patterns

### Different Policies for Different Activities

```python
# Retry network errors aggressively
@Stent.durable(
    retry_policy=RetryPolicy(
        max_attempts=5,
        initial_delay=0.5,
        retry_for=(ConnectionError, TimeoutError)
    )
)
async def fetch_data(url: str):
    ...

# Be conservative with external APIs
@Stent.durable(
    retry_policy=RetryPolicy(
        max_attempts=3,
        initial_delay=5.0,
        backoff_factor=3.0
    )
)
async def call_external_api(data: dict):
    ...

# No retries for idempotent operations that shouldn't repeat
@Stent.durable(
    retry_policy=RetryPolicy(max_attempts=1),
    idempotent=True
)
async def process_payment(order_id: str):
    ...
```

### Combining with Idempotency

```python
@Stent.durable(
    idempotent=True,  # Ensures exactly-once execution
    retry_policy=RetryPolicy(
        max_attempts=5,
        retry_for=(NetworkError,)
    )
)
async def charge_customer(customer_id: str, amount: int):
    # Safe to retry - idempotency prevents duplicate charges
    ...
```

## Jitter Explanation

Jitter adds randomness to retry delays to prevent the "thundering herd" problem, where many failed tasks retry at exactly the same time.

```python
# With jitter=0.1 and base delay of 10 seconds:
# Actual delay will be between 9-11 seconds (10 ± 10%)

# With jitter=0.3 and base delay of 10 seconds:
# Actual delay will be between 7-13 seconds (10 ± 30%)
```

This is especially important when:
- Many tasks depend on the same external service
- The external service might be recovering from an outage
- You want to spread retry load over time

## Best Practices

1. **Match retry policy to failure mode**
   - Transient network errors: More retries, shorter initial delay
   - Rate limiting: Fewer retries, longer delays
   - Data validation: No retries (max_attempts=1)

2. **Use appropriate jitter**
   - Higher jitter (0.2-0.3) for many concurrent tasks
   - Lower jitter (0.05-0.1) for isolated operations

3. **Set reasonable max_delay**
   - Consider your workflow's timeout
   - Account for all retries fitting within the timeout

4. **Be specific with retry_for**
   - Don't retry on validation errors
   - Do retry on network/transient errors
   - Consider what errors are actually recoverable

5. **Combine with idempotency**
   - Retrying non-idempotent operations can cause duplicates
   - Use `idempotent=True` for operations with side effects
