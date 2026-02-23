# v2 Hardening - SQLite Connection Pooling and Type Safety

## Description

This feature completes Phase 1 and Phase 2 of the v2 hardening plan, implementing persistent connection pooling for the SQLite backend, improving Postgres backend lifecycle management, and resolving all type errors for clean static analysis.

## Key Changes

### SQLite Backend (`stent/backend/sqlite.py`)
* Replaced connection-per-operation with persistent connection using `asyncio.Lock`
* Enabled WAL mode and `busy_timeout` for better concurrent read performance
* Added `isolation_level=None` for manual transaction control with explicit BEGIN/COMMIT/ROLLBACK
* Added automatic stale transaction recovery (rollback on connection reuse)
* Added `close()` method for proper resource cleanup
* Added `cleanup_dead_letters()` method for dead letter queue maintenance

### Postgres Backend (`stent/backend/postgres.py`)
* Added `close()` method to properly close the connection pool
* Added `min_pool_size` and `max_pool_size` constructor parameters for pool configuration
* Added `cleanup_dead_letters()` method
* Added `_closed` flag to prevent use after close
* Added `_Conn` type alias to fix PoolConnectionProxy vs Connection type mismatch

### Backend Protocol (`stent/backend/base.py`)
* Added `close()` method to Backend protocol
* Added `cleanup_dead_letters()` method to Backend protocol

### Executor (`stent/executor.py`)
* Fixed all type errors related to `bytes | None` passed to `serializer.loads()`
* Added null checks for result deserialization

### Telemetry (`stent/telemetry.py`)
* Rewrote optional OpenTelemetry import handling for proper type safety
* Used `cast()` to properly type module references after dynamic import
* Added `type:ignore` comments for optional dependency imports

### Test Utilities (`tests/utils.py`, `tests/test_signals.py`)
* Added proper `backend.close()` calls in test teardown
* Fixed Windows file locking issues in signal tests

## Usage/Configuration

### SQLite Backend
```python
from stent import Stent

# Create backend with persistent connection
backend = Stent.backends.SQLiteBackend("mydb.sqlite")
await backend.init_db()

# Use the backend
executor = Stent(backend=backend)

# Clean up when done
await backend.close()
```

### Postgres Backend with Pool Configuration
```python
from stent import Stent

# Configure pool size
backend = Stent.backends.PostgresBackend(
    dsn="postgres://user:pass@localhost/db",
    min_pool_size=2,  # default: 2
    max_pool_size=10  # default: 10
)
await backend.init_db()

# Clean up when done
await backend.close()
```

### Dead Letter Cleanup
```python
from datetime import datetime, timedelta

# Clean up dead letters older than 30 days
cutoff = datetime.now() - timedelta(days=30)
deleted_count = await backend.cleanup_dead_letters(cutoff)
```

## Type Checking

The codebase now passes pyright with zero errors:

```bash
uv run pyright stent/
# 0 errors, 0 warnings, 0 informations
```
