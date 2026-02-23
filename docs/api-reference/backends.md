# Backends API Reference

Stent supports multiple storage backends for persisting workflow state. This document covers the available backends and their configuration.

## Backend Protocol

All backends implement the `Backend` protocol:

```python
from typing import Protocol

class Backend(Protocol):
    async def init_db(self) -> None: ...
    async def close(self) -> None: ...
    
    # Execution operations
    async def create_execution(self, record: ExecutionRecord) -> None: ...
    async def create_execution_with_root_task(self, record: ExecutionRecord, task: TaskRecord) -> None: ...
    async def get_execution(self, execution_id: str) -> ExecutionRecord | None: ...
    async def update_execution(self, record: ExecutionRecord) -> None: ...
    async def list_executions(self, limit: int, offset: int, state: str | None) -> List[ExecutionRecord]: ...
    async def count_executions(self, state: str | None = None) -> int: ...
    
    # Task operations
    async def create_task(self, task: TaskRecord) -> None: ...
    async def create_tasks(self, tasks: List[TaskRecord]) -> None: ...
    async def get_task(self, task_id: str) -> TaskRecord | None: ...
    async def update_task(self, task: TaskRecord) -> None: ...
    async def list_tasks(self, limit: int, offset: int, state: str | None) -> List[TaskRecord]: ...
    async def count_tasks(self, queue: str | None = None, state: str | None = None) -> int: ...
    async def claim_next_task(...) -> TaskRecord | None: ...
    async def renew_task_lease(...) -> bool: ...
    
    # Cache/idempotency
    async def get_cached_result(self, cache_key: str) -> bytes | None: ...
    async def set_cached_result(self, cache_key: str, value: bytes, ttl: timedelta | None) -> None: ...
    async def get_idempotency_result(self, idempotency_key: str) -> bytes | None: ...
    async def set_idempotency_result(self, idempotency_key: str, value: bytes) -> None: ...
    
    # Dead letter queue
    async def move_task_to_dead_letter(self, task: TaskRecord, reason: str) -> None: ...
    async def list_dead_tasks(self, limit: int) -> List[DeadLetterRecord]: ...
    async def count_dead_tasks(self) -> int: ...
    async def get_dead_task(self, task_id: str) -> DeadLetterRecord | None: ...
    async def delete_dead_task(self, task_id: str) -> bool: ...
    
    # Cleanup
    async def cleanup_executions(self, older_than: datetime) -> int: ...
    async def cleanup_dead_letters(self, older_than: datetime) -> int: ...
    
    # Signals
    async def create_signal(self, signal: SignalRecord) -> None: ...
    async def get_signal(self, execution_id: str, name: str) -> SignalRecord | None: ...
    
    # Progress
    async def append_progress(self, execution_id: str, progress: ExecutionProgress) -> None: ...

    # Execution context (counters/custom state)
    async def ensure_execution_counters(self, execution_id: str, counters: dict[str, int | float]) -> None: ...
    async def increment_execution_counter(self, execution_id: str, name: str, amount: int | float) -> float: ...
    async def get_execution_counters(self, execution_id: str) -> dict[str, int | float]: ...
    async def ensure_execution_state_values(self, execution_id: str, values: dict[str, bytes]) -> None: ...
    async def set_execution_state_value(self, execution_id: str, key: str, value: bytes) -> None: ...
    async def get_execution_state_values(self, execution_id: str) -> dict[str, bytes]: ...
```

---

## SQLite Backend

The SQLite backend is perfect for development, testing, and single-node deployments.

### Creation

```python
from stent import Stent

backend = Stent.backends.SQLiteBackend("path/to/database.sqlite")
await backend.init_db()
```

### Features

- **WAL Mode**: Uses Write-Ahead Logging for better concurrency
- **Async Support**: Built on `aiosqlite` for non-blocking operations
- **Single File**: All data stored in one portable file
- **No Setup**: No server required

### Configuration

The SQLite backend accepts a file path:

```python
# Relative path
backend = Stent.backends.SQLiteBackend("workflow.db")

# Absolute path
backend = Stent.backends.SQLiteBackend("/var/lib/stent/workflow.db")

# In-memory (for testing)
backend = Stent.backends.SQLiteBackend(":memory:")
```

### Database Schema

The SQLite backend creates the following tables:

| Table | Purpose |
|-------|---------|
| `executions` | Workflow execution records |
| `tasks` | Task records for orchestrators and activities |
| `signals` | External signal storage |
| `dead_letters` | Failed task records |
| `cache` | Cached computation results |
| `idempotency` | Idempotency keys and results |
| `execution_counters` | Execution-scoped counters |
| `execution_state` | Execution-scoped custom key-value state |

### Best Practices

1. **Use WAL mode** (enabled by default):
   ```python
   # WAL mode allows concurrent reads during writes
   # Stent enables this automatically
   ```

2. **Regular backups**:
   ```bash
   # Simple file copy when database is not actively written
   cp workflow.db workflow.db.backup
   
   # Or use SQLite's backup API
   sqlite3 workflow.db ".backup backup.db"
   ```

3. **Disk space monitoring**: SQLite databases grow over time. Use `cleanup_interval` in workers to remove old data.

### Limitations

- **Single machine only**: Cannot be accessed by multiple machines
- **Write contention**: Heavy write loads may cause lock contention
- **Not recommended** for high-throughput production workloads

---

## PostgreSQL Backend

The PostgreSQL backend is recommended for production deployments.

### Creation

```python
from stent import Stent

backend = Stent.backends.PostgresBackend(
    "postgresql://user:password@localhost:5432/stent"
)
await backend.init_db()
```

### Features

- **Connection Pooling**: Built on `asyncpg` with connection pooling
- **ACID Transactions**: Full transaction support for consistency
- **Concurrent Access**: Multiple workers can access simultaneously
- **Production Ready**: Supports high-throughput workloads

### Connection String Format

```
postgresql://[user[:password]@][host][:port][/database][?param1=value1&...]
```

Examples:

```python
# Basic connection
backend = Stent.backends.PostgresBackend(
    "postgresql://stent:password@localhost/stent"
)

# With SSL
backend = Stent.backends.PostgresBackend(
    "postgresql://stent:password@db.example.com/stent?sslmode=require"
)

# With connection pool settings
backend = Stent.backends.PostgresBackend(
    "postgresql://stent:password@localhost/stent"
    "?min_size=5&max_size=20"
)
```

### Database Setup

Before using the PostgreSQL backend, create the database:

```sql
-- Create database
CREATE DATABASE stent;

-- Create user (optional)
CREATE USER stent WITH PASSWORD 'your-secure-password';
GRANT ALL PRIVILEGES ON DATABASE stent TO stent;
```

Then initialize the schema:

```python
backend = Stent.backends.PostgresBackend(dsn)
await backend.init_db()  # Creates tables if they don't exist
```

### Connection Pool Configuration

The PostgreSQL backend uses `asyncpg` connection pooling. Configure via DSN parameters:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `min_size` | 10 | Minimum pool connections |
| `max_size` | 10 | Maximum pool connections |
| `max_queries` | 50000 | Queries before connection reset |
| `max_inactive_connection_lifetime` | 300 | Seconds before idle connection close |

```python
backend = Stent.backends.PostgresBackend(
    "postgresql://user:pass@host/db"
    "?min_size=5&max_size=50&max_inactive_connection_lifetime=600"
)
```

### Best Practices

1. **Use connection pooling**:
   ```python
   # Size pool based on worker concurrency
   # Rule of thumb: max_size >= max_concurrency per worker * num_workers
   ```

2. **Enable SSL in production**:
   ```python
   backend = Stent.backends.PostgresBackend(
       "postgresql://user:pass@host/db?sslmode=require"
   )
   ```

3. **Regular maintenance**:
   ```sql
   -- Run periodically for performance
   VACUUM ANALYZE executions;
   VACUUM ANALYZE tasks;
   ```

4. **Index optimization**: The backend creates necessary indexes, but monitor query performance.

5. **Backup strategy**:
   ```bash
   # Regular backups with pg_dump
   pg_dump -h localhost -U stent stent > backup.sql
   
   # Or use continuous archiving for point-in-time recovery
   ```

---

## Custom Backend

You can implement a custom backend by following the `Backend` protocol:

```python
from stent.backend.base import Backend
from stent.core import ExecutionRecord, TaskRecord, SignalRecord, DeadLetterRecord, ExecutionProgress
from datetime import datetime, timedelta
from typing import List

class MyCustomBackend:
    async def init_db(self) -> None:
        # Initialize your storage
        ...
    
    async def close(self) -> None:
        # Clean up connections
        ...
    
    async def create_execution(self, record: ExecutionRecord) -> None:
        # Store execution record
        ...
    
    async def get_execution(self, execution_id: str) -> ExecutionRecord | None:
        # Retrieve execution by ID
        ...
    
    async def claim_next_task(
        self,
        *,
        worker_id: str,
        queues: List[str] | None = None,
        tags: List[str] | None = None,
        now: datetime | None = None,
        lease_duration: timedelta | None = None,
        concurrency_limits: dict[str, int] | None = None,
    ) -> TaskRecord | None:
        # Atomically claim the next available task
        # MUST be atomic to prevent double-claiming
        ...
    
    # ... implement all other protocol methods
```

### Key Requirements

1. **Atomic task claiming**: `claim_next_task` must be atomic to prevent multiple workers from claiming the same task.

2. **Transactional consistency**: Operations that modify multiple records should be transactional.

3. **Efficient queries**: Task claiming is called frequently; optimize for performance.

4. **Proper locking**: Respect lease durations and worker IDs.

### Using Custom Backend

```python
from stent import Stent

custom_backend = MyCustomBackend(config)
await custom_backend.init_db()

executor = Stent(backend=custom_backend)
```

---

## Backend Comparison

| Feature | SQLite | PostgreSQL |
|---------|--------|------------|
| Setup | None | Requires server |
| Scalability | Single node | Multi-node |
| Concurrency | Limited | High |
| Persistence | File-based | Server-based |
| Backup | File copy | pg_dump/streaming |
| Best for | Dev/test | Production |
| Dependencies | aiosqlite | asyncpg |

### Choosing a Backend

**Use SQLite when:**
- Developing locally
- Running tests
- Single-worker deployment
- Embedded workflows
- Simple use cases

**Use PostgreSQL when:**
- Running in production
- Multiple workers needed
- High throughput required
- Need reliability guarantees
- Distributed deployment
