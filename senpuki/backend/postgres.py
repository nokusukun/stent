import asyncpg
import asyncpg.pool
import json
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Any, Union
from senpuki.backend.base import Backend
from senpuki.core import ExecutionRecord, TaskRecord, ExecutionProgress, RetryPolicy, SignalRecord, DeadLetterRecord
from senpuki.backend.utils import (
    build_filtered_count_query,
    build_filtered_list_query,
    dollar_placeholder,
    execution_row_values,
    row_to_dead_letter,
    row_to_execution,
    row_to_progress,
    row_to_signal,
    row_to_task,
    retry_policy_from_json,
    retry_policy_to_json,
    task_record_to_json,
    task_row_values,
)

logger = logging.getLogger(__name__)

# Type alias for connection objects (both direct connections and pool proxies)
# Using Any because PoolConnectionProxy and Connection both have execute() but type checker doesn't know
_Conn = Any

class PostgresBackend(Backend):
    def __init__(self, dsn: str, min_pool_size: int = 2, max_pool_size: int = 10):
        """
        Initialize Postgres backend.
        
        Args:
            dsn: PostgreSQL connection string
            min_pool_size: Minimum connections in pool (default 2)
            max_pool_size: Maximum connections in pool (default 10)
        """
        self.dsn = dsn
        self._min_pool_size = min_pool_size
        self._max_pool_size = max_pool_size
        self.pool: Optional[asyncpg.Pool] = None
        self._closed = False

    async def close(self) -> None:
        """Close the connection pool and release resources."""
        self._closed = True
        if self.pool is not None:
            await self.pool.close()
            self.pool = None
            logger.info("Postgres connection pool closed")

    async def init_db(self):
        if self._closed:
            raise RuntimeError("PostgresBackend has been closed")
        if not self.pool:
            self.pool = await asyncpg.create_pool(
                self.dsn,
                min_size=self._min_pool_size,
                max_size=self._max_pool_size,
            )
        
        assert self.pool is not None
        
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    execution_id TEXT,
                    name TEXT,
                    payload BYTEA,
                    created_at TIMESTAMP,
                    consumed BOOLEAN,
                    consumed_at TIMESTAMP,
                    PRIMARY KEY (execution_id, name)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS executions (
                    id TEXT PRIMARY KEY,
                    root_function TEXT,
                    state TEXT,
                    args BYTEA,
                    kwargs BYTEA,
                    result BYTEA,
                    error BYTEA,
                    retries INTEGER,
                    created_at TIMESTAMP,
                    started_at TIMESTAMP,
                    completed_at TIMESTAMP,
                    expiry_at TIMESTAMP,
                    tags TEXT,
                    priority INTEGER,
                    queue TEXT
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS execution_progress (
                    execution_id TEXT,
                    step TEXT,
                    status TEXT,
                    started_at TIMESTAMP,
                    completed_at TIMESTAMP,
                    detail TEXT,
                    ordinal SERIAL PRIMARY KEY
                )
            """)
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_progress_exec ON execution_progress(execution_id)")
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    execution_id TEXT,
                    step_name TEXT,
                    kind TEXT,
                    parent_task_id TEXT,
                    state TEXT,
                    args BYTEA,
                    kwargs BYTEA,
                    result BYTEA,
                    error BYTEA,
                    retries INTEGER,
                    created_at TIMESTAMP,
                    started_at TIMESTAMP,
                    completed_at TIMESTAMP,
                    worker_id TEXT,
                    lease_expires_at TIMESTAMP,
                    tags TEXT,
                    priority INTEGER,
                    queue TEXT,
                    idempotency_key TEXT,
                    retry_policy TEXT,
                    scheduled_for TIMESTAMP
                )
            """)
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_state_queue_scheduled ON tasks(state, queue, scheduled_for)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_priority_created ON tasks(priority, created_at)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_execution ON tasks(execution_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_step_lease ON tasks(step_name, state, lease_expires_at)")
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS dead_tasks (
                    id TEXT PRIMARY KEY,
                    reason TEXT,
                    moved_at TIMESTAMP,
                    data TEXT -- full JSON dump of task
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY,
                    value BYTEA,
                    expires_at TIMESTAMP
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS idempotency (
                    key TEXT PRIMARY KEY,
                    value BYTEA
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS execution_counters (
                    execution_id TEXT,
                    name TEXT,
                    value DOUBLE PRECISION NOT NULL,
                    PRIMARY KEY (execution_id, name)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS execution_state (
                    execution_id TEXT,
                    key TEXT,
                    value BYTEA,
                    PRIMARY KEY (execution_id, key)
                )
            """)

    def _execution_row_values(self, record: ExecutionRecord) -> tuple[Any, ...]:
        return execution_row_values(record)

    async def _insert_execution(self, conn: _Conn, record: ExecutionRecord) -> None:
        await conn.execute(
            "INSERT INTO executions (id, root_function, state, args, kwargs, result, error, retries, created_at, started_at, completed_at, expiry_at, tags, priority, queue) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)",
            *self._execution_row_values(record),
        )
        for p in record.progress:
            await conn.execute(
                "INSERT INTO execution_progress (execution_id, step, status, started_at, completed_at, detail) VALUES ($1, $2, $3, $4, $5, $6)",
                record.id,
                p.step,
                p.status,
                p.started_at,
                p.completed_at,
                p.detail,
            )

    def _task_row_values(self, task: TaskRecord) -> tuple[Any, ...]:
        return task_row_values(task)

    async def _insert_task(self, conn: _Conn, task: TaskRecord) -> None:
        await conn.execute(
            "INSERT INTO tasks (id, execution_id, step_name, kind, parent_task_id, state, args, kwargs, result, error, retries, created_at, started_at, completed_at, worker_id, lease_expires_at, tags, priority, queue, idempotency_key, retry_policy, scheduled_for) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22)",
            *self._task_row_values(task),
        )

    async def create_execution(self, record: ExecutionRecord) -> None:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            await self._insert_execution(conn, record)

    async def create_execution_with_root_task(self, record: ExecutionRecord, task: TaskRecord) -> None:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await self._insert_execution(conn, record)
                await self._insert_task(conn, task)

    async def get_execution(self, execution_id: str) -> ExecutionRecord | None:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM executions WHERE id = $1", execution_id)
            if not row:
                return None
            
            # Fetch progress
            progress = []
            p_rows = await conn.fetch("SELECT * FROM execution_progress WHERE execution_id = $1 ORDER BY ordinal", execution_id)
            for pr in p_rows:
                 progress.append(self._row_to_progress(pr))

            return self._row_to_execution(row, progress)

    async def update_execution(self, record: ExecutionRecord) -> None:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE executions SET
                    state=$1, args=$2, kwargs=$3, result=$4, error=$5, retries=$6,
                    started_at=$7, completed_at=$8, expiry_at=$9, tags=$10,
                    priority=$11, queue=$12
                WHERE id=$13
            """, 
                record.state, record.args, record.kwargs, record.result, record.error,
                record.retries, record.started_at, record.completed_at, record.expiry_at,
                json.dumps(record.tags), record.priority, record.queue, record.id
            )

    async def list_executions(self, limit: int = 10, offset: int = 0, state: str | None = None) -> List[ExecutionRecord]:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            query, params = build_filtered_list_query(
                table="executions",
                filters=[("state", state)],
                order_by="created_at DESC",
                limit=limit,
                offset=offset,
                placeholder=dollar_placeholder,
            )
            
            rows = await conn.fetch(query, *params)
            results = []
            for row in rows:
                results.append(self._row_to_execution(row, progress=[]))
            return results

    async def count_executions(self, state: str | None = None) -> int:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            query, params = build_filtered_count_query(
                table="executions",
                filters=[("state", state)],
                placeholder=dollar_placeholder,
            )

            val = await conn.fetchval(query, *params)
            return val if val else 0

    async def create_task(self, task: TaskRecord) -> None:
        await self.create_tasks([task])

    async def create_tasks(self, tasks: List[TaskRecord]) -> None:
        if not tasks:
            return
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            await conn.executemany(
                "INSERT INTO tasks (id, execution_id, step_name, kind, parent_task_id, state, args, kwargs, result, error, retries, created_at, started_at, completed_at, worker_id, lease_expires_at, tags, priority, queue, idempotency_key, retry_policy, scheduled_for) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22)",
                [self._task_row_values(task) for task in tasks],
            )

    async def count_tasks(self, queue: str | None = None, state: str | None = None) -> int:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            query, params = build_filtered_count_query(
                table="tasks",
                filters=[("queue", queue), ("state", state)],
                placeholder=dollar_placeholder,
            )
            
            val = await conn.fetchval(query, *params)
            return val if val else 0

    async def get_task(self, task_id: str) -> TaskRecord | None:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM tasks WHERE id = $1", task_id)
            if not row:
                return None
            return self._row_to_task(row)

    async def update_task(self, task: TaskRecord) -> None:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE tasks SET
                    state=$1, result=$2, error=$3, retries=$4, started_at=$5, completed_at=$6,
                    worker_id=$7, lease_expires_at=$8, scheduled_for=$9
                WHERE id=$10
            """, 
                task.state, task.result, task.error, task.retries, task.started_at,
                task.completed_at, task.worker_id, task.lease_expires_at, task.scheduled_for, task.id
            )

    async def list_tasks(self, limit: int = 10, offset: int = 0, state: str | None = None) -> List[TaskRecord]:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            query, params = build_filtered_list_query(
                table="tasks",
                filters=[("state", state)],
                order_by="created_at DESC",
                limit=limit,
                offset=offset,
                placeholder=dollar_placeholder,
            )
            
            rows = await conn.fetch(query, *params)
            return [self._row_to_task(row) for row in rows]

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
        if now is None:
            now = datetime.now()
        if lease_duration is None:
            lease_duration = timedelta(minutes=5)
            
        expires_at = now + lease_duration
        worker_tags_set = set(tags or [])
        should_filter_by_tags = bool(tags)
        
        # Helper for queues condition
        queue_clause = ""
        # $1=now, $2=now
        params: List[Any] = [now, now]
        
        if queues:
            placeholders: List[str] = []
            for q in queues:
                params.append(q)
                placeholders.append(f"${len(params)}")
            
            queue_clause = f"AND (queue IN ({', '.join(placeholders)}) OR queue IS NULL)"
        else:
            queue_clause = "AND 1=1"

        assert self.pool is not None
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # 1. Fetch candidates
                query = f"""
                    SELECT * FROM tasks
                    WHERE (
                        state='pending'
                        OR (state='running' AND lease_expires_at < $1)
                    )
                    AND (scheduled_for IS NULL OR scheduled_for <= $2)
                    AND kind != 'signal'
                    {queue_clause}
                    ORDER BY priority DESC, created_at ASC
                    LIMIT 50
                    FOR UPDATE SKIP LOCKED
                """
                rows = await conn.fetch(query, *params)
                
                if not rows:
                    return None
                
                for row in rows:
                    if should_filter_by_tags:
                        task_tags = json.loads(row["tags"]) if row["tags"] else []
                        if task_tags and worker_tags_set.isdisjoint(task_tags):
                            continue

                    step_name = row["step_name"]
                    limit = concurrency_limits.get(step_name) if concurrency_limits else None
                    
                    if limit is not None:
                         # Check count of currently running tasks for this step
                         # We exclude lease-expired ones from "active" count implicitly by checking lease_expires_at > now
                         count_val = await conn.fetchval("""
                            SELECT COUNT(*) FROM tasks 
                            WHERE step_name = $1 
                            AND state = 'running' 
                            AND lease_expires_at > $2
                         """, step_name, now)
                         
                         if (count_val or 0) >= limit:
                             continue

                    # Claim it
                    # We already locked it, so update is safe
                    updated_row = await conn.fetchrow("""
                        UPDATE tasks
                        SET state='running', worker_id=$1, lease_expires_at=$2, started_at=$3
                        WHERE id=$4
                        RETURNING *
                    """, worker_id, expires_at, now, row["id"])
                    
                    if updated_row:
                        return self._row_to_task(updated_row)
        return None

    async def renew_task_lease(
        self,
        task_id: str,
        worker_id: str,
        lease_duration: timedelta,
    ) -> bool:
        assert self.pool is not None
        expires_at = datetime.now() + lease_duration
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE tasks
                SET lease_expires_at=$1
                WHERE id=$2 AND worker_id=$3 AND state='running'
                RETURNING id
                """,
                expires_at,
                task_id,
                worker_id,
            )
            return row is not None

    async def list_tasks_for_execution(self, execution_id: str) -> List[TaskRecord]:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM tasks WHERE execution_id = $1", execution_id)
            return [self._row_to_task(row) for row in rows]

    async def append_progress(self, execution_id: str, progress: ExecutionProgress) -> None:
         assert self.pool is not None
         async with self.pool.acquire() as conn:
             await conn.execute(
                "INSERT INTO execution_progress (execution_id, step, status, started_at, completed_at, detail) VALUES ($1, $2, $3, $4, $5, $6)",
                execution_id, progress.step, progress.status, progress.started_at, progress.completed_at, progress.detail
            )

    async def get_cached_result(self, cache_key: str) -> bytes | None:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT value, expires_at FROM cache WHERE key = $1", cache_key)
            if row:
                val, expires_at = row['value'], row['expires_at']
                # logger.debug(f"Fetched from cache: key={cache_key}, expires_at={expires_at}, value_len={len(val) if val else 0}")
                if expires_at and expires_at < datetime.now():
                    # logger.debug(f"Cache expired for key={cache_key}")
                    return None
                return val
            # logger.debug(f"Cache miss for key={cache_key}")
        return None

    async def set_cached_result(self, cache_key: str, value: bytes, ttl: timedelta | None = None) -> None:
        expires_at = None
        if ttl:
            expires_at = datetime.now() + ttl
        
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO cache (key, value, expires_at) VALUES ($1, $2, $3)
                ON CONFLICT (key) DO UPDATE SET value=$2, expires_at=$3
            """, cache_key, value, expires_at)

    async def get_idempotency_result(self, idempotency_key: str) -> bytes | None:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT value FROM idempotency WHERE key = $1", idempotency_key)
            return row['value'] if row else None

    async def set_idempotency_result(self, idempotency_key: str, value: bytes) -> None:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
             await conn.execute("""
                INSERT INTO idempotency (key, value) VALUES ($1, $2)
                ON CONFLICT (key) DO UPDATE SET value=$2
            """, idempotency_key, value)

    async def move_task_to_dead_letter(self, task: TaskRecord, reason: str) -> None:
         assert self.pool is not None
         async with self.pool.acquire() as conn:
            payload = task_record_to_json(task)
            await conn.execute(
                "INSERT INTO dead_tasks (id, reason, moved_at, data) VALUES ($1, $2, $3, $4)",
                task.id, reason, datetime.now(), payload
            )

    def _row_to_dead_letter(self, row: Any) -> DeadLetterRecord:
        return row_to_dead_letter(row)

    async def list_dead_tasks(self, limit: int = 50) -> List[DeadLetterRecord]:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM dead_tasks ORDER BY moved_at DESC LIMIT $1",
                limit,
            )
            return [self._row_to_dead_letter(row) for row in rows]

    async def count_dead_tasks(self) -> int:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            val = await conn.fetchval("SELECT COUNT(*) FROM dead_tasks")
            return val if val else 0

    async def get_dead_task(self, task_id: str) -> DeadLetterRecord | None:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM dead_tasks WHERE id = $1",
                task_id,
            )
            if not row:
                return None
            return self._row_to_dead_letter(row)

    async def delete_dead_task(self, task_id: str) -> bool:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            tag = await conn.execute("DELETE FROM dead_tasks WHERE id = $1", task_id)
            try:
                count = int(tag.split(" ")[1])
            except (IndexError, ValueError):
                count = 0
            return count > 0

    async def cleanup_executions(self, older_than: datetime) -> int:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            where_clause = "completed_at IS NOT NULL AND completed_at < $1 AND state IN ('completed', 'failed', 'timed_out', 'cancelled')"
            
            async with conn.transaction():
                # Delete dependents using subquery
                await conn.execute(f"DELETE FROM tasks WHERE execution_id IN (SELECT id FROM executions WHERE {where_clause})", older_than)
                await conn.execute(f"DELETE FROM execution_progress WHERE execution_id IN (SELECT id FROM executions WHERE {where_clause})", older_than)
                await conn.execute(f"DELETE FROM signals WHERE execution_id IN (SELECT id FROM executions WHERE {where_clause})", older_than)
                await conn.execute(f"DELETE FROM execution_counters WHERE execution_id IN (SELECT id FROM executions WHERE {where_clause})", older_than)
                await conn.execute(f"DELETE FROM execution_state WHERE execution_id IN (SELECT id FROM executions WHERE {where_clause})", older_than)
                
                # Delete executions
                tag = await conn.execute(f"DELETE FROM executions WHERE {where_clause}", older_than)
                # tag format is usually "DELETE <count>"
                try:
                    count = int(tag.split(" ")[1])
                except (IndexError, ValueError):
                    count = 0
                return count

    async def cleanup_dead_letters(self, older_than: datetime) -> int:
        """Remove dead letter records older than the specified datetime."""
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            tag = await conn.execute(
                "DELETE FROM dead_tasks WHERE moved_at < $1",
                older_than,
            )
            try:
                count = int(tag.split(" ")[1])
            except (IndexError, ValueError):
                count = 0
            return count

    async def create_signal(self, signal: SignalRecord) -> None:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO signals (execution_id, name, payload, created_at, consumed, consumed_at) VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (execution_id, name) DO UPDATE SET payload=$3, consumed=$5, consumed_at=$6
            """, signal.execution_id, signal.name, signal.payload, signal.created_at, signal.consumed, signal.consumed_at)

    async def get_signal(self, execution_id: str, name: str) -> SignalRecord | None:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM signals WHERE execution_id = $1 AND name = $2", execution_id, name)
            if not row:
                return None
            return row_to_signal(row)

    async def ensure_execution_counters(self, execution_id: str, counters: dict[str, int | float]) -> None:
        if not counters:
            return
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            rows = [(execution_id, name, float(value)) for name, value in counters.items()]
            await conn.executemany(
                """
                INSERT INTO execution_counters (execution_id, name, value)
                VALUES ($1, $2, $3)
                ON CONFLICT (execution_id, name) DO NOTHING
                """,
                rows,
            )

    async def increment_execution_counter(self, execution_id: str, name: str, amount: int | float) -> float:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            value = await conn.fetchval(
                """
                INSERT INTO execution_counters (execution_id, name, value)
                VALUES ($1, $2, $3)
                ON CONFLICT (execution_id, name) DO UPDATE SET value = execution_counters.value + EXCLUDED.value
                RETURNING value
                """,
                execution_id,
                name,
                float(amount),
            )
            return float(value or 0.0)

    async def get_execution_counters(self, execution_id: str) -> dict[str, int | float]:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT name, value FROM execution_counters WHERE execution_id = $1",
                execution_id,
            )
            return {row["name"]: row["value"] for row in rows}

    async def ensure_execution_state_values(self, execution_id: str, values: dict[str, bytes]) -> None:
        if not values:
            return
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            rows = [(execution_id, key, value) for key, value in values.items()]
            await conn.executemany(
                """
                INSERT INTO execution_state (execution_id, key, value)
                VALUES ($1, $2, $3)
                ON CONFLICT (execution_id, key) DO NOTHING
                """,
                rows,
            )

    async def set_execution_state_value(self, execution_id: str, key: str, value: bytes) -> None:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO execution_state (execution_id, key, value)
                VALUES ($1, $2, $3)
                ON CONFLICT (execution_id, key) DO UPDATE SET value = EXCLUDED.value
                """,
                execution_id,
                key,
                value,
            )

    async def get_execution_state_values(self, execution_id: str) -> dict[str, bytes]:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key, value FROM execution_state WHERE execution_id = $1",
                execution_id,
            )
            return {row["key"]: row["value"] for row in rows}


    def _policy_to_json(self, p: RetryPolicy | None) -> str:
        return retry_policy_to_json(p)

    def _json_to_policy(self, s: str) -> RetryPolicy:
        return retry_policy_from_json(s)

    def _row_to_execution(self, row: Any, progress: List[ExecutionProgress]) -> ExecutionRecord:
        return row_to_execution(row, progress)

    def _row_to_progress(self, row: Any) -> ExecutionProgress:
        return row_to_progress(row)

    def _row_to_task(self, row: Any) -> TaskRecord:
        return row_to_task(row)
