# Executor Dispatch and Map Helper Extraction

## Description
Completed the P2.3 dispatch/map extraction slice by moving batching and dispatch-orchestration logic out of `executor.py` into a dedicated helper module, with no public API or behavior change.

## Key Changes
* `stent/executor_orchestration.py`
  * Added `execute_map_batch` to encapsulate map batching flow (cache/idempotency checks, batch task creation, shared permit release/reacquire, result/error handling).
  * Added dispatch helpers: `normalize_dispatch_timing`, `build_dispatch_records`, and `persist_dispatch_records`.
  * Added `schedule_activity_and_wait` for shared activity scheduling + progress + wait flow.
* `stent/executor.py`
  * `map` now delegates to `execute_map_batch`.
  * `dispatch` now delegates timing normalization, record construction, and persistence.
  * `_schedule_activity` now delegates to `schedule_activity_and_wait`.
  * Removed imports no longer needed after extraction.

## Usage/Configuration
```python
# Public API remains unchanged.
execution_id = await executor.dispatch(workflow_fn, arg1, delay="5s")
values = await Stent.map(activity_fn, items)
```
