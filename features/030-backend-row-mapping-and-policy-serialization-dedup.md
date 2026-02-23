# Backend Row Mapping and Policy Serialization Dedup

## Description
Started P2.4 by extracting shared row-mapping and retry-policy serialization helpers used by both SQLite and Postgres backends, reducing duplicate conversion logic while keeping backend-specific SQL and transaction flow intact.

## Key Changes
* `stent/backend/utils.py`
  * Added shared helpers for storage-level retry policy JSON (`retry_policy_to_json`, `retry_policy_from_json`).
  * Added shared tuple builders (`execution_row_values`, `task_row_values`).
  * Added shared row mappers (`row_to_execution`, `row_to_progress`, `row_to_task`) with consistent datetime/tag handling.
* `stent/backend/sqlite.py`
  * Updated backend-local conversion methods to delegate to shared helpers.
* `stent/backend/postgres.py`
  * Updated backend-local conversion methods to delegate to shared helpers.

## Usage/Configuration
```python
# No API changes. Existing backend usage remains the same.
backend = Stent.backends.SQLiteBackend("tasks.db")
await backend.init_db()
```
