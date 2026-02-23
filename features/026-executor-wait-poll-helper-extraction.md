# Executor Wait/Poll Helper Extraction

## Description
Started P2.3 by extracting wait/poll logic from `Stent` into a focused helper module while keeping public API and runtime behavior unchanged.

## Key Changes
* `stent/executor_wait.py`
  * Added shared wait helpers for task-terminal and execution-terminal waits.
  * Centralized notification-vs-poll fallback behavior and terminal state definitions.
* `stent/executor.py`
  * Replaced inline wait/poll implementations in `_wait_for_task_internal` and `wait_for` with calls to the new helpers.
  * Kept timeout/error semantics and polling behavior aligned with prior implementation.

## Usage/Configuration
```python
# Public usage is unchanged.
result = await executor.wait_for(execution_id, expiry=5.0)
task = await executor._wait_for_task_internal(task_id, expiry=1.0)
```
