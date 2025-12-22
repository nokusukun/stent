# Scheduling & Efficient Sleep

## Description
This feature introduces the ability to schedule tasks for future execution and provides a mechanism for efficient sleeping within workflows. 

Previously, using `time.sleep` or `asyncio.sleep` inside a worker would block the worker process, preventing it from handling other tasks. The new `senpuki.sleep` releases the worker capacity while waiting.

Additionally, `asyncio.sleep` is now monkey-patched to emit a warning if used inside a durable function, guiding developers to use `senpuki.sleep`.

## Key Changes
*   `senpuki/backend/sqlite.py`: Added `scheduled_for` column to `tasks` table and updated `claim_next_task` to respect it.
*   `senpuki/core.py`: Added `scheduled_for` to `TaskRecord`.
*   `senpuki/executor.py`:
    *   Added `schedule(delay, fn, ...)` method to `Senpuki`.
    *   Added `sleep(duration)` method to `Senpuki`.
    *   Implemented `_builtin_sleep` durable function (noop).
    *   Monkey-patched `asyncio.sleep` to warn on misuse.
    *   Updated internal loops (`serve`, `_wait_for_task`) to use `_original_sleep` to avoid warnings.
*   `senpuki/utils/time.py`: Improved `parse_duration` to support composite strings (e.g., "2d8h") and dicts.

## Usage/Configuration

### Scheduling a Task
```python
# Run 'my_task' after 2 days and 8 hours
await senpuki.schedule("2d8h", my_task, arg1, arg2)

# Using a dict
await senpuki.schedule({"minutes": 30}, my_task)
```

### Efficient Sleep
Inside a durable workflow:
```python
import senpuki

@Senpuki.durable()
async def my_workflow():
    do_something()
    # sleeps for 1 hour, releasing the worker
    await senpuki.sleep("1h") 
    do_something_else()
```
The global `senpuki.sleep` helper automatically detects the current execution context.

### Warnings
If a user writes:
```python
@Senpuki.durable()
async def bad_workflow():
    await asyncio.sleep(60) # WARN: Blocking worker
```
They will see a warning in the logs:
`Detected asyncio.sleep(60) inside a durable function. Use 'await ctx.sleep(...)' or 'await senpuki.sleep(...)' to release worker capacity.`
