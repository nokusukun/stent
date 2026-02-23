# Database Compaction (Auto-Cleanup)

## Description
This feature implements an automatic database cleanup mechanism to manage the growth of execution history. A background loop in the worker process periodically deletes execution records that are in a terminal state (`completed`, `failed`, `timed_out`, `cancelled`) and are older than a specified `retention_period`.

## Key Changes
*   `stent/backend/base.py`: Added `cleanup_executions` method signature to the `Backend` protocol.
*   `stent/backend/sqlite.py`: Implemented the SQL logic for `cleanup_executions` to delete old records based on their state and `completed_at` timestamp.
*   `stent/executor.py`: Integrated a background `_cleanup_loop` into the `serve` function, responsible for periodically invoking the `cleanup_executions` method.
*   `stent/utils/async_sqlite.py`: Added `rowcount` property to `AsyncCursor` to support the cleanup logic, allowing the backend to report the number of deleted records.
*   `tests/test_execution.py`: Added `test_cleanup` to verify the database cleanup functionality.

## Usage/Configuration
The cleanup process is enabled by default when calling `serve()`. It can be configured using `cleanup_interval` and `retention_period`.

```python
from datetime import timedelta

await executor.serve(
    # Run cleanup every 2 hours
    cleanup_interval=7200, 
    
    # Retain history for 30 days
    retention_period=timedelta(days=30)
)

# To disable cleanup:
await executor.serve(cleanup_interval=None)
```
