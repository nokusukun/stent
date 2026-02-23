# Worker Tag Filtering

## Description
Implemented worker tag filtering in task-claim logic so worker `serve(..., tags=[...])` actually constrains which tagged tasks can be claimed. This enables targeted worker pools while preserving default behavior for workers that do not specify tags.

## Key Changes
* `stent/backend/sqlite.py`
  * Added tag eligibility filtering inside `claim_next_task` candidate selection.
* `stent/backend/postgres.py`
  * Added matching tag eligibility filtering inside `claim_next_task` candidate selection.
* `tests/test_execution.py`
  * Added end-to-end worker tag filtering test that verifies:
    * alpha-tagged worker claims alpha tasks
    * untagged tasks remain globally eligible
    * beta-tagged tasks remain pending until a beta worker starts

## Usage/Configuration
```python
# Worker only claims tasks tagged with "alpha" plus untagged tasks.
worker = asyncio.create_task(executor.serve(tags=["alpha"], poll_interval=0.1))

# Durable task tags.
@Stent.durable(tags=["alpha"])
async def alpha_only_step(payload: str) -> str:
    return payload
```
