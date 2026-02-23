from __future__ import annotations
import asyncio
import functools
import uuid
import logging
from datetime import datetime, timedelta
from typing import Callable, Awaitable, Any, ClassVar, List, Literal, Optional, cast
from contextvars import ContextVar

from stent.core import (
    Result, RetryPolicy, TaskRecord, ExecutionProgress, 
    ExecutionState, DeadLetterRecord
)
from stent.backend.base import Backend
from stent.notifications.base import NotificationBackend
from stent.registry import registry, FunctionMetadata, FunctionRegistry
from stent.utils.serialization import Serializer, JsonSerializer
from stent.utils.idempotency import default_idempotency_key
from stent.utils.time import parse_duration
from stent.executor_signals import persist_signal_and_wake_waiter, resolve_signal_wait
from stent.executor_orchestration import (
    build_dispatch_records,
    execute_map_batch,
    normalize_dispatch_timing,
    persist_dispatch_records,
    schedule_activity_and_wait,
)
from stent.executor_wait import wait_for_execution_terminal, wait_for_task_terminal
from stent.executor_worker import lease_heartbeat_loop, run_claimed_task
from stent.executor_context import ExecutionContext, current_execution_context
from stent.metrics import MetricsRecorder, NoOpMetricsRecorder

from dataclasses import dataclass, field, replace

logger = logging.getLogger(__name__)

class StentLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.stent_execution_id = current_execution_id.get()
        record.stent_task_id = current_task_id.get()
        record.stent_worker_id = current_worker_id.get()
        return True

def install_structured_logging(target: logging.Logger | None = None) -> None:
    """
    Adds a logging.Filter that injects Stent context (execution/task id) into
    every log record. Integrate it with your formatter via
    %(stent_execution_id)s etc.
    """
    logger_obj = target or logging.getLogger("stent")
    for existing in getattr(logger_obj, "filters", []):
        if isinstance(existing, StentLogFilter):
            return
    logger_obj.addFilter(StentLogFilter())

install_structured_logging(logger)

class ExpiryError(TimeoutError):
    pass


class UnregisteredFunctionError(RuntimeError):
    def __init__(self, function_name: str):
        message = (
            f"No durable registration found for '{function_name}'. "
            "Decorate the function with @Stent.durable() or register it on the "
            "FunctionRegistry provided to the executor."
        )
        super().__init__(message)
        self.function_name = function_name

@dataclass
class PermitHolder:
    sem: asyncio.Semaphore
    held: bool = True

    def release(self):
        if self.held:
            self.sem.release()
            self.held = False
    
    async def acquire(self):
        if not self.held:
            await self.sem.acquire()
            self.held = True

@dataclass(eq=False)
class WorkerLifecycle:
    """
    Represents the lifecycle of a long-running worker loop. Callers can use it
    to coordinate readiness / draining with their process manager (e.g. K8s).
    """
    name: str | None = None
    ready_event: asyncio.Event = field(default_factory=asyncio.Event)
    draining_event: asyncio.Event = field(default_factory=asyncio.Event)
    stopped_event: asyncio.Event = field(default_factory=asyncio.Event)
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    state: Literal["starting", "ready", "draining", "stopped"] = "starting"

    def __hash__(self) -> int:
        return id(self)

    def reset(self) -> None:
        self.ready_event.clear()
        self.draining_event.clear()
        self.stopped_event.clear()
        self.stop_event.clear()
        self.state = "starting"

    def mark_ready(self) -> None:
        self.state = "ready"
        self.ready_event.set()

    def mark_draining(self) -> None:
        if self.state != "draining":
            self.state = "draining"
            self.draining_event.set()

    def mark_stopped(self) -> None:
        self.state = "stopped"
        self.stopped_event.set()

    def request_drain(self) -> None:
        self.stop_event.set()

    async def wait_until_ready(self) -> None:
        await self.ready_event.wait()

    async def wait_until_stopped(self) -> None:
        await self.stopped_event.wait()

# Backends helper
class Backends:
    @staticmethod
    def SQLiteBackend(path: str) -> Backend:
        from stent.backend.sqlite import SQLiteBackend
        return SQLiteBackend(path)

    @staticmethod
    def MongoBackend(url: str, db_name: str) -> Backend:
        # Placeholder
        raise NotImplementedError("Mongo backend not implemented yet")

    @staticmethod
    def PostgresBackend(dsn: str) -> Backend:
        from stent.backend.postgres import PostgresBackend
        return PostgresBackend(dsn)

class Notifications:
    @staticmethod
    def RedisBackend(url: str) -> NotificationBackend:
        from stent.notifications.redis import RedisBackend
        return RedisBackend(url)

# Capture original sleep before any patching
_original_sleep = asyncio.sleep

class DurableFunction:
    """Wrapper returned by @Stent.durable(). Callable like a normal async
    function, but also exposes .map() and .starmap() for batch dispatch."""

    def __init__(self, meta: FunctionMetadata):
        self._meta = meta
        functools.update_wrapper(self, meta.fn)

    async def __call__(self, *args, **kwargs):
        return await Stent._call_durable_stub(self._meta, args, kwargs)

    async def map(self, iterable) -> list[Any]:
        """Dispatch this function for each item in *iterable* (single arg per call).

        ``await square.map([1, 2, 3])``  ➜  ``[square(1), square(2), square(3)]``
        """
        return await self._map_internal(((item,), {}) for item in iterable)

    async def starmap(self, iterable) -> list[Any]:
        """Dispatch this function for each entry in *iterable*, unpacking args.

        Each entry can be:
        - a tuple of positional args:  ``(a, b)``  ➜  ``fn(a, b)``
        - a (tuple, dict) pair:        ``((a,), {"k": v})``  ➜  ``fn(a, k=v)``

        ``await send_email.starmap([("alice", "hi"), ("bob", "hey")])``
        """
        def _normalise(entry):
            if isinstance(entry, dict):
                return ((), entry)
            if isinstance(entry, (list, tuple)) and len(entry) == 2 and isinstance(entry[1], dict):
                return (tuple(entry[0]), entry[1])
            return (tuple(entry) if isinstance(entry, (list, tuple)) else (entry,), {})
        return await self._map_internal(_normalise(e) for e in iterable)

    async def _map_internal(self, args_iter) -> list[Any]:
        executor: Stent | None = current_executor.get()
        if not executor:
            executor = Stent._default_executor
        if not executor:
            return await asyncio.gather(*[self._meta.fn(*a, **kw) for a, kw in args_iter])

        return await execute_map_batch(
            executor=executor,
            meta=self._meta,
            args_list=list(args_iter),
            exec_id=current_execution_id.get() or "",
            parent_id=current_task_id.get() or "",
            permit_holder=current_permit_holder.get(),
        )


current_execution_id: ContextVar[str | None] = ContextVar("stent_execution_id", default=None)
current_task_id: ContextVar[str | None] = ContextVar("stent_task_id", default=None)
current_worker_semaphore: ContextVar[asyncio.Semaphore | None] = ContextVar("stent_worker_semaphore", default=None)
current_permit_holder: ContextVar[PermitHolder | None] = ContextVar("stent_permit_holder", default=None)
current_worker_id: ContextVar[str | None] = ContextVar("stent_worker_id", default=None)

class Stent:
    backends = Backends
    notifications = Notifications
    default_registry: FunctionRegistry = registry
    _default_executor: ClassVar[Optional[Stent]] = None

    @classmethod
    def use(
        cls,
        backend: Backend,
        serializer: Serializer | Literal["json", "pickle"] = "json",
        notification_backend: NotificationBackend | None = None,
        poll_min_interval: float = 0.1,
        poll_max_interval: float = 5.0,
        poll_backoff_factor: float = 2.0,
        function_registry: FunctionRegistry | None = None,
        metrics: MetricsRecorder | None = None,
    ) -> Stent:
        """Configure a default executor for auto-dispatch.

        After calling this, any ``@Stent.durable()`` function called outside
        of a worker context will automatically dispatch through this executor
        and wait for the result.
        """
        instance = cls(
            backend=backend,
            serializer=serializer,
            notification_backend=notification_backend,
            poll_min_interval=poll_min_interval,
            poll_max_interval=poll_max_interval,
            poll_backoff_factor=poll_backoff_factor,
            function_registry=function_registry,
            metrics=metrics,
        )
        cls._default_executor = instance
        return instance

    @classmethod
    def reset(cls):
        """Clear the default executor (useful for test teardown)."""
        cls._default_executor = None

    def __init__(
        self,
        backend: Backend,
        serializer: Serializer | Literal["json", "pickle"] = "json",
        notification_backend: NotificationBackend | None = None,
        poll_min_interval: float = 0.1,
        poll_max_interval: float = 5.0,
        poll_backoff_factor: float = 2.0,
        function_registry: FunctionRegistry | None = None,
        metrics: MetricsRecorder | None = None,
    ):
        self.backend = backend
        self.registry = function_registry or self.default_registry
        self.serializer: Serializer

        if isinstance(serializer, str):
            if serializer == "json":
                self.serializer = JsonSerializer()
            else:
                 # Local import to avoid circular dependency if pickle serializer was here
                from stent.utils.serialization import PickleSerializer
                self.serializer = PickleSerializer()
        else:
            self.serializer = serializer
            
        self.notification_backend = notification_backend
        self.poll_min_interval = max(0.001, poll_min_interval)
        self.poll_max_interval = max(self.poll_min_interval, poll_max_interval)
        self.poll_backoff_factor = poll_backoff_factor if poll_backoff_factor >= 1.0 else 1.0
        self.metrics: MetricsRecorder = metrics or NoOpMetricsRecorder()
        
        # Tracks background asyncio.Task instances spawned by this executor so their
        # lifecycle can be managed (for example, awaiting or cleanup on shutdown).
        self.background_tasks: set[asyncio.Task[Any]] = set()
        self._active_worker_lifecycles: set[WorkerLifecycle] = set()
        self._default_worker_lifecycle: WorkerLifecycle | None = None
        
        self._register_builtin_tasks()

    def _register_builtin_tasks(self):
        if self.registry.get("stent.sleep"):
            return

        async def sleep_impl(duration: float):
            pass
            
        meta = FunctionMetadata(
            name="stent.sleep",
            fn=sleep_impl,
            cached=False,
            retry_policy=RetryPolicy(),
            tags=["builtin"],
            priority=0,
            queue=None,
            idempotent=True,
            idempotency_key_func=None,
            version="1.0"
        )
        self.registry.register(meta)

    def create_worker_lifecycle(self, *, name: str | None = None) -> WorkerLifecycle:
        """
        Returns a WorkerLifecycle handle that can be passed into serve() so the
        caller can coordinate readiness / draining signals with their host.
        """
        return WorkerLifecycle(name=name)

    def active_worker_lifecycles(self) -> List[WorkerLifecycle]:
        return list(self._active_worker_lifecycles)

    def worker_status_overview(self) -> dict[str, Any]:
        """
        Returns a summary that external health checks can expose.
        """
        handles = self.active_worker_lifecycles()
        ready = any(h.state in ("ready", "draining") and h.ready_event.is_set() for h in handles)
        draining = any(h.state == "draining" for h in handles)
        return {
            "ready": ready,
            "draining": draining,
            "workers": [
                {
                    "name": handle.name or f"worker-{idx}",
                    "state": handle.state,
                }
                for idx, handle in enumerate(handles)
            ],
        }

    def request_worker_drain(self, lifecycle: WorkerLifecycle | None = None) -> None:
        """
        Signals all active workers (or a specific lifecycle) to stop accepting
        new work while letting in-flight tasks finish.
        """
        if lifecycle:
            lifecycle.request_drain()
            return

        for handle in list(self._active_worker_lifecycles):
            handle.request_drain()
        if not self._active_worker_lifecycles and self._default_worker_lifecycle:
            self._default_worker_lifecycle.request_drain()

    async def schedule(
        self, 
        delay: str | dict | timedelta, 
        fn: Callable[..., Awaitable[Any]], 
        *args, 
        **kwargs
    ) -> str:
        """
        Schedule a function to run after a specific delay.
        """
        return await self.dispatch(fn, *args, delay=delay, **kwargs)

    @staticmethod
    async def sleep(duration: str | dict | timedelta):
        """
        Static helper to sleep for a specific duration.
        Proxies to the global sleep function which uses the current executor context.
        """
        # Local import or direct call to global sleep if available in scope
        # Since 'sleep' is defined at the end of this file, we can't call it directly if it's not defined yet.
        # But methods are bound at runtime.
        # However, 'sleep' is defined AFTER 'Stent' class.
        # We can use 'current_executor' directly here.
        executor = current_executor.get()
        if executor:
            await executor.sleep_instance(duration)
        else:
            d = parse_duration(duration)
            await _original_sleep(d.total_seconds())

    async def sleep_instance(self, duration: str | dict | timedelta | int | float):
        """
        Sleep for a specific duration.
        This releases the worker to process other tasks while waiting.
        """
        if isinstance(duration, (int, float)):
            d = timedelta(seconds=duration)
        else:
            d = parse_duration(duration)
        meta = self.registry.get("stent.sleep")
        if not meta:
            self._register_builtin_tasks()
            meta = self.registry.get("stent.sleep")
        if not meta:
            raise RuntimeError("stent.sleep not registered")
        
        # Schedule the sleep task to run AFTER the duration.
        # The orchestrator will wait for it to complete.
        await self._schedule_activity(meta, (d.total_seconds(),), {}, None, delay=d)

    @classmethod
    def durable(
        cls,
        *,
        cached: bool = False,
        retry_policy: RetryPolicy | None = None,
        tags: List[str] | None = None,
        priority: int = 0,
        queue: str | None = None,
        idempotent: bool = False,
        idempotency_key_func: Callable[..., str] | None = None,
        version: str | None = None,
        max_concurrent: int | None = None,
    ):
        def decorator(fn):
            reg = cls.default_registry
            name = reg.name_for_function(fn)
            meta = FunctionMetadata(
                name=name,
                fn=fn,
                cached=cached,
                retry_policy=retry_policy or RetryPolicy(),
                tags=tags or [],
                priority=priority,
                queue=queue,
                idempotent=idempotent,
                idempotency_key_func=idempotency_key_func,
                version=version,
                max_concurrent=max_concurrent,
            )
            reg.register(meta)
            return DurableFunction(meta)
        return decorator

    @classmethod
    async def wrap(cls, fn: Callable[..., Awaitable[Any]], args: tuple, kwargs: dict | None = None):
         if kwargs is None:
             kwargs = {}

         executor = current_executor.get()
         reg = executor.registry if executor else cls.default_registry
         name = reg.name_for_function(fn)
         meta = reg.get(name)
         if not meta:
             raise UnregisteredFunctionError(name)

         return await cls._call_durable_stub(meta, args, kwargs)

    @classmethod
    async def map(
        cls,
        fn: Callable[..., Awaitable[Any]],
        iterable: Any
    ) -> List[Any]:
        """Batch-dispatch *fn* for each item in *iterable*.

        Prefer ``fn.map(items)`` instead.  This classmethod is kept for
        backwards compatibility.
        """
        if isinstance(fn, DurableFunction):
            return await fn.map(iterable)

        executor: Optional[Stent] = current_executor.get()
        if not executor:
            return await asyncio.gather(*[fn(item) for item in iterable])

        reg = executor.registry
        name = reg.name_for_function(fn)
        meta = reg.get(name)
        if not meta:
            raise UnregisteredFunctionError(name)

        return await execute_map_batch(
            executor=executor,
            meta=meta,
            args_list=[((item,), {}) for item in iterable],
            exec_id=current_execution_id.get() or "",
            parent_id=current_task_id.get() or "",
            permit_holder=current_permit_holder.get(),
        )

    @classmethod
    async def gather(cls, *tasks, **kwargs):
        """
        Alias for asyncio.gather. 
        Note: If you pass function calls (e.g. `stent.gather(func(1), func(2))`), 
        they are scheduled immediately when called, not batched by gather.
        Use `stent.map` for batch scheduling optimization if applicable.
        
        Supports `return_exceptions=True`.
        """
        return await asyncio.gather(*tasks, **kwargs)

    @classmethod
    def context(
        cls,
        *,
        counters: dict[str, int | float] | None = None,
        state: dict[str, Any] | None = None,
    ) -> ExecutionContext:
        executor = current_executor.get()
        if not executor:
            raise RuntimeError("Cannot access context outside of durable function")

        exec_id = current_execution_id.get()
        if not exec_id:
            raise RuntimeError("Cannot access context without active execution")

        ctx = current_execution_context.get()
        if ctx is None:
            ctx = ExecutionContext(executor, exec_id)
            current_execution_context.set(ctx)

        if counters or state:
            ctx._track(ctx.initialize(counters=counters, state=state))
        return ctx

    @classmethod
    async def _call_durable_stub(cls, meta: FunctionMetadata, args: tuple, kwargs: dict):
        executor = current_executor.get() # Get the current executor if any
        exec_id = current_execution_id.get()
        
        # If no executor in context, check for a default executor for auto-dispatch.
        # If neither exists, fall through to a direct local call (unit test / no-runtime mode).
        if not executor:
            executor = cls._default_executor
            if not executor:
                logger.debug(f"STUB: {meta.name} running locally (no executor context)")
                return await meta.fn(*args, **kwargs)

            # Auto-dispatch: create a full execution and wait for a worker to process it.
            exec_id = await executor.dispatch(meta.fn, *args, **kwargs)
            result = await executor.wait_for(exec_id)
            if result.ok:
                return result.value
            if isinstance(result.error, BaseException):
                raise result.error
            raise Exception(str(result.error))

        # Now we know we are within an executor's context (i.e., this is a distributed call)

        # Idempotency / Caching check - applies to any durable function call within an execution
        # This key is generated here and passed down if not a hit.
        key = None
        if meta.idempotent or meta.cached:
             if meta.idempotency_key_func:
                 key = meta.idempotency_key_func(*args, **kwargs)
             else:
                 key = default_idempotency_key(meta.name, meta.version, args, kwargs, serializer=executor.serializer)

        if key:
            if meta.cached:
                 cached_val = await executor.backend.get_cached_result(key)
                 logger.debug(f"_call_durable_stub: fetched cached_val: {cached_val is not None} for key {key}")
                 logger.debug(f"_call_durable_stub: type(cached_val)={type(cached_val)}, cached_val={cached_val}, bool(cached_val)={bool(cached_val)}")
                 should_hit_cache = (cached_val is not None)
                 logger.debug(f"_call_durable_stub: should_hit_cache={should_hit_cache}")
                 if should_hit_cache:
                      logger.info(f"Cache HIT for {meta.name} with key {key}")
                      if exec_id: # Only append progress if we are in an execution
                          await executor.backend.append_progress(exec_id, ExecutionProgress(
                              step=meta.name, status="cache_hit"
                          ))
                      return executor.serializer.loads(cached_val)

            # This 'if' should be at the same indentation level as 'if meta.cached:'
            if meta.idempotent:
                stored_val = await executor.backend.get_idempotency_result(key)
                if stored_val is not None:
                    logger.info(f"Idempotency HIT for {meta.name} with key {key}")
                    if exec_id: # Only append progress if we are in an execution
                        await executor.backend.append_progress(exec_id, ExecutionProgress(
                            step=meta.name, status="cache_hit" # Using cache_hit status for idempotency too
                        ))
                    return executor.serializer.loads(stored_val)

        # If we reached here, it's not a cache/idempotency hit, so schedule as an activity.
        # The key is passed so _schedule_activity can store it in the TaskRecord.
        completed_task = await executor._schedule_activity(meta, args, kwargs, key)
        
        if completed_task.state == "failed":
            # Propagate error from activity
            if completed_task.error:
                err = executor.serializer.loads(completed_task.error)
                if isinstance(err, dict) and "__type__" in err: 
                    raise Exception(str(err))
                if isinstance(err, BaseException):
                    raise err
                raise Exception(str(err))
            raise Exception("Task failed without error info")

        completed_task_result = completed_task.result or b"null"
        res = executor.serializer.loads(completed_task_result)
        
        # Store cache/idempotency if enabled and key was generated after successful execution
        if key:
            if meta.cached:
                await executor.backend.set_cached_result(key, completed_task_result)
                logger.debug(f"Stored cached result for {meta.name} with key {key}")
            if meta.idempotent:
                await executor.backend.set_idempotency_result(key, completed_task_result)
                logger.debug(f"Stored idempotency result for {meta.name} with key {key}")
            
        return res

    async def dispatch(
        self,
        fn: Callable[..., Awaitable[Any]],
        *args,
        expiry: str | timedelta | None = None,
        max_duration: str | timedelta | None = None,
        delay: str | dict | timedelta | None = None,
        tags: List[str] | None = None,
        priority: int = 0,
        queue: str | None = None,
        **kwargs,
    ) -> str:
        reg = self.registry
        name = reg.name_for_function(fn)
        meta = reg.get(name)
        if not meta:
            raise UnregisteredFunctionError(name)

        normalized_expiry, normalized_delay = normalize_dispatch_timing(
            expiry=expiry,
            max_duration=max_duration,
            delay=delay,
        )

        execution_id, record, task = build_dispatch_records(
            name=name,
            args_bytes=self.serializer.dumps(args),
            kwargs_bytes=self.serializer.dumps(kwargs),
            retry_policy=meta.retry_policy,
            tags=tags or meta.tags,
            priority=priority,
            queue=queue or meta.queue,
            expiry=normalized_expiry,
            delay=normalized_delay,
        )
        await persist_dispatch_records(executor=self, execution=record, root_task=task)

        return execution_id

    async def send_signal(self, execution_id: str, name: str, payload: Any) -> None:
        """
        Send a signal to a running execution. 
        If the execution is waiting for this signal, it will be resumed.
        If not, the signal will be buffered.
        """
        payload_bytes = self.serializer.dumps(payload)
        await persist_signal_and_wake_waiter(
            execution_id=execution_id,
            name=name,
            payload_bytes=payload_bytes,
            create_signal=self.backend.create_signal,
            list_tasks_for_execution=self.backend.list_tasks_for_execution,
            update_task=self.backend.update_task,
            notify_task_completed=(
                self.notification_backend.notify_task_completed
                if self.notification_backend
                else None
            ),
        )

    @staticmethod
    async def wait_for_signal(name: str) -> Any:
        """
        Static helper to wait for a signal.
        Proxies to the global executor context.
        """
        executor = current_executor.get()
        if not executor:
             raise Exception("Cannot wait for signal outside of durable function")
        return await executor.wait_for_signal_instance(name)

    async def wait_for_signal_instance(self, name: str) -> Any:
        """
        Pauses the current workflow until a signal with the given name is received.
        """
        exec_id = current_execution_id.get()
        if not exec_id:
             raise Exception("Cannot wait for signal outside of durable function")
             
        return await resolve_signal_wait(
            execution_id=exec_id,
            name=name,
            parent_task_id=current_task_id.get(),
            loads=self.serializer.loads,
            get_task=self.backend.get_task,
            get_signal=self.backend.get_signal,
            create_signal=self.backend.create_signal,
            create_task=self.backend.create_task,
            append_progress=self.backend.append_progress,
            wait_for_task=self._wait_for_task,
        )

    async def _schedule_activity(
        self, 
        meta: FunctionMetadata, 

        args: tuple, 
        kwargs: dict, 
        idempotency_key: str | None,
        delay: timedelta | None = None
    ) -> TaskRecord:
        return await schedule_activity_and_wait(
            executor=self,
            meta=meta,
            args=args,
            kwargs=kwargs,
            idempotency_key=idempotency_key,
            delay=delay,
            exec_id=current_execution_id.get() or "",
            parent_id=current_task_id.get() or "",
        )

    async def _wait_for_task_internal(self, task_id: str, expiry: float | None = None) -> TaskRecord:
        """Waits for task completion without modifying semaphore."""
        return await wait_for_task_terminal(
            task_id=task_id,
            get_task=self.backend.get_task,
            notification_backend=self.notification_backend,
            expiry=expiry,
            poll_min_interval=self.poll_min_interval,
            poll_max_interval=self.poll_max_interval,
            poll_backoff_factor=self.poll_backoff_factor,
            sleep_fn=_original_sleep,
            expiry_error_factory=ExpiryError,
        )

    async def _wait_for_task(self, task_id: str, expiry: float | None = None) -> TaskRecord:
        permit = current_permit_holder.get()
        if permit:
            permit.release()
            
        try:
            return await self._wait_for_task_internal(task_id, expiry)
        finally:
            if permit:
                await permit.acquire()

    async def state_of(self, execution_id: str) -> ExecutionState:
        record = await self.backend.get_execution(execution_id)
        if not record:
            raise ValueError("Execution not found")

        counters_method = getattr(self.backend, "get_execution_counters", None)
        if callable(counters_method):
            counters = await cast(Awaitable[dict[str, int | float]], counters_method(execution_id))
        else:
            counters = {}

        state_method = getattr(self.backend, "get_execution_state_values", None)
        if callable(state_method):
            state_values = await cast(Awaitable[dict[str, bytes]], state_method(execution_id))
        else:
            state_values = {}
        custom_state = {
            key: self.serializer.loads(value)
            for key, value in state_values.items()
        }
        # Optional: refresh progress?
        return ExecutionState(
            id=record.id,
            state=record.state,
            result=self.serializer.loads(record.result) if record.result else None,
            started_at=record.started_at,
            completed_at=record.completed_at,
            retries=record.retries,
            progress=record.progress,
            tags=record.tags,
            priority=record.priority,
            queue=record.queue,
            counters=counters,
            custom_state=custom_state,
        )

    async def result_of(self, execution_id: str) -> Result[Any, Any]:
        record = await self.backend.get_execution(execution_id)
        if not record:
            raise ValueError("Execution not found")
        if record.state not in ("completed", "failed", "timed_out", "cancelled"):
             raise Exception("Execution still running")
        
        if record.result:
            val = self.serializer.loads(record.result)
            if isinstance(val, Result):
                return val
            return Result.Ok(val)
        if record.error:
            # Wrap error in Result.Error if it's raw exception
            err = self.serializer.loads(record.error)
            if isinstance(err, Result):
                 return err
            return Result.Error(err)

        if record.state == "cancelled":
            return Result.Error(Exception("Execution cancelled"))
             
        raise Exception("No result available")

    async def wait_for(self, execution_id: str, expiry: float | None = None) -> Result[Any, Any]:
        """
        Blocks until the execution with the given ID is completed, failed, or timed out.
        Returns the result of the execution.
        """
        # Quick check first
        try:
            return await self.result_of(execution_id)
        except Exception:
            pass # Not done yet

        await wait_for_execution_terminal(
            execution_id=execution_id,
            state_of=self.state_of,
            notification_backend=self.notification_backend,
            expiry=expiry,
            sleep_interval=0.5,
            expiry_error_factory=ExpiryError,
        )

        return await self.result_of(execution_id)

    async def list_executions(self, limit: int = 10, offset: int = 0, state: str | None = None) -> List[ExecutionState]:
        """
        List executions with optional filtering by state.
        Returns a list of ExecutionState objects (without full progress history for efficiency).
        """
        records = await self.backend.list_executions(limit, offset, state)
        return [
            ExecutionState(
                id=r.id,
                state=r.state,
                result=None, # Don't deserialize result for summary
                started_at=r.started_at,
                completed_at=r.completed_at,
                retries=r.retries,
                progress=[], # Skip progress
                tags=r.tags,
                priority=r.priority,
                queue=r.queue
            ) for r in records
        ]

    async def queue_depth(self, queue: str | None = None) -> int:
        """
        Returns the number of pending tasks in the specified queue (or all queues if None).
        """
        return await self.backend.count_tasks(queue=queue, state="pending")

    async def get_running_activities(self) -> List[TaskRecord]:
        """
        Returns a list of currently running tasks (activities).
        """
        return await self.backend.list_tasks(limit=100, state="running")

    async def list_dead_letters(self, limit: int = 50) -> List[DeadLetterRecord]:
        """
        Returns the most recent dead-lettered tasks.
        """
        return await self.backend.list_dead_tasks(limit=limit)

    async def get_dead_letter(self, task_id: str) -> DeadLetterRecord | None:
        return await self.backend.get_dead_task(task_id)

    async def discard_dead_letter(self, task_id: str) -> bool:
        return await self.backend.delete_dead_task(task_id)

    async def replay_dead_letter(
        self,
        task_id: str,
        *,
        queue: str | None = None,
        reset_retries: bool = True,
    ) -> str:
        """
        Re-enqueues a dead-lettered task with a fresh task ID so it can run again.
        """
        record = await self.get_dead_letter(task_id)
        if not record:
            raise ValueError(f"Dead-letter task {task_id} not found")

        new_task = replace(record.task)
        original_id = new_task.id
        new_task.id = str(uuid.uuid4())
        new_task.state = "pending"
        new_task.worker_id = None
        new_task.lease_expires_at = None
        new_task.started_at = None
        new_task.completed_at = None
        new_task.result = None
        new_task.error = None
        new_task.created_at = datetime.now()
        if reset_retries:
            new_task.retries = 0
        if queue is not None:
            new_task.queue = queue

        await self.backend.create_task(new_task)
        await self.backend.delete_dead_task(original_id)

        if record.task.kind == "orchestrator":
            execution = await self.backend.get_execution(new_task.execution_id)
            if execution:
                execution.state = "pending"
                execution.started_at = None
                execution.completed_at = None
                execution.result = None
                execution.error = None
                execution.retries = 0
                await self.backend.update_execution(execution)

        return new_task.id

    async def serve(
        self,
        *,
        worker_id: str | None = None,
        queues: List[str] | None = None,
        tags: List[str] | None = None,
        max_concurrency: int = 10,
        lease_duration: timedelta = timedelta(minutes=5),
        heartbeat_interval: timedelta | None = None,
        poll_interval: float = 1.0,
        poll_interval_max: float | None = None,
        poll_backoff_factor: float | None = None,
        cleanup_interval: float | None = 3600.0, # Default 1 hour
        retention_period: timedelta = timedelta(days=7),
        lifecycle: WorkerLifecycle | None = None,
    ):
        if not worker_id:
            worker_id = str(uuid.uuid4())

        lifecycle_handle = lifecycle or self.create_worker_lifecycle(name=worker_id)
        if lifecycle is None:
            self._default_worker_lifecycle = lifecycle_handle
        lifecycle_handle.reset()
        self._active_worker_lifecycles.add(lifecycle_handle)
        worker_tasks: set[asyncio.Task[Any]] = set()

        if heartbeat_interval is None and lease_duration > timedelta(0):
            heartbeat_interval = lease_duration / 2
            min_interval = timedelta(milliseconds=100)
            if heartbeat_interval < min_interval:
                heartbeat_interval = min_interval
        elif heartbeat_interval is not None and heartbeat_interval <= timedelta(0):
            heartbeat_interval = None
            
        worker_poll_min = max(0.001, poll_interval)
        worker_poll_max = poll_interval_max if poll_interval_max is not None else self.poll_max_interval
        worker_poll_max = max(worker_poll_min, worker_poll_max)
        worker_poll_backoff = (
            poll_backoff_factor if poll_backoff_factor is not None and poll_backoff_factor >= 1.0
            else self.poll_backoff_factor
        )
        current_poll_delay = worker_poll_min

        sem = asyncio.Semaphore(max_concurrency)
        logger.info(f"Worker {worker_id} started. Queues: {queues}")
        lifecycle_handle.mark_ready()
        
        cleanup_task = None
        if cleanup_interval is not None:
            cleanup_task = asyncio.create_task(self._cleanup_loop(retention_period, cleanup_interval))

        try:
            while True:
                if lifecycle_handle.stop_event.is_set():
                    lifecycle_handle.mark_draining()
                    if worker_tasks:
                        await asyncio.wait(worker_tasks, return_when=asyncio.FIRST_COMPLETED)
                        continue
                    break

                # Check if we can acquire
                # We don't want to block here, just check if sem is full
                # But asyncio.Semaphore doesn't have a check without acquire. 
                # We acquire first, then claim.
                await sem.acquire()
                if lifecycle_handle.stop_event.is_set():
                    lifecycle_handle.mark_draining()
                    sem.release()
                    continue

                # Collect concurrency limits
                concurrency_limits = {}
                for name, meta in self.registry.items():
                    if meta.max_concurrent is not None:
                        concurrency_limits[name] = meta.max_concurrent

                try:
                    task = await self.backend.claim_next_task(
                        worker_id=worker_id,
                        queues=queues,
                        tags=tags,
                        now=datetime.now(),
                        lease_duration=lease_duration,
                        concurrency_limits=concurrency_limits
                    )
                    
                    if task:
                        # logger.info(f"Worker claimed task {task.step_name} ({task.id})")
                        self.metrics.task_claimed(queue=task.queue, step_name=task.step_name, kind=task.kind)
                        # Run in background
                        bg_task = asyncio.create_task(
                            self._handle_task(task, worker_id, lease_duration, heartbeat_interval, sem)
                        )
                        self.background_tasks.add(bg_task)
                        worker_tasks.add(bg_task)
                        bg_task.add_done_callback(self.background_tasks.discard)
                        bg_task.add_done_callback(worker_tasks.discard)
                        current_poll_delay = worker_poll_min
                    else:
                        sem.release()
                        await _original_sleep(current_poll_delay)
                        current_poll_delay = min(
                            worker_poll_max, current_poll_delay * worker_poll_backoff
                        )
                except Exception as e:
                    sem.release()
                    logger.error(f"Error in worker loop: {e}")
                    await _original_sleep(current_poll_delay)
                    current_poll_delay = min(
                        worker_poll_max, current_poll_delay * worker_poll_backoff
                    )
        except asyncio.CancelledError:
            logger.info("Worker cancelled")
            raise
        finally:
            if cleanup_task:
                cleanup_task.cancel()
                try:
                    await cleanup_task
                except asyncio.CancelledError:
                    pass
            lifecycle_handle.mark_stopped()
            self._active_worker_lifecycles.discard(lifecycle_handle)

    async def shutdown(self):
        """
        Gracefully shuts down the executor, waiting for pending background tasks.
        """
        if self.background_tasks:
            logger.info(f"Waiting for {len(self.background_tasks)} background tasks to complete...")
            await asyncio.gather(*self.background_tasks, return_exceptions=True)

    async def _cleanup_loop(self, retention: timedelta, interval: float):
        logger.info(f"Starting cleanup loop. Retention: {retention}, Interval: {interval}s")
        while True:
            try:
                # Jitter the startup/interval slightly to avoid thundering herd if multiple workers start at once
                # But simple sleep is fine for now
                await _original_sleep(interval)
                
                cutoff = datetime.now() - retention
                count = await self.backend.cleanup_executions(cutoff)
                if count > 0:
                    logger.info(f"Cleaned up {count} old executions (older than {cutoff})")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Cleanup failed: {e}")
                await _original_sleep(60) # Wait a bit before retrying on error

    async def _lease_heartbeat_loop(
        self,
        *,
        task_id: str,
        worker_id: str,
        lease_duration: timedelta,
        interval: timedelta,
        stop_event: asyncio.Event,
    ) -> None:
        await lease_heartbeat_loop(
            executor=self,
            task_id=task_id,
            worker_id=worker_id,
            lease_duration=lease_duration,
            interval=interval,
            stop_event=stop_event,
            logger=logger,
        )

    async def _handle_task(
        self, 
        task: TaskRecord, 
        worker_id: str, 
        lease_duration: timedelta, 
        heartbeat_interval: timedelta | None,
        sem: asyncio.Semaphore
    ):
        # logger.info(f"DEBUG: Starting _handle_task for {task.step_name} ({task.id})")
        token_exec = current_execution_id.set(task.execution_id)
        token_task = current_task_id.set(task.id)
        token_executor = current_executor.set(self)
        token_sem = current_worker_semaphore.set(sem)
        token_permit = current_permit_holder.set(PermitHolder(sem))
        token_worker = current_worker_id.set(worker_id)

        try:
            await run_claimed_task(
                executor=self,
                task=task,
                worker_id=worker_id,
                lease_duration=lease_duration,
                heartbeat_interval=heartbeat_interval,
                expiry_error_type=ExpiryError,
                unregistered_error_factory=UnregisteredFunctionError,
                logger=logger,
            )
        finally:
            sem.release()
            current_execution_id.reset(token_exec)
            current_task_id.reset(token_task)
            current_executor.reset(token_executor)
            current_worker_semaphore.reset(token_sem)
            current_permit_holder.reset(token_permit)
            current_worker_id.reset(token_worker)
            # logger.info(f"DEBUG: Finished _handle_task for {task.step_name} ({task.id})")

# Context var for executor instance
current_executor: ContextVar[Optional[Stent]] = ContextVar("stent_executor", default=None)



Stent.backends = Backends
Stent.notifications = Notifications

# ---------------------------------------------------------------------------
# Durable sleep threshold (seconds).  Any ``asyncio.sleep(n)`` call made
# inside a durable execution where *n* >= this value is automatically
# promoted to a durable sleep that releases the worker.  Sleeps below
# the threshold use the real ``asyncio.sleep``.
# ---------------------------------------------------------------------------
DURABLE_SLEEP_THRESHOLD: float = 1.0


async def sleep(duration: str | dict | timedelta):
    """
    Global sleep helper that uses the current executor context if available,
    otherwise falls back to asyncio.sleep (non-durable).
    """
    executor = current_executor.get()
    if executor:
        await executor.sleep(duration)
    else:
        # Fallback for local testing or non-durable usage
        d = parse_duration(duration)
        await _original_sleep(d.total_seconds())


async def _durable_sleep_wrapper(delay, result=None, *, _real_sleep=_original_sleep):
    """Drop-in replacement for ``asyncio.sleep`` that promotes long sleeps
    to durable sleeps when running inside a Stent execution context."""
    executor: Stent | None = current_executor.get()
    if executor and isinstance(delay, (int, float)) and delay >= DURABLE_SLEEP_THRESHOLD:
        await executor.sleep_instance(delay)
        return result
    return await _real_sleep(delay, result)


# Patch asyncio.sleep so that user code written with the standard library
# automatically benefits from durable sleep inside Stent workers.
asyncio.sleep = _durable_sleep_wrapper  # type: ignore[assignment]
