# Fan-out / Fan-in Support

## Description
Introduced `Stent.map` and `Stent.gather` for efficient parallel execution of tasks. `Stent.map` specifically optimizes the scheduling process by batching database writes, significantly improving performance when dispatching large numbers of tasks.

## Key Changes
*   **Executor:** Added `Stent.map` and `Stent.gather` methods.
*   **Backend:** Added `create_tasks` method to `Backend` protocol and implementations (`SQLiteBackend`, `PostgresBackend`) for batch insertion.
*   **API:** `Stent.map(func, iterable)` creates all tasks in a single DB transaction.

## Usage/Configuration
```python
@Stent.durable()
async def process_item(item: int) -> int:
    return item * 2

@Stent.durable()
async def workflow(items: list[int]) -> list[int]:
    # Efficiently schedules all tasks in one batch
    results = await Stent.map(process_item, items)
    return results
```
