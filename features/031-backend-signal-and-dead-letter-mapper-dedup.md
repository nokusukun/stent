# Backend Signal and Dead-Letter Mapper Dedup

## Description
Continued P2.4 by deduplicating signal and dead-letter row mapping logic shared by SQLite and Postgres backends, while keeping SQL execution and transaction behavior backend-specific.

## Key Changes
* `stent/backend/utils.py`
  * Added `row_to_signal` for consistent `SignalRecord` conversion across backends.
  * Added `row_to_dead_letter` for consistent `DeadLetterRecord` conversion from stored JSON payloads.
* `stent/backend/sqlite.py`
  * Updated `get_signal` and dead-letter mapping to delegate to shared helpers.
* `stent/backend/postgres.py`
  * Updated `get_signal` and dead-letter mapping to delegate to shared helpers.

## Usage/Configuration
```python
# No API changes.
signal = await backend.get_signal(execution_id, "my_signal")
dead = await backend.get_dead_task(task_id)
```
