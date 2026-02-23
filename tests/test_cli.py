import sys
from datetime import datetime
from typing import Any, cast

import pytest

import stent.cli as cli
from stent.core import ExecutionState, Result


class _SpyBackend:
    def __init__(self, *, raise_on_list: bool = False):
        self.raise_on_list = raise_on_list
        self.init_calls = 0
        self.close_calls = 0

    async def init_db(self) -> None:
        self.init_calls += 1

    async def close(self) -> None:
        self.close_calls += 1

    async def list_executions(self, limit: int = 10, offset: int = 0, state: str | None = None):
        if self.raise_on_list:
            raise RuntimeError("boom")
        return []


class _StatsBackend(_SpyBackend):
    def __init__(self):
        super().__init__()
        self.count_executions_calls = 0
        self.count_dead_tasks_calls = 0

    async def count_executions(self, state: str | None = None) -> int:
        self.count_executions_calls += 1
        return 0

    async def count_tasks(self, queue: str | None = None, state: str | None = None) -> int:
        return 0

    async def list_tasks(self, limit: int = 10, offset: int = 0, state: str | None = None):
        return []

    async def count_dead_tasks(self) -> int:
        self.count_dead_tasks_calls += 1
        return 0

    async def list_dead_tasks(self, limit: int = 50):
        raise AssertionError("stats should use count_dead_tasks, not list_dead_tasks")

    async def get_task(self, task_id: str):
        return None

    async def get_execution(self, execution_id: str):
        return None


class _ShowExecutor:
    async def state_of(self, _execution_id: str) -> ExecutionState:
        return ExecutionState(
            id="exec-1",
            state="running",
            result=Result.Ok("ok"),
            started_at=datetime.now(),
            completed_at=None,
            retries=0,
            progress=[],
            tags=["alpha"],
            priority=1,
            queue=None,
            counters={"progress": 1, "volume": 30, "abc": 40},
            custom_state={"owner": "foo", "phase": "bar_done"},
        )


@pytest.mark.asyncio
async def test_cli_initializes_and_closes_backend(monkeypatch):
    backend = _SpyBackend()

    monkeypatch.setattr(cli.Stent.backends, "SQLiteBackend", staticmethod(lambda _db: backend))
    monkeypatch.setattr(sys, "argv", ["stent", "list"])

    result = await cli.main_async()
    assert result == 0
    assert backend.init_calls == 1
    assert backend.close_calls == 1


@pytest.mark.asyncio
async def test_cli_closes_backend_when_command_errors(monkeypatch):
    backend = _SpyBackend(raise_on_list=True)

    monkeypatch.setattr(cli.Stent.backends, "SQLiteBackend", staticmethod(lambda _db: backend))
    monkeypatch.setattr(sys, "argv", ["stent", "list"])

    with pytest.raises(RuntimeError, match="boom"):
        await cli.main_async()

    assert backend.init_calls == 1
    assert backend.close_calls == 1


@pytest.mark.asyncio
async def test_cli_stats_uses_count_apis(monkeypatch):
    backend = _StatsBackend()

    async def _forbidden_list_executions(limit: int = 10, offset: int = 0, state: str | None = None):
        raise AssertionError("stats should use count_executions, not list_executions")

    backend.list_executions = _forbidden_list_executions  # type: ignore[method-assign]

    monkeypatch.setattr(cli.Stent.backends, "SQLiteBackend", staticmethod(lambda _db: backend))
    monkeypatch.setattr(sys, "argv", ["stent", "stats"])

    result = await cli.main_async()

    assert result == 0
    assert backend.init_calls == 1
    assert backend.close_calls == 1
    assert backend.count_executions_calls > 0
    assert backend.count_dead_tasks_calls > 0


@pytest.mark.asyncio
async def test_show_execution_prints_counters_and_custom_state(capsys):
    class _Args:
        id = "exec-1"

    result = await cli.show_execution(cast(Any, _ShowExecutor()), _Args())
    out = capsys.readouterr().out

    assert result == 0
    assert "Counters" in out
    assert "progress: 1" in out
    assert "volume: 30" in out
    assert "abc: 40" in out
    assert "Custom State" in out
    assert "owner: foo" in out
    assert "phase: bar_done" in out
