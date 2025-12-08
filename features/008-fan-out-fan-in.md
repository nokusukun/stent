# Fan-out / Fan-in Support

## Description
Introduced `DFns.map` and `DFns.gather` for efficient parallel execution of tasks. `DFns.map` specifically optimizes the scheduling process by batching database writes, significantly improving performance when dispatching large numbers of tasks.

## Key Changes
*   **Executor:** Added `DFns.map` and `DFns.gather` methods.
*   **Backend:** Added `create_tasks` method to `Backend` protocol and implementations (`SQLiteBackend`, `PostgresBackend`) for batch insertion.
*   **API:** `DFns.map(func, iterable)` creates all tasks in a single DB transaction.

## Usage/Configuration
```python
@DFns.durable()
async def process_item(item: int) -> int:
    return item * 2

@DFns.durable()
async def workflow(items: list[int]) -> list[int]:
    # Efficiently schedules all tasks in one batch
    results = await DFns.map(process_item, items)
    return results
```
