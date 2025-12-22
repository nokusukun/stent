# Fix Type Errors and Segfault

## Description
Addressed type errors reported by `pyrefly` and fixed a segmentation fault in tests by switching to the standard `aiosqlite` library.

## Key Changes
*   **`senpuki/backend/sqlite.py`**: 
    *   Switched import to use the installed `aiosqlite` library instead of the custom `senpuki.utils.async_sqlite` implementation (which was causing segfaults during tests).
    *   Fixed type mismatch errors by casting list parameters to tuples for SQL execution.
    *   Ensured `params` lists are typed as `List[Any]` where mixed types are used.
*   **`senpuki/notifications/redis.py`**:
    *   Updated `asyncio.expiry` to `asyncio.timeout` (Python 3.11+).
    *   Updated `asyncio.ExpiryError` to `asyncio.TimeoutError`.
*   **`senpuki/notifications/base.py`**:
    *   Updated `NotificationBackend` Protocol to use `def` for subscription methods, correctly typing them as returning `AsyncIterator` (matching async generator implementations).
*   **`senpuki/executor.py`**:
    *   Removed `await` when calling `subscribe_to_task` and `subscribe_to_execution`, as these now correctly return async generators immediately.
*   **`tests/test_execution.py`**:
    *   Added type annotation for `target_task` to fix a `NoneType` assignment error.

## Verification
*   `uv run pyrefly check` passes with 0 errors.
*   `uv run pytest` passes (21 tests).
