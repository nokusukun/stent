# Executor Worker Task Handling Extraction

## Description
Continued P2.3 by extracting worker-task execution and lease heartbeat behavior from `executor.py` into a focused worker helper module, preserving public API and runtime behavior.

## Key Changes
* `stent/executor_worker.py`
  * Added `lease_heartbeat_loop` to manage periodic lease renewals and metrics updates.
  * Added `run_claimed_task` to handle claimed task execution flow (state checks, orchestrator transitions, retries, dead-lettering, notifications, metrics).
* `stent/executor.py`
  * `_lease_heartbeat_loop` now delegates to `executor_worker.lease_heartbeat_loop`.
  * `_handle_task` now delegates core task lifecycle logic to `executor_worker.run_claimed_task` while keeping context-var/semaphore handling local.
  * Removed imports no longer needed after extraction.

## Usage/Configuration
```python
# Public API remains unchanged.
await executor.serve(max_concurrency=10)
```
