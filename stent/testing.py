"""Pytest fixtures and helpers for testing Stent durable functions.

Usage in your ``conftest.py``::

    from stent.testing import stent_backend, stent_executor, stent_worker

Or import the plugin directly::

    pytest_plugins = ["stent.testing"]
"""
from __future__ import annotations

import asyncio
import pytest_asyncio
from stent import Stent


@pytest_asyncio.fixture
async def stent_backend(tmp_path):
    """Provides an initialised SQLite backend in a temp directory."""
    db_path = str(tmp_path / "test_stent.sqlite")
    backend = Stent.backends.SQLiteBackend(db_path)
    await backend.init_db()
    yield backend
    await backend.close()


@pytest_asyncio.fixture
async def stent_executor(stent_backend):
    """Provides a ``Stent`` executor wired to the test backend.

    The executor is set as the default (``Stent.use``) so durable functions
    auto-dispatch. It is automatically reset after the test.
    """
    executor = Stent.use(backend=stent_backend)
    yield executor
    await executor.shutdown()
    Stent.reset()


@pytest_asyncio.fixture
async def stent_worker(stent_executor):
    """Starts a background worker and yields the executor.

    The worker is cancelled on teardown.
    """
    worker = asyncio.create_task(stent_executor.serve(poll_interval=0.05))
    # Wait for readiness
    lifecycle = stent_executor._default_worker_lifecycle
    if lifecycle:
        await lifecycle.wait_until_ready()
    yield stent_executor
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass
