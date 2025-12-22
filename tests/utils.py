import os
import pytest
from senpuki import Senpuki

def get_test_backend(test_id: str):
    backend_type = os.environ.get("SENPUKI_TEST_BACKEND", "sqlite")
    
    if backend_type == "postgres":
        # Assumes a running postgres instance, e.g. via docker
        # DSN format: postgres://user:password@host:port/database
        # We append test_id to db name or use schemas? 
        # For simplicity, let's assume a single test db and we clear tables or use unique prefixes if needed.
        # BUT, standard practice for concurrent tests is separate DBs or schemas.
        # Here we will rely on the caller to cleanup or we can use a single DB and init_db ensures tables exist.
        dsn = os.environ.get("SENPUKI_TEST_PG_DSN", "postgres://postgres:postgres@localhost:5432/senpuki_test")
        return Senpuki.backends.PostgresBackend(dsn)
    else:
        db_path = f"test_senpuki_{test_id}.sqlite"
        return Senpuki.backends.SQLiteBackend(db_path)

async def cleanup_test_backend(backend):
    if hasattr(backend, "db_path"):
        import os
        if os.path.exists(backend.db_path):
            os.remove(backend.db_path)
    elif hasattr(backend, "pool") and backend.pool:
         # For postgres, we might want to truncate tables
         async with backend.pool.acquire() as conn:
             await conn.execute("TRUNCATE TABLE executions, execution_progress, tasks, dead_tasks, cache, idempotency CASCADE")
         await backend.pool.close()
