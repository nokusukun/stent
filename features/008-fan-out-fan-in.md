# Fan-out / Fan-in Support

## Description
Introduced `Senpuki.map` and `Senpuki.gather` for efficient parallel execution of tasks. `Senpuki.map` specifically optimizes the scheduling process by batching database writes, significantly improving performance when dispatching large numbers of tasks.

## Key Changes
*   **Executor:** Added `Senpuki.map` and `Senpuki.gather` methods.
*   **Backend:** Added `create_tasks` method to `Backend` protocol and implementations (`SQLiteBackend`, `PostgresBackend`) for batch insertion.
*   **API:** `Senpuki.map(func, iterable)` creates all tasks in a single DB transaction.

## Usage/Configuration
```python
@Senpuki.durable()
async def process_item(item: int) -> int:
    return item * 2

@Senpuki.durable()
async def workflow(items: list[int]) -> list[int]:
    # Efficiently schedules all tasks in one batch
    results = await Senpuki.map(process_item, items)
    return results
```
