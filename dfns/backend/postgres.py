import asyncpg
import json
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Any
from dfns.backend.base import Backend
from dfns.core import ExecutionRecord, TaskRecord, ExecutionProgress, RetryPolicy

logger = logging.getLogger(__name__)

class PostgresBackend(Backend):
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: Optional[asyncpg.Pool] = None

    async def init_db(self):
        if not self.pool:
             self.pool = await asyncpg.create_pool(self.dsn)
        
        assert self.pool is not None
        
        async with self.pool.acquire() as conn:
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

    async def create_execution(self, record: ExecutionRecord) -> None:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO executions (id, root_function, state, args, kwargs, result, error, retries, created_at, started_at, completed_at, expiry_at, tags, priority, queue) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)",
                record.id, record.root_function, record.state, record.args, record.kwargs,
                record.result, record.error, record.retries, record.created_at,
                record.started_at, record.completed_at, record.expiry_at,
                json.dumps(record.tags), record.priority, record.queue
            )
            for p in record.progress:
                await conn.execute(
                    "INSERT INTO execution_progress (execution_id, step, status, started_at, completed_at, detail) VALUES ($1, $2, $3, $4, $5, $6)",
                    record.id, p.step, p.status, p.started_at, p.completed_at, p.detail
                )

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
            query = "SELECT * FROM executions"
            params: List[Any] = []
            if state:
                query += " WHERE state = $1"
                params.append(state)
            
            query += f" ORDER BY created_at DESC LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}"
            params.extend([limit, offset])
            
            rows = await conn.fetch(query, *params)
            results = []
            for row in rows:
                results.append(self._row_to_execution(row, progress=[]))
            return results

    async def create_task(self, task: TaskRecord) -> None:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tasks (id, execution_id, step_name, kind, parent_task_id, state, args, kwargs, result, error, retries, created_at, started_at, completed_at, worker_id, lease_expires_at, tags, priority, queue, idempotency_key, retry_policy, scheduled_for) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22)",
                task.id, task.execution_id, task.step_name, task.kind, task.parent_task_id,
                task.state, task.args, task.kwargs, task.result, task.error, task.retries,
                task.created_at, task.started_at, task.completed_at, task.worker_id,
                task.lease_expires_at, json.dumps(task.tags), task.priority, task.queue,
                task.idempotency_key, self._policy_to_json(task.retry_policy), task.scheduled_for
            )

    async def count_tasks(self, queue: str | None = None, state: str | None = None) -> int:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            query = "SELECT COUNT(*) FROM tasks WHERE 1=1"
            params: List[Any] = []
            if queue:
                query += f" AND queue = ${len(params) + 1}"
                params.append(queue)
            if state:
                query += f" AND state = ${len(params) + 1}"
                params.append(state)
            
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
                    worker_id=$7, lease_expires_at=$8
                WHERE id=$9
            """, 
                task.state, task.result, task.error, task.retries, task.started_at,
                task.completed_at, task.worker_id, task.lease_expires_at, task.id
            )

    async def list_tasks(self, limit: int = 10, offset: int = 0, state: str | None = None) -> List[TaskRecord]:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            query = "SELECT * FROM tasks"
            params: List[Any] = []
            if state:
                query += " WHERE state = $1"
                params.append(state)
            
            query += f" ORDER BY created_at DESC LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}"
            params.extend([limit, offset])
            
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
    ) -> TaskRecord | None:
        if now is None:
            now = datetime.now()
        if lease_duration is None:
            lease_duration = timedelta(minutes=5)
            
        expires_at = now + lease_duration
        
        # Helper for queues condition
        queue_clause = ""
        # $1=worker_id, $2=expires_at, $3=now, $4=now, $5=now
        params: List[Any] = [worker_id, expires_at, now, now, now]
        
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
            # Using sub-select for claim with RETURNING
            query = f"""
                UPDATE tasks
                SET state='running', worker_id=$1, lease_expires_at=$2, started_at=$3
                WHERE id = (
                    SELECT id FROM tasks
                    WHERE (
                        state='pending'
                        OR (state='running' AND lease_expires_at < $4)
                    )
                    AND (scheduled_for IS NULL OR scheduled_for <= $5)
                      {queue_clause}
                    ORDER BY priority DESC, created_at ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING *
            """
            row = await conn.fetchrow(query, *params)
            if row:
                return self._row_to_task(row)
        return None

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
            await conn.execute(
                "INSERT INTO dead_tasks (id, reason, moved_at, data) VALUES ($1, $2, $3, $4)",
                task.id, reason, datetime.now(), str(task)
            )

    async def cleanup_executions(self, older_than: datetime) -> int:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            where_clause = "completed_at IS NOT NULL AND completed_at < $1 AND state IN ('completed', 'failed', 'timed_out', 'cancelled')"
            
            async with conn.transaction():
                # Delete dependents using subquery
                await conn.execute(f"DELETE FROM tasks WHERE execution_id IN (SELECT id FROM executions WHERE {where_clause})", older_than)
                await conn.execute(f"DELETE FROM execution_progress WHERE execution_id IN (SELECT id FROM executions WHERE {where_clause})", older_than)
                
                # Delete executions
                tag = await conn.execute(f"DELETE FROM executions WHERE {where_clause}", older_than)
                # tag format is usually "DELETE <count>"
                try:
                    count = int(tag.split(" ")[1])
                except (IndexError, ValueError):
                    count = 0
                return count

    def _policy_to_json(self, p: RetryPolicy | None) -> str:
        if not p:
            return "{}"
        return json.dumps({
            "max_attempts": p.max_attempts,
            "backoff_factor": p.backoff_factor,
            "initial_delay": p.initial_delay,
            "max_delay": p.max_delay,
            "jitter": p.jitter
        })

    def _json_to_policy(self, s: str) -> RetryPolicy:
        d = json.loads(s)
        return RetryPolicy(
            max_attempts=d.get("max_attempts", 3),
            backoff_factor=d.get("backoff_factor", 2.0),
            initial_delay=d.get("initial_delay", 1.0),
            max_delay=d.get("max_delay", 60.0),
            jitter=d.get("jitter", 0.1)
        )

    def _row_to_execution(self, row: Any, progress: List[ExecutionProgress]) -> ExecutionRecord:
        return ExecutionRecord(
            id=row["id"],
            root_function=row["root_function"],
            state=row["state"],
            args=row["args"],
            kwargs=row["kwargs"],
            result=row["result"],
            error=row["error"],
            retries=row["retries"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            expiry_at=row["expiry_at"],
            progress=progress,
            tags=json.loads(row["tags"]),
            priority=row["priority"],
            queue=row["queue"]
        )

    def _row_to_progress(self, row: Any) -> ExecutionProgress:
        return ExecutionProgress(
            step=row["step"],
            status=row["status"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            detail=row["detail"]
        )

    def _row_to_task(self, row: Any) -> TaskRecord:
        return TaskRecord(
            id=row["id"],
            execution_id=row["execution_id"],
            step_name=row["step_name"],
            kind=row["kind"],
            parent_task_id=row["parent_task_id"],
            state=row["state"],
            args=row["args"],
            kwargs=row["kwargs"],
            result=row["result"],
            error=row["error"],
            retries=row["retries"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            worker_id=row["worker_id"],
            lease_expires_at=row["lease_expires_at"],
            tags=json.loads(row["tags"]),
            priority=row["priority"],
            queue=row["queue"],
            idempotency_key=row["idempotency_key"],
            retry_policy=self._json_to_policy(row["retry_policy"]),
            scheduled_for=row["scheduled_for"]
        )
