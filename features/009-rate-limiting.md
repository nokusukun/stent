# Task Rate Limiting (Concurrency Control)

## Description
This feature enables limiting the maximum number of concurrent executions for specific durable functions across the entire cluster. This is useful for managing resource-intensive tasks (e.g., GPU jobs) or rate-limiting external API calls (e.g., "max 5 concurrent requests").

## Key Changes
*   **`dfns/registry.py`**: Updated `FunctionMetadata` to include `max_concurrent`.
*   **`dfns/executor.py`**:
    *   Updated `@DFns.durable` decorator to accept `max_concurrent` argument.
    *   Updated `DFns.serve` to collect concurrency limits and pass them to the backend.
*   **`dfns/backend/base.py`**: Updated `claim_next_task` protocol to accept `concurrency_limits`.
*   **`dfns/backend/sqlite.py` & `dfns/backend/postgres.py`**: Implemented `claim_next_task` logic to enforce concurrency limits by checking active task counts before claiming.

## Usage/Configuration
You can apply a concurrency limit using the `max_concurrent` parameter in the `@DFns.durable` decorator.

```python
from dfns import DFns

# Limit to 1 concurrent execution globally
@DFns.durable(max_concurrent=1)
async def exclusive_task(x: int):
    # ... expensive operation ...
    pass

# Limit to 5 concurrent executions
@DFns.durable(max_concurrent=5)
async def rate_limited_api_call(url: str):
    # ... call external API ...
    pass
```

The worker (`DFns.serve`) automatically respects these limits when claiming tasks.
