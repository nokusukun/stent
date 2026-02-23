# Execution Context Counters and Custom State

## Description
Added workflow execution context support for runtime counters and custom key-value state so durable functions can persist progress-like values and arbitrary serialized state during execution.

## Key Changes
* `stent/executor.py`
  * Added `Stent.context(...)` for accessing execution-scoped context inside durable functions.
  * `state_of(...)` now returns `counters` and `custom_state` when available.
* `stent/executor_context.py`
  * Added `ExecutionContext` with:
    * `ctx.counters(name).add(amount)`
    * `ctx.state(key).set(value)`
  * Added async operation tracking and flush-on-task-finalization support.
* `stent/executor_worker.py`
  * Binds and flushes execution context around task execution to persist context updates.
* `stent/backend/base.py`
  * Added backend protocol methods for execution counters and custom state storage.
* `stent/backend/sqlite.py`
  * Added `execution_counters` and `execution_state` tables and CRUD helpers.
  * Extended cleanup to remove execution context rows with execution deletion.
* `stent/backend/postgres.py`
  * Added `execution_counters` and `execution_state` tables and CRUD helpers.
  * Extended cleanup to remove execution context rows with execution deletion.
* `stent/core.py`
  * Extended `ExecutionState` with `counters` and `custom_state` fields.
* `tests/test_context.py`
  * Added regression coverage for initializing counters/state, nested updates, and `state_of` visibility.

## Usage/Configuration
```python
@Stent.durable()
async def bar():
    ctx = Stent.context()
    ctx.counters("progress").add(1)
    ctx.counters("abc").add(40)
    ctx.state("phase").set("bar_done")


@Stent.durable()
async def foo():
    ctx = Stent.context(counters={"progress": 0, "volume": 30}, state={"owner": "foo"})
    await bar()


# Later
state = await executor.state_of(execution_id)
print(state.counters)
print(state.custom_state)
```
