from __future__ import annotations

import asyncio
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Awaitable

if TYPE_CHECKING:
    from senpuki.executor import Senpuki


class _TrackedOps:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def add(self, task: asyncio.Task[Any]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def flush(self) -> None:
        if not self._tasks:
            return
        tasks = list(self._tasks)
        self._tasks.clear()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result


class _CounterAccessor:
    def __init__(self, context: "ExecutionContext", name: str) -> None:
        self._context = context
        self._name = name

    def add(self, amount: int | float) -> None:
        self._context._track(self._context._add_counter(self._name, amount))


class _StateAccessor:
    def __init__(self, context: "ExecutionContext", key: str) -> None:
        self._context = context
        self._key = key

    def set(self, value: Any) -> None:
        self._context._track(self._context._set_state(self._key, value))


class ExecutionContext:
    def __init__(self, executor: "Senpuki", execution_id: str) -> None:
        self._executor = executor
        self.execution_id = execution_id
        self._tracked = _TrackedOps()

    def counters(self, name: str) -> _CounterAccessor:
        return _CounterAccessor(self, name)

    def state(self, key: str) -> _StateAccessor:
        return _StateAccessor(self, key)

    def _track(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tracked.add(task)

    async def _add_counter(self, name: str, amount: int | float) -> None:
        if not isinstance(amount, (int, float)):
            raise TypeError(f"Counter increment must be int/float, got {type(amount)}")
        await self._executor.backend.increment_execution_counter(self.execution_id, name, amount)

    async def _set_state(self, key: str, value: Any) -> None:
        payload = self._executor.serializer.dumps(value)
        await self._executor.backend.set_execution_state_value(self.execution_id, key, payload)

    async def initialize(
        self,
        *,
        counters: dict[str, int | float] | None = None,
        state: dict[str, Any] | None = None,
    ) -> None:
        if counters:
            for name, value in counters.items():
                if not isinstance(value, (int, float)):
                    raise TypeError(f"Counter initial value must be int/float, got {type(value)}")
            await self._executor.backend.ensure_execution_counters(self.execution_id, counters)

        if state:
            encoded = {key: self._executor.serializer.dumps(val) for key, val in state.items()}
            await self._executor.backend.ensure_execution_state_values(self.execution_id, encoded)

    async def flush(self) -> None:
        await self._tracked.flush()


current_execution_context: ContextVar[ExecutionContext | None] = ContextVar(
    "senpuki_execution_context",
    default=None,
)
