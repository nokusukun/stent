# Scheduling & Efficient Sleep

## Description
This feature introduces the ability to schedule tasks for future execution and provides a mechanism for efficient sleeping within workflows. 

Previously, using `time.sleep` or `asyncio.sleep` inside a worker would block the worker process, preventing it from handling other tasks. The new `stent.sleep` releases the worker capacity while waiting.

Additionally, `asyncio.sleep` is now monkey-patched to emit a warning if used inside a durable function, guiding developers to use `stent.sleep`.

## Key Changes
*   `stent/backend/sqlite.py`: Added `scheduled_for` column to `tasks` table and updated `claim_next_task` to respect it.
*   `stent/core.py`: Added `scheduled_for` to `TaskRecord`.
*   `stent/executor.py`:
    *   Added `schedule(delay, fn, ...)` method to `Stent`.
    *   Added `sleep(duration)` method to `Stent`.
    *   Implemented `_builtin_sleep` durable function (noop).
    *   Monkey-patched `asyncio.sleep` to warn on misuse.
    *   Updated internal loops (`serve`, `_wait_for_task`) to use `_original_sleep` to avoid warnings.
*   `stent/utils/time.py`: Improved `parse_duration` to support composite strings (e.g., "2d8h") and dicts.

## Usage/Configuration

### Scheduling a Task
```python
# Run 'my_task' after 2 days and 8 hours
await stent.schedule("2d8h", my_task, arg1, arg2)

# Using a dict
await stent.schedule({"minutes": 30}, my_task)
```

### Efficient Sleep
Inside a durable workflow:
```python
import stent

@Stent.durable()
async def my_workflow():
    do_something()
    # sleeps for 1 hour, releasing the worker
    await stent.sleep("1h") 
    do_something_else()
```
The global `stent.sleep` helper automatically detects the current execution context.

### Warnings
If a user writes:
```python
@Stent.durable()
async def bad_workflow():
    await asyncio.sleep(60) # WARN: Blocking worker
```
They will see a warning in the logs:
`Detected asyncio.sleep(60) inside a durable function. Use 'await ctx.sleep(...)' or 'await stent.sleep(...)' to release worker capacity.`
