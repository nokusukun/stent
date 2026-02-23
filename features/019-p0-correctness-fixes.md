# P0 Correctness Fixes

## Description
Implemented the P0 correctness items from `PLAN-2-0.md` to make retry timing, idempotency hits, timeout handling, and cancelled execution behavior consistent and predictable. These changes prevent early retry pickup, preserve falsey idempotent results, and ensure wait APIs fail with timeout errors instead of returning stale non-terminal state.

## Key Changes
* `stent/executor.py`
  * Retry path now schedules retries with `scheduled_for` and clears lease ownership fields.
  * Idempotency check now treats falsey serialized payloads as valid hits via `is not None`.
  * Notification-based task wait converts timeout to `ExpiryError` and rejects non-terminal post-notification task state.
  * Notification-based execution wait now validates terminal state after subscription and raises `ExpiryError` on expiry.
  * `result_of` now treats `cancelled` as terminal and returns `Result.Error(Exception("Execution cancelled"))` when no serialized error is present.
* `stent/notifications/redis.py`
  * Redis subscription helpers now re-raise `asyncio.TimeoutError` instead of swallowing it.
* `stent/backend/sqlite.py`
  * `update_task` now persists `scheduled_for` updates.
* `stent/backend/postgres.py`
  * `update_task` now persists `scheduled_for` updates.
* `tests/test_backend_correctness.py`
  * Added scheduled claimability test to verify tasks are not claimable before `scheduled_for` and become claimable after due time.
* `tests/test_execution.py`
  * Added retry-delay regression test ensuring pending retries carry `scheduled_for` and not `lease_expires_at`.
  * Added idempotency regression test for falsey results (`0`, `False`, `""`, `[]`, `{}`).
* `tests/test_wait_for.py`
  * Added notification-timeout regression tests for task and execution waits.
  * Added cancelled terminal-state consistency test for `wait_for` and `result_of`.

## Usage/Configuration
```python
# Retry delays are now honored via scheduled_for.
@Stent.durable(retry_policy=RetryPolicy(max_attempts=3, initial_delay=1.0))
async def flaky_step(x: int) -> int:
    ...

# Falsey idempotent results are treated as cache hits.
@Stent.durable(idempotent=True)
async def compute_flag(key: str) -> bool:
    return False

# wait_for() and internal task waits now raise ExpiryError on expiry
# even when notification backends do not deliver terminal updates.
result = await executor.wait_for(execution_id, expiry=5.0)
```
