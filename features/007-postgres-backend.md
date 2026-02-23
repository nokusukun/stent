# PostgreSQL Backend

## Description
Added a PostgreSQL backend implementation using `asyncpg` to support scalable, production-grade deployments. Also refactored the test suite to allow dynamic backend selection via environment variables.

## Key Changes
*   **`stent/backend/postgres.py`**: Implemented `PostgresBackend` with `asyncpg`.
    *   Implements all `Backend` protocol methods.
    *   Uses atomic `SKIP LOCKED` for task claiming, ensuring safe concurrency with multiple workers.
*   **`stent/executor.py`**: Added `PostgresBackend` to `Backends` helper.
*   **`tests/utils.py`**: Added `get_test_backend` and `cleanup_test_backend` to support `STENT_TEST_BACKEND=postgres`.
*   **Tests**: Refactored all tests (`tests/*.py`) to use `get_test_backend` instead of hardcoding `SQLiteBackend`.
*   **Dependencies**: Added `asyncpg`.

## Usage/Configuration

**Using Postgres in Application:**
```python
executor = Stent(backend=Stent.backends.PostgresBackend("postgres://user:pass@localhost:5432/db"))
```

**Running Tests with Postgres:**
```bash
# Ensure Postgres is running (e.g., docker run -p 5432:5432 -e POSTGRES_PASSWORD=postgres postgres)
export STENT_TEST_BACKEND=postgres
export STENT_TEST_PG_DSN="postgres://postgres:postgres@localhost:5432/stent_test"
uv run pytest
```
