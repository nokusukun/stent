import aiosqlite
import asyncio
import sqlite3
import json
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Any
from senpuki.backend.base import Backend
from senpuki.core import ExecutionRecord, TaskRecord, ExecutionProgress, RetryPolicy, SignalRecord, DeadLetterRecord
from senpuki.backend.utils import task_record_to_json, task_record_from_json

logger = logging.getLogger(__name__)

def _adapt_datetime(dt: datetime) -> str:
    return dt.isoformat()

def _convert_datetime(val: bytes) -> datetime:
    return datetime.fromisoformat(val.decode("utf-8"))

sqlite3.register_adapter(datetime, _adapt_datetime)
sqlite3.register_converter("datetime", _convert_datetime)
sqlite3.register_converter("TIMESTAMP", _convert_datetime)


class SQLiteBackend(Backend):
    """
    SQLite backend with persistent connection pooling.
    
    Uses a single persistent connection with serialized access via asyncio.Lock
    to respect SQLite's single-writer model while avoiding the overhead of
    opening a new connection for every operation.
    """
    
    def __init__(self, db_path: str, pool_size: int = 1):
        """
        Initialize SQLite backend.
        
        Args:
            db_path: Path to SQLite database file
            pool_size: Number of connections (default 1, SQLite works best with single writer)
        """
        self.db_path = db_path
        self._pool_size = pool_size
        self._connection: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()
        self._closed = False

    async def _get_connection(self) -> aiosqlite.Connection:
        """Get or create the persistent connection."""
        if self._closed:
            raise RuntimeError("SQLiteBackend has been closed")
        if self._connection is None:
            self._connection = await aiosqlite.connect(
                self.db_path,
                detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
                isolation_level=None,  # Enable manual transaction control
            )
            self._connection.row_factory = aiosqlite.Row
            # Enable WAL mode for better concurrent read performance
            await self._connection.execute("PRAGMA journal_mode=WAL")
            await self._connection.execute("PRAGMA busy_timeout=5000")
        else:
            # Ensure no stale transaction is left open from a cancelled operation
            if self._connection._conn.in_transaction:
                await self._connection.execute("ROLLBACK")
        return self._connection

    async def close(self) -> None:
        """Close the persistent connection and release resources."""
        async with self._lock:
            self._closed = True
            if self._connection is not None:
                await self._connection.close()
                self._connection = None
                logger.info("SQLite connection closed")

    async def init_db(self) -> None:
        async with self._lock:
            db = await self._get_connection()
            await db.execute("BEGIN")
            try:
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS signals (
                        execution_id TEXT,
                        name TEXT,
                        payload BLOB,
                        created_at TIMESTAMP,
                        consumed BOOLEAN,
                        consumed_at TIMESTAMP,
                        PRIMARY KEY (execution_id, name)
                    )
                """)
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS executions (
                        id TEXT PRIMARY KEY,
                        root_function TEXT,
                        state TEXT,
                        args BLOB,
                        kwargs BLOB,
                        result BLOB,
                        error BLOB,
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
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS execution_progress (
                        execution_id TEXT,
                        step TEXT,
                        status TEXT,
                        started_at TIMESTAMP,
                        completed_at TIMESTAMP,
                        detail TEXT,
                        ordinal INTEGER PRIMARY KEY AUTOINCREMENT
                    )
                """)
                await db.execute("CREATE INDEX IF NOT EXISTS idx_progress_exec ON execution_progress(execution_id)")
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS tasks (
                        id TEXT PRIMARY KEY,
                        execution_id TEXT,
                        step_name TEXT,
                        kind TEXT,
                        parent_task_id TEXT,
                        state TEXT,
                        args BLOB,
                        kwargs BLOB,
                        result BLOB,
                        error BLOB,
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
                await db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_state_queue_scheduled ON tasks(state, queue, scheduled_for)")
                await db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_priority_created ON tasks(priority, created_at)")
                await db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_execution ON tasks(execution_id)")
                await db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_step_lease ON tasks(step_name, state, lease_expires_at)")
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS dead_tasks (
                        id TEXT PRIMARY KEY,
                        reason TEXT,
                        moved_at TIMESTAMP,
                        data TEXT -- full JSON dump of task
                    )
                """)
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS cache (
                        key TEXT PRIMARY KEY,
                        value BLOB,
                        expires_at TIMESTAMP
                    )
                """)
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS idempotency (
                        key TEXT PRIMARY KEY,
                        value BLOB
                    )
                """)
                await db.execute("COMMIT")
            except Exception:
                await db.execute("ROLLBACK")
                raise

    def _execution_row_values(self, record: ExecutionRecord) -> tuple[Any, ...]:
        return (
            record.id,
            record.root_function,
            record.state,
            record.args,
            record.kwargs,
            record.result,
            record.error,
            record.retries,
            record.created_at,
            record.started_at,
            record.completed_at,
            record.expiry_at,
            json.dumps(record.tags),
            record.priority,
            record.queue,
        )

    async def _insert_execution(self, db: aiosqlite.Connection, record: ExecutionRecord) -> None:
        await db.execute(
            "INSERT INTO executions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            self._execution_row_values(record),
        )
        for p in record.progress:
            await db.execute(
                "INSERT INTO execution_progress (execution_id, step, status, started_at, completed_at, detail) VALUES (?, ?, ?, ?, ?, ?)",
                (record.id, p.step, p.status, p.started_at, p.completed_at, p.detail),
            )

    def _task_row_values(self, task: TaskRecord) -> tuple[Any, ...]:
        return (
            task.id,
            task.execution_id,
            task.step_name,
            task.kind,
            task.parent_task_id,
            task.state,
            task.args,
            task.kwargs,
            task.result,
            task.error,
            task.retries,
            task.created_at,
            task.started_at,
            task.completed_at,
            task.worker_id,
            task.lease_expires_at,
            json.dumps(task.tags),
            task.priority,
            task.queue,
            task.idempotency_key,
            self._policy_to_json(task.retry_policy),
            task.scheduled_for,
        )

    async def _insert_task(self, db: aiosqlite.Connection, task: TaskRecord) -> None:
        await db.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            self._task_row_values(task),
        )

    async def create_execution(self, record: ExecutionRecord) -> None:
        async with self._lock:
            db = await self._get_connection()
            await db.execute("BEGIN")
            try:
                await self._insert_execution(db, record)
                await db.execute("COMMIT")
            except Exception:
                await db.execute("ROLLBACK")
                raise

    async def create_execution_with_root_task(self, record: ExecutionRecord, task: TaskRecord) -> None:
        async with self._lock:
            db = await self._get_connection()
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._insert_execution(db, record)
                await self._insert_task(db, task)
                await db.execute("COMMIT")
            except Exception:
                await db.execute("ROLLBACK")
                raise

    async def get_execution(self, execution_id: str) -> ExecutionRecord | None:
        async with self._lock:
            db = await self._get_connection()
            async with db.execute("SELECT * FROM executions WHERE id = ?", (execution_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                
                # Fetch progress
                progress = []
                async with db.execute("SELECT * FROM execution_progress WHERE execution_id = ? ORDER BY ordinal", (execution_id,)) as p_cursor:
                    p_rows = await p_cursor.fetchall()
                    for pr in p_rows:
                         progress.append(self._row_to_progress(pr))

                return self._row_to_execution(row, progress)

    async def update_execution(self, record: ExecutionRecord) -> None:
        async with self._lock:
            db = await self._get_connection()
            await db.execute("BEGIN")
            try:
                await db.execute("""
                    UPDATE executions SET
                        state=?, args=?, kwargs=?, result=?, error=?, retries=?,
                        started_at=?, completed_at=?, expiry_at=?, tags=?,
                        priority=?, queue=?
                    WHERE id=?
                """, (
                    record.state, record.args, record.kwargs, record.result, record.error,
                    record.retries, record.started_at, record.completed_at, record.expiry_at,
                    json.dumps(record.tags), record.priority, record.queue, record.id
                ))
                # Do NOT update progress here as it is managed via execution_progress table
                await db.execute("COMMIT")
            except Exception:
                await db.execute("ROLLBACK")
                raise

    async def list_executions(self, limit: int = 10, offset: int = 0, state: str | None = None) -> List[ExecutionRecord]:
        async with self._lock:
            db = await self._get_connection()
            query = "SELECT * FROM executions"
            params: List[Any] = []
            if state:
                query += " WHERE state = ?"
                params.append(state)
            
            query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            
            async with db.execute(query, tuple(params)) as cursor:
                rows = await cursor.fetchall()
                results = []
                for row in rows:
                    # For listing, we might skip fetching progress to keep it light
                    results.append(self._row_to_execution(row, progress=[]))
                return results

    async def create_task(self, task: TaskRecord) -> None:
        await self.create_tasks([task])

    async def create_tasks(self, tasks: List[TaskRecord]) -> None:
        if not tasks:
            return
        async with self._lock:
            db = await self._get_connection()
            await db.execute("BEGIN")
            try:
                await db.executemany(
                    "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [self._task_row_values(task) for task in tasks],
                )
                await db.execute("COMMIT")
            except Exception:
                await db.execute("ROLLBACK")
                raise

    async def count_tasks(self, queue: str | None = None, state: str | None = None) -> int:
        async with self._lock:
            db = await self._get_connection()
            query = "SELECT COUNT(*) FROM tasks WHERE 1=1"
            params: List[Any] = []
            if queue:
                query += " AND queue = ?"
                params.append(queue)
            if state:
                query += " AND state = ?"
                params.append(state)
            
            async with db.execute(query, tuple(params)) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

    async def get_task(self, task_id: str) -> TaskRecord | None:
        async with self._lock:
            db = await self._get_connection()
            async with db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return self._row_to_task(row)

    async def update_task(self, task: TaskRecord) -> None:
        async with self._lock:
            db = await self._get_connection()
            await db.execute("BEGIN")
            try:
                await db.execute("""
                    UPDATE tasks SET
                        state=?, result=?, error=?, retries=?, started_at=?, completed_at=?,
                        worker_id=?, lease_expires_at=?
                    WHERE id=?
                """, (
                    task.state, task.result, task.error, task.retries, task.started_at,
                    task.completed_at, task.worker_id, task.lease_expires_at, task.id
                ))
                await db.execute("COMMIT")
            except Exception:
                await db.execute("ROLLBACK")
                raise

    async def list_tasks(self, limit: int = 10, offset: int = 0, state: str | None = None) -> List[TaskRecord]:
        async with self._lock:
            db = await self._get_connection()
            query = "SELECT * FROM tasks"
            params: List[Any] = []
            if state:
                query += " WHERE state = ?"
                params.append(state)
            
            query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            
            async with db.execute(query, tuple(params)) as cursor:
                rows = await cursor.fetchall()
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
        
        # Helper for queues condition
        queue_clause = ""
        params: List[Any] = [now, now]
        if queues:
            placeholders = ",".join(["?"] * len(queues))
            queue_clause = f"AND (queue IN ({placeholders}) OR queue IS NULL)"
            params.extend(queues)
        else:
            queue_clause = "AND 1=1"

        async with self._lock:
            db = await self._get_connection()
            await db.execute("BEGIN IMMEDIATE")
            try:
                query = f"""
                    SELECT * FROM tasks
                    WHERE (
                        state='pending'
                        OR (state='running' AND lease_expires_at < ?)
                    )
                    AND (scheduled_for IS NULL OR scheduled_for <= ?)
                    AND kind != 'signal'
                    {queue_clause}
                    ORDER BY priority DESC, created_at ASC
                    LIMIT 50
                """
                async with db.execute(query, tuple(params)) as cursor:
                    candidates = await cursor.fetchall()

                if not candidates:
                    await db.execute("ROLLBACK")
                    return None

                for row in candidates:
                    step_name = row["step_name"]
                    limit = concurrency_limits.get(step_name) if concurrency_limits else None

                    if limit is not None:
                        count_query = """
                            SELECT COUNT(*) FROM tasks 
                            WHERE step_name = ? 
                            AND state = 'running' 
                            AND lease_expires_at > ?
                        """
                        async with db.execute(count_query, (step_name, now)) as count_cursor:
                            count_row = await count_cursor.fetchone()
                            current_count = count_row[0] if count_row else 0

                        if current_count >= limit:
                            continue

                    claim_query = """
                        UPDATE tasks
                        SET state='running', worker_id=?, lease_expires_at=?, started_at=?
                        WHERE id = ?
                        AND (
                            state='pending'
                            OR (state='running' AND lease_expires_at < ?)
                        )
                        RETURNING *
                    """
                    claim_params = (worker_id, expires_at, now, row["id"], now)

                    async with db.execute(claim_query, claim_params) as claim_cursor:
                        claimed_row = await claim_cursor.fetchone()
                        if claimed_row:
                            await db.execute("COMMIT")
                            return self._row_to_task(claimed_row)

                await db.execute("ROLLBACK")
                return None
            except Exception:
                await db.execute("ROLLBACK")
                raise

    async def renew_task_lease(
        self,
        task_id: str,
        worker_id: str,
        lease_duration: timedelta,
    ) -> bool:
        new_expiry = datetime.now() + lease_duration
        async with self._lock:
            db = await self._get_connection()
            await db.execute("BEGIN")
            try:
                cursor = await db.execute(
                    """
                    UPDATE tasks
                    SET lease_expires_at=?
                    WHERE id=? AND worker_id=? AND state='running'
                    """,
                    (new_expiry, task_id, worker_id),
                )
                await db.execute("COMMIT")
                return (cursor.rowcount or 0) > 0
            except Exception:
                await db.execute("ROLLBACK")
                raise

    async def list_tasks_for_execution(self, execution_id: str) -> List[TaskRecord]:
        async with self._lock:
            db = await self._get_connection()
            async with db.execute("SELECT * FROM tasks WHERE execution_id = ?", (execution_id,)) as cursor:
                rows = await cursor.fetchall()
                return [self._row_to_task(row) for row in rows]

    async def append_progress(self, execution_id: str, progress: ExecutionProgress) -> None:
        async with self._lock:
            db = await self._get_connection()
            await db.execute("BEGIN")
            try:
                await db.execute(
                    "INSERT INTO execution_progress (execution_id, step, status, started_at, completed_at, detail) VALUES (?, ?, ?, ?, ?, ?)",
                    (execution_id, progress.step, progress.status, progress.started_at, progress.completed_at, progress.detail)
                )
                await db.execute("COMMIT")
            except Exception:
                await db.execute("ROLLBACK")
                raise

    async def get_cached_result(self, cache_key: str) -> bytes | None:
        async with self._lock:
            db = await self._get_connection()
            async with db.execute("SELECT value, expires_at FROM cache WHERE key = ?", (cache_key,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    val, expires_at = row[0], row[1]
                    logger.debug(f"Fetched from cache: key={cache_key}, expires_at={expires_at}, value_len={len(val) if val else 0}")
                    if expires_at and datetime.fromisoformat(expires_at) < datetime.now():
                        logger.debug(f"Cache expired for key={cache_key}")
                        return None
                    return val
                logger.debug(f"Cache miss for key={cache_key}")
        return None

    async def set_cached_result(self, cache_key: str, value: bytes, ttl: timedelta | None = None) -> None:
        expires_at = None
        if ttl:
            expires_at = datetime.now() + ttl
        async with self._lock:
            db = await self._get_connection()
            await db.execute("BEGIN")
            try:
                await db.execute(
                    "INSERT OR REPLACE INTO cache (key, value, expires_at) VALUES (?, ?, ?)",
                    (cache_key, value, expires_at)
                )
                await db.execute("COMMIT")
            except Exception:
                await db.execute("ROLLBACK")
                raise

    async def get_idempotency_result(self, idempotency_key: str) -> bytes | None:
        async with self._lock:
            db = await self._get_connection()
            async with db.execute("SELECT value FROM idempotency WHERE key = ?", (idempotency_key,)) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None

    async def set_idempotency_result(self, idempotency_key: str, value: bytes) -> None:
        async with self._lock:
            db = await self._get_connection()
            await db.execute("BEGIN")
            try:
                await db.execute(
                    "INSERT OR REPLACE INTO idempotency (key, value) VALUES (?, ?)",
                    (idempotency_key, value)
                )
                await db.execute("COMMIT")
            except Exception:
                await db.execute("ROLLBACK")
                raise

    async def move_task_to_dead_letter(self, task: TaskRecord, reason: str) -> None:
        async with self._lock:
            db = await self._get_connection()
            await db.execute("BEGIN")
            try:
                payload = task_record_to_json(task)
                await db.execute(
                    "INSERT INTO dead_tasks (id, reason, moved_at, data) VALUES (?, ?, ?, ?)",
                    (task.id, reason, datetime.now(), payload)
                )
                await db.execute("COMMIT")
            except Exception:
                await db.execute("ROLLBACK")
                raise

    def _row_to_dead_letter(self, row: Any) -> DeadLetterRecord:
        moved_at = row["moved_at"]
        if isinstance(moved_at, str):
            moved_at = datetime.fromisoformat(moved_at)
        task = task_record_from_json(row["data"])
        return DeadLetterRecord(task=task, reason=row["reason"], moved_at=moved_at)

    async def list_dead_tasks(self, limit: int = 50) -> List[DeadLetterRecord]:
        async with self._lock:
            db = await self._get_connection()
            async with db.execute(
                "SELECT * FROM dead_tasks ORDER BY moved_at DESC LIMIT ?",
                (limit,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [self._row_to_dead_letter(row) for row in rows]

    async def get_dead_task(self, task_id: str) -> DeadLetterRecord | None:
        async with self._lock:
            db = await self._get_connection()
            async with db.execute(
                "SELECT * FROM dead_tasks WHERE id = ?",
                (task_id,),
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return self._row_to_dead_letter(row)

    async def delete_dead_task(self, task_id: str) -> bool:
        async with self._lock:
            db = await self._get_connection()
            await db.execute("BEGIN")
            try:
                cursor = await db.execute(
                    "DELETE FROM dead_tasks WHERE id = ?",
                    (task_id,),
                )
                await db.execute("COMMIT")
                return (cursor.rowcount or 0) > 0
            except Exception:
                await db.execute("ROLLBACK")
                raise

    async def cleanup_executions(self, older_than: datetime) -> int:
        async with self._lock:
            db = await self._get_connection()
            await db.execute("BEGIN")
            try:
                where_clause = "completed_at IS NOT NULL AND completed_at < ? AND state IN ('completed', 'failed', 'timed_out', 'cancelled')"
                
                # Delete dependents using subquery
                await db.execute(f"DELETE FROM tasks WHERE execution_id IN (SELECT id FROM executions WHERE {where_clause})", (older_than,))
                await db.execute(f"DELETE FROM execution_progress WHERE execution_id IN (SELECT id FROM executions WHERE {where_clause})", (older_than,))
                await db.execute(f"DELETE FROM signals WHERE execution_id IN (SELECT id FROM executions WHERE {where_clause})", (older_than,))
                
                # Delete executions
                cursor = await db.execute(f"DELETE FROM executions WHERE {where_clause}", (older_than,))
                count = cursor.rowcount or 0
                await db.execute("COMMIT")
                return count
            except Exception:
                await db.execute("ROLLBACK")
                raise

    async def cleanup_dead_letters(self, older_than: datetime) -> int:
        """Remove dead letter records older than the specified datetime."""
        async with self._lock:
            db = await self._get_connection()
            await db.execute("BEGIN")
            try:
                cursor = await db.execute(
                    "DELETE FROM dead_tasks WHERE moved_at < ?",
                    (older_than,),
                )
                await db.execute("COMMIT")
                return cursor.rowcount or 0
            except Exception:
                await db.execute("ROLLBACK")
                raise

    async def create_signal(self, signal: SignalRecord) -> None:
        async with self._lock:
            db = await self._get_connection()
            await db.execute("BEGIN")
            try:
                await db.execute(
                    "INSERT OR REPLACE INTO signals (execution_id, name, payload, created_at, consumed, consumed_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (signal.execution_id, signal.name, signal.payload, signal.created_at, signal.consumed, signal.consumed_at)
                )
                await db.execute("COMMIT")
            except Exception:
                await db.execute("ROLLBACK")
                raise

    async def get_signal(self, execution_id: str, name: str) -> SignalRecord | None:
        async with self._lock:
            db = await self._get_connection()
            async with db.execute("SELECT * FROM signals WHERE execution_id = ? AND name = ?", (execution_id, name)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return SignalRecord(
                    execution_id=row["execution_id"],
                    name=row["name"],
                    payload=row["payload"],
                    created_at=datetime.fromisoformat(row["created_at"]) if isinstance(row["created_at"], str) else row["created_at"],
                    consumed=bool(row["consumed"]),
                    consumed_at=datetime.fromisoformat(row["consumed_at"]) if row["consumed_at"] and isinstance(row["consumed_at"], str) else row["consumed_at"]
                )


    def _progress_to_dict(self, p: ExecutionProgress) -> dict:
        return {
            "step": p.step,
            "status": p.status,
            "started_at": p.started_at.isoformat() if p.started_at else None,
            "completed_at": p.completed_at.isoformat() if p.completed_at else None,
            "detail": p.detail
        }

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
            created_at=datetime.fromisoformat(row["created_at"]) if isinstance(row["created_at"], str) else row["created_at"],
            started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] and isinstance(row["started_at"], str) else row["started_at"],
            completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] and isinstance(row["completed_at"], str) else row["completed_at"],
            expiry_at=datetime.fromisoformat(row["expiry_at"]) if row["expiry_at"] and isinstance(row["expiry_at"], str) else row["expiry_at"],
            progress=progress,
            tags=json.loads(row["tags"]),
            priority=row["priority"],
            queue=row["queue"]
        )

    def _row_to_progress(self, row: Any) -> ExecutionProgress:
        return ExecutionProgress(
            step=row["step"],
            status=row["status"],
            started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] and isinstance(row["started_at"], str) else row["started_at"],
            completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] and isinstance(row["completed_at"], str) else row["completed_at"],
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
            created_at=datetime.fromisoformat(row["created_at"]) if isinstance(row["created_at"], str) else row["created_at"],
            started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] and isinstance(row["started_at"], str) else row["started_at"],
            completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] and isinstance(row["completed_at"], str) else row["completed_at"],
            worker_id=row["worker_id"],
            lease_expires_at=datetime.fromisoformat(row["lease_expires_at"]) if row["lease_expires_at"] and isinstance(row["lease_expires_at"], str) else row["lease_expires_at"],
            tags=json.loads(row["tags"]),
            priority=row["priority"],
            queue=row["queue"],
            idempotency_key=row["idempotency_key"],
            retry_policy=self._json_to_policy(row["retry_policy"]),
            scheduled_for=datetime.fromisoformat(row["scheduled_for"]) if row["scheduled_for"] and isinstance(row["scheduled_for"], str) else row["scheduled_for"]
        )
