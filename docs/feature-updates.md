# Feature Updates (019-034)

This document tracks major updates that landed after the initial documentation set, based on entries in `features/019-...` through `features/034-...`.

## Reliability and Correctness

### 019 - P0 Correctness Fixes
- Retry delays now use `scheduled_for`, preventing early retry pickup.
- Falsey idempotent values (for example `0`, `False`, `""`, `[]`, `{}`) are treated as valid cache hits.
- Wait paths now raise `ExpiryError` consistently on timeout.
- Cancelled executions are treated as terminal in `result_of()`/`wait_for()`.

### 023 - Duration Parser Validation Hardening
- Duration parsing is now strict and requires the entire input string to be valid.
- Valid examples: `10s`, `1h30m`, `2d8h`.
- Invalid examples: `10sfoo`, `foo10s`, `1h 30m`.

### 024 - UTC Utility Consistency
- `now_utc()` now returns timezone-aware UTC datetimes.

## Worker Routing and CLI Operations

### 020 - Worker Tag Filtering
- Worker `serve(tags=[...])` now enforces tag eligibility at claim time.
- Tagged workers still claim untagged tasks.

### 021 - CLI Backend Lifecycle
- CLI now always initializes the backend before command execution and closes it in all exit paths.

### 022 - Count-Based CLI Stats
- `stent stats` and `stent watch` now use backend count queries instead of scan-heavy list operations.

### 034 - CLI Execution Context Display
- `stent show <execution-id>` now prints execution `Counters` and `Custom State` sections when present.

## Execution Context and Runtime APIs

### 033 - Execution Context Counters and Custom State
- Added `Stent.context(...)` to access execution-scoped counters and custom key-value state.
- `state_of()` now returns `counters` and `custom_state`.

```python
@Stent.durable()
async def flow():
    ctx = Stent.context(counters={"progress": 0}, state={"phase": "start"})
    ctx.counters("progress").add(1)
    ctx.state("phase").set("running")
```

## Internal Refactors and Maintenance (No Public API Break)

### 025 - Typecheck Hygiene and Dev Dependency Scope
- Type-checking cleanup across runtime code.
- `pyrefly` moved to dev dependencies only.

### 026-029 - Executor Module Extraction
- Wait/poll, signal handling, worker task handling, and dispatch/map logic were extracted from `executor.py` into focused helper modules.
- Public APIs and behavior were preserved.

### 030-032 - Backend Helper Deduplication
- Shared backend row mappers and query builders were extracted to reduce duplication across SQLite/Postgres.
- Includes shared list/count query building and consistent row conversion for executions, tasks, signals, and dead letters.

## Related References

- API: `docs/api-reference/stent.md`
- Backends: `docs/api-reference/backends.md`
- Monitoring/CLI guide: `docs/guides/monitoring.md`
- Worker routing: `docs/guides/workers.md`
