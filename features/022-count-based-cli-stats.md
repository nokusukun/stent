# Count-Based CLI Stats

## Description
Replaced scan-heavy CLI statistics paths with backend count APIs for execution and dead-letter totals. This makes stats/watch operations more efficient and avoids loading large record sets just to compute counts.

## Key Changes
* `stent/backend/base.py`
  * Added backend protocol methods:
    * `count_executions(state: str | None = None) -> int`
    * `count_dead_tasks() -> int`
* `stent/backend/sqlite.py`
  * Implemented `count_executions` and `count_dead_tasks` with SQL `COUNT(*)` queries.
* `stent/backend/postgres.py`
  * Implemented `count_executions` and `count_dead_tasks` with SQL `COUNT(*)` queries.
* `stent/cli.py`
  * `stats` now uses `count_executions` per state instead of `list_executions(limit=10000)`.
  * `stats` now uses `count_dead_tasks` instead of listing DLQ entries to count them.
  * `watch` modes now use `count_dead_tasks` for DLQ totals.
* `tests/test_backend_correctness.py`
  * Added coverage for the new SQLite count APIs.
* `tests/test_cli.py`
  * Added regression test asserting `stats` uses count APIs rather than scan/list APIs.

## Usage/Configuration
```bash
# Existing CLI commands are unchanged.
stent stats
stent watch
```
