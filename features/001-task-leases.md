# Task Leases & Recovery

## Description
This feature introduces a lease mechanism for tasks, ensuring resilience against worker crashes. If a worker fails while processing a task, the task's lease will eventually expire, allowing another worker to reclaim and restart the task. This prevents tasks from getting stuck in an indefinite `running` state.

## Key Changes
*   `stent/backend/base.py`: Added `lease_duration` parameter to `claim_next_task` and updated `Backend` protocol.
*   `stent/backend/sqlite.py`: Implemented SQL logic to handle task leasing, including claiming tasks with a lease, checking for expired leases, and reclaiming tasks.
*   `stent/executor.py`: Updated `serve` function to incorporate lease management, setting lease durations, and handling task reclamation logic.
*   `tests/test_execution.py`: Added `test_lease_expiration_crash` to verify the lease mechanism.

## Usage/Configuration
Workers acquire leases for tasks when `serve()` is called. The `lease_duration` can be configured:

```python
from datetime import timedelta

# Start a worker with a 5-minute lease (default)
await executor.serve()

# Start a worker with a custom lease duration
await executor.serve(lease_duration=timedelta(seconds=30))
```
