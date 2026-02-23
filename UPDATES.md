# Recent Updates: Resilience & Maintenance

This document outlines the recent improvements made to the Stent framework, focusing on task resilience (handling worker crashes) and database maintenance (automatic cleanup).

## 1. Task Resilience & Recovery

### The Problem
Previously, if a worker process crashed while executing a task, that task would remain in the `running` state indefinitely. The orchestrator would wait forever for a result that would never come, effectively freezing the execution.

### The Solution: Task Leases
We have introduced a **Lease Mechanism** for task execution.
- When a worker claims a task, it acquires a "lease" for a specific duration.
- If the worker successfully completes the task, the result is saved as normal.
- If the worker crashes (or hangs), the lease eventually expires.
- Other workers periodically check for tasks with expired leases and "steal" them, resetting the state to `running` with a new lease.

### Configuration
The lease duration is configurable when starting a worker using `serve()`.

```python
from datetime import timedelta

# Start a worker with a 5-minute lease (default)
await executor.serve()

# Start a worker with a custom short lease (e.g., for quick tasks)
await executor.serve(lease_duration=timedelta(seconds=30))
```

> **Note:** The `lease_duration` should be longer than the expected execution time of your longest task to avoid unnecessary "stealing" of active tasks.

---

## 2. Database Compaction (Auto-Cleanup)

### The Problem
As workflows run, the database accumulates history of executions, tasks, and progress logs. Over time, this data grows indefinitely, potentially impacting performance and consuming storage space on the server.

### The Solution: Background Cleanup Loop
We added a background maintenance loop to the worker process. This loop periodically deletes execution records that:
1. Are in a **terminal state** (`completed`, `failed`, `timed_out`, `cancelled`).
2. Have a `completed_at` timestamp older than the configured **retention period**.

### Configuration
You can control the cleanup behavior via arguments to `serve()`.

```python
from datetime import timedelta

await executor.serve(
    # Check for cleanup every hour (default is 3600s)
    cleanup_interval=3600,
    
    # Keep history for 7 days (default)
    retention_period=timedelta(days=7)
)
```

To disable cleanup (e.g., for archival or debugging purposes), set `cleanup_interval=None`.

```python
# Disable auto-cleanup
await executor.serve(cleanup_interval=None)
```

---

## 3. Rate Limiting (Concurrency Control)

### The Problem
Some tasks require limiting global concurrency (e.g., GPU tasks, API calls to rate-limited services). Previously, only worker-local concurrency was limited via `max_concurrency` (semaphore).

### The Solution: Global Concurrency Limits
We added support for **global/cluster-wide concurrency limits** for specific durable functions.

### Configuration
Use the `max_concurrent` parameter in the `@durable` decorator.

```python
# Limit to 1 concurrent instance globally
@Stent.durable(max_concurrent=1)
async def exclusive_task(x: int):
    pass
```

The system automatically enforces this limit across all workers connected to the same backend.

## Summary of `serve()` Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `lease_duration` | `timedelta` | 5 mins | Time before a running task is considered crashed and reclaimed. |
| `cleanup_interval` | `float` | 3600.0 (1h) | How often (in seconds) to run the cleanup job. `None` to disable. |
| `retention_period` | `timedelta` | 7 days | How long to keep completed execution history. |