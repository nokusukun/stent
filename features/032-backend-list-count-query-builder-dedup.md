# Backend List/Count Query Builder Dedup

## Description
Continued P2.4 by extracting shared list/count query-builder helpers used by SQLite and Postgres for common filtered reads, while preserving backend-specific SQL execution semantics.

## Key Changes
* `stent/backend/utils.py`
  * Added reusable query helpers:
    * `build_filtered_query`
    * `build_filtered_count_query`
    * `build_filtered_list_query`
  * Added placeholder helpers for dialect-specific parameter tokens:
    * `qmark_placeholder` (SQLite)
    * `dollar_placeholder` (Postgres)
* `stent/backend/sqlite.py`
  * Switched `list_executions`, `count_executions`, `list_tasks`, and `count_tasks` to shared builders.
* `stent/backend/postgres.py`
  * Switched `list_executions`, `count_executions`, `list_tasks`, and `count_tasks` to shared builders.

## Usage/Configuration
```python
# Public API unchanged.
executions = await backend.list_executions(limit=20, state="running")
pending = await backend.count_tasks(state="pending")
```
