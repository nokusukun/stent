from __future__ import annotations
import asyncio
import contextlib
import functools
import uuid
import logging
from datetime import datetime, timedelta
from typing import Callable, Awaitable, Any, List, Literal, Optional
from contextvars import ContextVar

from senpuki.core import (
    Result, RetryPolicy, ExecutionRecord, TaskRecord, ExecutionProgress, 
    ExecutionState, compute_retry_delay, SignalRecord, DeadLetterRecord
)
from senpuki.backend.base import Backend
from senpuki.notifications.base import NotificationBackend
from senpuki.registry import registry, FunctionMetadata, FunctionRegistry
from senpuki.utils.serialization import Serializer, JsonSerializer
from senpuki.utils.idempotency import default_idempotency_key
from senpuki.utils.time import parse_duration
from senpuki.metrics import MetricsRecorder, NoOpMetricsRecorder

from dataclasses import dataclass, field, replace

logger = logging.getLogger(__name__)

class SenpukiLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.senpuki_execution_id = current_execution_id.get()
        record.senpuki_task_id = current_task_id.get()
        record.senpuki_worker_id = current_worker_id.get()
        return True

def install_structured_logging(target: logging.Logger | None = None) -> None:
    """
    Adds a logging.Filter that injects Senpuki context (execution/task id) into
    every log record. Integrate it with your formatter via
    %(senpuki_execution_id)s etc.
    """
    logger_obj = target or logging.getLogger("senpuki")
    for existing in getattr(logger_obj, "filters", []):
        if isinstance(existing, SenpukiLogFilter):
            return
    logger_obj.addFilter(SenpukiLogFilter())

install_structured_logging(logger)

class ExpiryError(TimeoutError):
    pass


class UnregisteredFunctionError(RuntimeError):
    def __init__(self, function_name: str):
        message = (
            f"No durable registration found for '{function_name}'. "
            "Decorate the function with @Senpuki.durable() or register it on the "
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
        from senpuki.backend.sqlite import SQLiteBackend
        return SQLiteBackend(path)

    @staticmethod
    def MongoBackend(url: str, db_name: str) -> Backend:
        # Placeholder
        raise NotImplementedError("Mongo backend not implemented yet")

    @staticmethod
    def PostgresBackend(dsn: str) -> Backend:
        from senpuki.backend.postgres import PostgresBackend
        return PostgresBackend(dsn)

class Notifications:
    @staticmethod
    def RedisBackend(url: str) -> NotificationBackend:
        from senpuki.notifications.redis import RedisBackend
        return RedisBackend(url)

# Capture original sleep before any patching
_original_sleep = asyncio.sleep

current_execution_id: ContextVar[str | None] = ContextVar("senpuki_execution_id", default=None)
current_task_id: ContextVar[str | None] = ContextVar("senpuki_task_id", default=None)
current_worker_semaphore: ContextVar[asyncio.Semaphore | None] = ContextVar("senpuki_worker_semaphore", default=None)
current_permit_holder: ContextVar[PermitHolder | None] = ContextVar("senpuki_permit_holder", default=None)
current_worker_id: ContextVar[str | None] = ContextVar("senpuki_worker_id", default=None)

class Senpuki:
    backends = Backends
    notifications = Notifications
    default_registry: FunctionRegistry = registry

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
                from senpuki.utils.serialization import PickleSerializer
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
        if self.registry.get("senpuki.sleep"):
            return

        async def sleep_impl(duration: float):
            pass
            
        meta = FunctionMetadata(
            name="senpuki.sleep",
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
        # However, 'sleep' is defined AFTER 'Senpuki' class.
        # We can use 'current_executor' directly here.
        executor = current_executor.get()
        if executor:
            await executor.sleep_instance(duration)
        else:
            d = parse_duration(duration)
            await _original_sleep(d.total_seconds())

    async def sleep_instance(self, duration: str | dict | timedelta):
        """
        Sleep for a specific duration. 
        This releases the worker to process other tasks while waiting.
        """
        d = parse_duration(duration)
        meta = self.registry.get("senpuki.sleep")
        if not meta:
            self._register_builtin_tasks()
            meta = self.registry.get("senpuki.sleep")
        if not meta:
            raise RuntimeError("senpuki.sleep not registered")
        
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

            @functools.wraps(fn)
            async def stub(*args, **kwargs):
                return await cls._call_durable_stub(meta, args, kwargs)

            return stub
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
        """
        Efficiently schedules and waits for a function to be applied to each item in the iterable.
        Equivalent to `await asyncio.gather(*[fn(item) for item in iterable])` but with batch scheduling optimization.
        """
        executor: Optional[Senpuki] = current_executor.get()
        if not executor:
            return await asyncio.gather(*[fn(item) for item in iterable])

        reg = executor.registry
        name = reg.name_for_function(fn)
        meta = reg.get(name)
        if not meta:
            raise UnregisteredFunctionError(name)

        # Prepare tasks
        tasks_to_create: List[TaskRecord] = []
        # We also need to track cache/idempotency hits to avoid scheduling them
        results_or_tasks: List[Any | TaskRecord] = [] 
        
        exec_id = current_execution_id.get() or ""
        parent_id = current_task_id.get() or ""

        # Pre-calculate common fields
        args_list = []
        for item in iterable:
             # Support multiple args if iterable yields tuples? 
             # Standard map(func, iterable) takes one arg per item from iterable.
             # If user wants multi-args, they use starmap or pass tuple.
             # We'll assume single arg per item for simplicity matching python map.
             args = (item,)
             kwargs = {}
             args_list.append((args, kwargs))

        # Check cache/idempotency
        # TODO: Batch read cache/idempotency would be better. For now loop.
        
        for i, (args, kwargs) in enumerate(args_list):
            key = None
            hit = False
            
            if meta.idempotent or meta.cached:
                if meta.idempotency_key_func:
                    key = meta.idempotency_key_func(*args, **kwargs)
                else:
                    key = default_idempotency_key(meta.name, meta.version, args, kwargs, serializer=executor.serializer)

                if key:
                     if meta.cached:
                         cached_val = await executor.backend.get_cached_result(key)
                         if cached_val is not None:
                             if exec_id:
                                 await executor.backend.append_progress(exec_id, ExecutionProgress(
                                     step=meta.name, status="cache_hit"
                                 ))
                             results_or_tasks.append(executor.serializer.loads(cached_val))
                             hit = True
                     
                     if not hit and meta.idempotent:
                         stored_val = await executor.backend.get_idempotency_result(key)
                         if stored_val is not None:
                             if exec_id:
                                 await executor.backend.append_progress(exec_id, ExecutionProgress(
                                     step=meta.name, status="cache_hit"
                                 ))
                             results_or_tasks.append(executor.serializer.loads(stored_val))
                             hit = True

            if not hit:
                # Create TaskRecord
                task_id = str(uuid.uuid4())
                task = TaskRecord(
                    id=task_id,
                    execution_id=exec_id,
                    step_name=meta.name,
                    kind="activity",
                    parent_task_id=parent_id,
                    state="pending",
                    args=executor.serializer.dumps(args),
                    kwargs=executor.serializer.dumps(kwargs),
                    retries=0,
                    created_at=datetime.now(),
                    tags=meta.tags,
                    priority=meta.priority,
                    queue=meta.queue,
                    retry_policy=meta.retry_policy,
                    idempotency_key=key,
                    scheduled_for=None
                )
                tasks_to_create.append(task)
                results_or_tasks.append(task)

        # Batch create tasks
        if tasks_to_create:
            await executor.backend.create_tasks(tasks_to_create)
            if exec_id:
                # Batch log progress? We only have append_progress (single). 
                # Doing N updates is okay-ish or we can add batch progress later.
                # For now, just do it.
                for t in tasks_to_create:
                    await executor.backend.append_progress(exec_id, ExecutionProgress(
                        step=meta.name, status="dispatched"
                    ))
        
        # Optimized wait for all
        # We release the permit ONCE for the whole batch
        permit = current_permit_holder.get()
        if permit:
            permit.release()

        try:
            async def waiter(item):
                if isinstance(item, TaskRecord):
                    # Use internal wait that DOES NOT touch semaphore
                    completed = await executor._wait_for_task_internal(item.id) # pyrefly: ignore[missing-attribute]
                    if completed.state == "failed":
                        if completed.error:
                            err = executor.serializer.loads(completed.error) #  pyrefly: ignore[missing-attribute]
                            if isinstance(err, BaseException):
                                 raise err
                            raise Exception(str(err))
                        raise Exception("Task failed")
                    
                    res = executor.serializer.loads(completed.result) if completed.result is not None else None #  pyrefly: ignore[missing-attribute]
                    # Store cache/idempotency
                    if item.idempotency_key and completed.result is not None:
                        if meta.cached: #  pyrefly: ignore[missing-attribute]
                            await executor.backend.set_cached_result(item.idempotency_key, completed.result) #  pyrefly: ignore[missing-attribute]
                        if meta.idempotent: #  pyrefly: ignore[missing-attribute]
                            await executor.backend.set_idempotency_result(item.idempotency_key, completed.result) #  pyrefly: ignore[missing-attribute]
                    return res
                else:
                    return item

            return await asyncio.gather(*[waiter(item) for item in results_or_tasks])
        finally:
            if permit:
                await permit.acquire()

    @classmethod
    async def gather(cls, *tasks, **kwargs):
        """
        Alias for asyncio.gather. 
        Note: If you pass function calls (e.g. `senpuki.gather(func(1), func(2))`), 
        they are scheduled immediately when called, not batched by gather.
        Use `senpuki.map` for batch scheduling optimization if applicable.
        
        Supports `return_exceptions=True`.
        """
        return await asyncio.gather(*tasks, **kwargs)

    @classmethod
    async def _call_durable_stub(cls, meta: FunctionMetadata, args: tuple, kwargs: dict):
        executor = current_executor.get() # Get the current executor if any
        exec_id = current_execution_id.get()
        
        # If no executor in context, it's a local call/unit test.
        if not executor:
            logger.debug(f"STUB: {meta.name} running locally (no executor context)")
            return await meta.fn(*args, **kwargs)

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
                if stored_val:
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

        if expiry and max_duration:
            raise ValueError("Cannot provide both 'expiry' and 'max_duration'. Use 'max_duration' as 'expiry' is deprecated.")

        if max_duration:
            expiry = max_duration

        if isinstance(expiry, str):
            expiry = parse_duration(expiry)
            
        scheduled_for = None
        if delay:
            if isinstance(delay, (str, dict)):
                delay = parse_duration(delay)
            scheduled_for = datetime.now() + delay
        
        expiry_at = (datetime.now() + expiry) if expiry else None
        if scheduled_for and expiry:
             # expiry should start AFTER scheduled start? 
             # For now simple logic: expiry is absolute or relative to dispatch?
             # Standard: relative to dispatch usually, but if delayed, maybe relative to start.
             # Let's keep it simple: expiry_at is absolute. If delay > expiry, it times out immediately.
             # User should set expiry appropriately.
             expiry_at = scheduled_for + expiry

        execution_id = str(uuid.uuid4())
        
        record = ExecutionRecord(
            id=execution_id,
            root_function=name,
            state="pending",
            args=self.serializer.dumps(args),
            kwargs=self.serializer.dumps(kwargs),
            retries=0,
            created_at=datetime.now(),
            started_at=None,
            completed_at=None,
            expiry_at=expiry_at,
            progress=[],
            tags=tags or meta.tags,
            priority=priority,
            queue=queue or meta.queue
        )
        
        task = TaskRecord(
            id=str(uuid.uuid4()),
            execution_id=execution_id,
            step_name=name,
            kind="orchestrator",
            parent_task_id=None,
            state="pending",
            args=record.args,
            kwargs=record.kwargs,
            retries=0,
            created_at=datetime.now(),
            tags=record.tags,
            priority=priority,
            queue=record.queue,
            retry_policy=meta.retry_policy,
            scheduled_for=scheduled_for
        )
        
        create_with_root = getattr(self.backend, "create_execution_with_root_task", None)
        if create_with_root is not None and callable(create_with_root):
            await create_with_root(record, task)  # type: ignore[misc]
        else:
            await self.backend.create_execution(record)
            await self.backend.create_task(task)
        
        return execution_id

    async def send_signal(self, execution_id: str, name: str, payload: Any) -> None:
        """
        Send a signal to a running execution. 
        If the execution is waiting for this signal, it will be resumed.
        If not, the signal will be buffered.
        """
        # 1. Store signal
        payload_bytes = self.serializer.dumps(payload)
        signal = SignalRecord(
            execution_id=execution_id,
            name=name,
            payload=payload_bytes,
            created_at=datetime.now(),
            consumed=False
        )
        await self.backend.create_signal(signal)
        
        # 2. Wake up pending task if any
        # We use a deterministic task ID for signals: uuid5(exec_id + name)
        # But to be safe, let's just query the tasks or construct the ID if we define the generation rule.
        # Let's check if the specific waiter task exists.
        
        # To avoid dependency on uuid namespacing details, let's just search.
        # Optimization: We can search for tasks with step_name=f"signal:{name}"
        # But backend doesn't support search by step_name.
        # So we iterate.
        tasks = await self.backend.list_tasks_for_execution(execution_id)
        target_step = f"signal:{name}"
        
        for t in tasks:
            if t.step_name == target_step and t.kind == "signal" and t.state == "pending":
                # Mark as completed
                t.state = "completed"
                t.result = payload_bytes
                t.completed_at = datetime.now()
                await self.backend.update_task(t)
                
                # Notify
                if self.notification_backend:
                     await self.notification_backend.notify_task_completed(t.id)
                break

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
             
        step_name = f"signal:{name}"
        
        # Generate deterministic ID for the signal task to ensure idempotency on replay
        # We use a constructed UUID based on exec_id and signal name
        # exec_id is a UUID string.
        try:
             exec_uuid = uuid.UUID(exec_id)
        except ValueError:
             # Fallback if exec_id is not uuid (e.g. testing)
             exec_uuid = uuid.uuid4() 
             
        task_id = str(uuid.uuid5(exec_uuid, step_name))
        
        # 1. Check if we already have this task (replay or already running)
        existing_task = await self.backend.get_task(task_id)
        if existing_task:
            if existing_task.state == "completed":
                return self.serializer.loads(existing_task.result) if existing_task.result is not None else None
            else:
                # Still pending, wait for it
                completed = await self._wait_for_task(task_id)
                return self.serializer.loads(completed.result) if completed.result is not None else None
                
        # 2. Check signal buffer
        signal = await self.backend.get_signal(exec_id, name)
        if signal and not signal.consumed:
            # Consumed from buffer immediately
            signal.consumed = True
            signal.consumed_at = datetime.now()
            await self.backend.create_signal(signal) # upsert
            
            # Create completed task
            task = TaskRecord(
                 id=task_id,
                 execution_id=exec_id,
                 step_name=step_name,
                 kind="signal",
                 parent_task_id=current_task_id.get(),
                 state="completed",
                 args=b"",
                 kwargs=b"",
                 retries=0,
                 created_at=datetime.now(),
                 completed_at=datetime.now(),
                 tags=["signal"],
                 priority=0,
                 queue=None,
                 retry_policy=None,
                 result=signal.payload
            )
            await self.backend.create_task(task)
            await self.backend.append_progress(exec_id, ExecutionProgress(
                step=step_name, status="completed", detail="Signal consumed from buffer"
            ))
            return self.serializer.loads(signal.payload)
            
        # 3. Create pending task and wait
        task = TaskRecord(
             id=task_id,
             execution_id=exec_id,
             step_name=step_name,
             kind="signal",
             parent_task_id=current_task_id.get(),
             state="pending",
             args=b"",
             kwargs=b"",
             retries=0,
             created_at=datetime.now(),
             tags=["signal"],
             priority=0,
             queue=None,
             retry_policy=None
        )
        await self.backend.create_task(task)
        await self.backend.append_progress(exec_id, ExecutionProgress(
            step=step_name, status="dispatched", detail="Waiting for signal"
        ))
        
        # Wait
        completed = await self._wait_for_task(task_id)
        
        # Mark signal consumed if it exists now
        s = await self.backend.get_signal(exec_id, name)
        if s and not s.consumed:
             s.consumed = True
             s.consumed_at = datetime.now()
             await self.backend.create_signal(s)
             
        return self.serializer.loads(completed.result) if completed.result is not None else None

    async def _schedule_activity(
        self, 
        meta: FunctionMetadata, 

        args: tuple, 
        kwargs: dict, 
        idempotency_key: str | None,
        delay: timedelta | None = None
    ) -> TaskRecord:
        exec_id = current_execution_id.get() or ""
        parent_id = current_task_id.get() or ""
        
        scheduled_for = None
        if delay:
            scheduled_for = datetime.now() + delay

        task_id = str(uuid.uuid4())
        task = TaskRecord(
            id=task_id,
            execution_id=exec_id,
            step_name=meta.name,
            kind="activity",
            parent_task_id=parent_id,
            state="pending",
            args=self.serializer.dumps(args),
            kwargs=self.serializer.dumps(kwargs),
            retries=0,
            created_at=datetime.now(),
            tags=meta.tags,
            priority=meta.priority,
            queue=meta.queue,
            retry_policy=meta.retry_policy,
            idempotency_key=idempotency_key, # Store the key received from _call_durable_stub
            scheduled_for=scheduled_for
        )
        
        await self.backend.create_task(task)
        await self.backend.append_progress(exec_id, ExecutionProgress(
            step=meta.name, status="dispatched", detail=f"Scheduled for {delay}" if delay else None
        ))
        
        completed_task = await self._wait_for_task(task_id)
        
        return completed_task # Return completed_task, _call_durable_stub handles errors and result processing

    async def _wait_for_task_internal(self, task_id: str, expiry: float | None = None) -> TaskRecord:
        """Waits for task completion without modifying semaphore."""
        if self.notification_backend:
            # Subscribe and wait
            it = self.notification_backend.subscribe_to_task(task_id, expiry=expiry)
            async for _ in it:
                pass 
            # After loop (completion or expiry), fetch latest
            task = await self.backend.get_task(task_id)
            if not task:
                raise ValueError(f"Task not found after notification: {task_id}")
            return task
        else:
            # Poll
            start = datetime.now()
            delay = self.poll_min_interval
            while True:
                task = await self.backend.get_task(task_id)
                if task and task.state in ("completed", "failed"):
                    return task
                if expiry and (datetime.now() - start).total_seconds() > expiry:
                        raise ExpiryError(f"Task {task_id} timed out")
                await _original_sleep(delay)
                delay = min(self.poll_max_interval, delay * self.poll_backoff_factor)

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
            queue=record.queue
        )

    async def result_of(self, execution_id: str) -> Result[Any, Any]:
        record = await self.backend.get_execution(execution_id)
        if not record:
            raise ValueError("Execution not found")
        if record.state not in ("completed", "failed", "timed_out"):
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

        if self.notification_backend:
            it = self.notification_backend.subscribe_to_execution(execution_id, expiry=expiry)
            try:
                async for _ in it:
                    pass
            except asyncio.TimeoutError:
                raise ExpiryError(f"Timed out waiting for execution {execution_id}")
        else:
            # Polling fallback
            start = datetime.now()
            while True:
                state = await self.state_of(execution_id)
                if state.state in ("completed", "failed", "timed_out", "cancelled"):
                    break
                
                if expiry and (datetime.now() - start).total_seconds() > expiry:
                    raise ExpiryError(f"Timed out waiting for execution {execution_id}")
                
                await asyncio.sleep(0.5)

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
        timeout = interval.total_seconds()
        while True:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=timeout)
                return
            except asyncio.TimeoutError:
                try:
                    renewed = await self.backend.renew_task_lease(task_id, worker_id, lease_duration)
                except Exception:
                    logger.exception("Lease renewal failed for task %s", task_id)
                    self.metrics.lease_renewed(task_id=task_id, success=False)
                    return

                self.metrics.lease_renewed(task_id=task_id, success=renewed)
                if not renewed:
                    logger.warning(
                        "Lease renewal lost for task %s on worker %s; allowing reclaim",
                        task_id,
                        worker_id,
                    )
                    return
            except asyncio.CancelledError:
                return

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
        
        execution = None
        heartbeat_task: asyncio.Task | None = None
        heartbeat_stop: asyncio.Event | None = None
        try:
            # Check execution state or expiry
            execution = await self.backend.get_execution(task.execution_id)
            if not execution:
                raise ValueError(f"Execution {task.execution_id} not found")
            
            if execution.expiry_at and datetime.now() > execution.expiry_at:
                execution.state = "timed_out"
                execution.completed_at = datetime.now()
                task.state = "failed"
                task.error = self.serializer.dumps(Exception("Execution timed out"))
                task.completed_at = datetime.now()
                await self.backend.update_execution(execution)
                await self.backend.update_task(task)
                if self.notification_backend:
                    await self.notification_backend.notify_task_updated(task.id, "failed")
                    await self.notification_backend.notify_execution_updated(task.execution_id, "timed_out")
                await self.backend.append_progress(task.execution_id, ExecutionProgress(
                     step=task.step_name, status="failed", detail="Execution timed out"
                ))
                return # Exit early

            if execution.state in ("cancelling", "cancelled"):
                 task.state = "failed"
                 task.error = self.serializer.dumps(Exception("Execution cancelled"))
                 task.completed_at = datetime.now()
                 await self.backend.update_task(task)
                 if self.notification_backend:
                     await self.notification_backend.notify_task_updated(task.id, "failed")
                     # Ensure execution notification if state changed to cancelled? 
                     # State is already cancelling/cancelled, but maybe we want to signal task failed
                 await self.backend.append_progress(task.execution_id, ExecutionProgress(
                     step=task.step_name, status="failed", detail="Execution cancelled"
                 ))
                 return

            if task.kind == "orchestrator":
                 # Update execution state to running if needed
                 if execution.state == "pending":
                     execution.state = "running"
                     execution.started_at = datetime.now()
                     await self.backend.update_execution(execution)
                     if self.notification_backend:
                         await self.notification_backend.notify_execution_updated(task.execution_id, "running")

            args = self.serializer.loads(task.args)
            kwargs = self.serializer.loads(task.kwargs)
            
            meta = self.registry.get(task.step_name)
            if not meta:
                raise UnregisteredFunctionError(task.step_name)
                
            # Update progress
            await self.backend.append_progress(task.execution_id, ExecutionProgress(
                step=task.step_name, status="running", started_at=datetime.now()
            ))

            if heartbeat_interval:
                heartbeat_stop = asyncio.Event()
                heartbeat_task = asyncio.create_task(
                    self._lease_heartbeat_loop(
                        task_id=task.id,
                        worker_id=worker_id,
                        lease_duration=lease_duration,
                        interval=heartbeat_interval,
                        stop_event=heartbeat_stop,
                    )
                )

            # Execute
            # logger.info(f"Executing {task.step_name} with expiry {execution.expiry_at}")
            if execution.expiry_at:
                remaining = (execution.expiry_at - datetime.now()).total_seconds()
                if remaining <= 0:
                     # Already timed out
                     raise ExpiryError("Execution timed out before start")
                try:
                    async with asyncio.timeout(remaining):
                        result_val = await meta.fn(*args, **kwargs)
                except asyncio.TimeoutError:
                    # Mark execution as timed out
                    execution.state = "timed_out"
                    execution.completed_at = datetime.now()
                    await self.backend.update_execution(execution)
                    
                    task.state = "failed"
                    task.error = self.serializer.dumps(Exception("Execution timed out"))
                    task.completed_at = datetime.now()
                    await self.backend.update_task(task)
                    
                    if self.notification_backend:
                        await self.notification_backend.notify_task_updated(task.id, "failed")
                        await self.notification_backend.notify_execution_updated(task.execution_id, "timed_out")
                    await self.backend.append_progress(task.execution_id, ExecutionProgress(
                         step=task.step_name, status="failed", detail="Execution timed out"
                    ))
                    return
            else:
                # logger.info(f"Calling function {task.step_name}")
                result_val = await meta.fn(*args, **kwargs)
                # logger.info(f"Function {task.step_name} returned {result_val}")
            
            # Success
            task.result = self.serializer.dumps(result_val)
            task.state = "completed"
            task.completed_at = datetime.now()
            await self.backend.update_task(task)
            
            await self.backend.append_progress(task.execution_id, ExecutionProgress(
                step=task.step_name, status="completed", started_at=task.started_at, completed_at=datetime.now()
            ))
            
            if task.kind == "orchestrator":
                execution.result = task.result
                execution.state = "completed"
                execution.completed_at = datetime.now()
                await self.backend.update_execution(execution)
                if self.notification_backend:
                    await self.notification_backend.notify_execution_updated(task.execution_id, "completed")

            if self.notification_backend:
                await self.notification_backend.notify_task_completed(task.id)

            # Caching logic for orchestrator tasks
            if task.kind == "orchestrator" and meta.cached:
                 key = default_idempotency_key(meta.name, meta.version, args, kwargs, serializer=self.serializer)
                 await self.backend.set_cached_result(key, task.result)
                 logger.debug(f"Cached result for orchestrator {meta.name} with key {key}")

            duration_s = 0.0
            if task.started_at and task.completed_at:
                duration_s = (task.completed_at - task.started_at).total_seconds()
            self.metrics.task_completed(
                queue=task.queue,
                step_name=task.step_name,
                kind=task.kind,
                duration_s=duration_s,
            )

        except Exception as e:
            # Retry logic
            retry_policy = task.retry_policy or RetryPolicy()
            attempt = task.retries + 1
            
            is_retryable = any(isinstance(e, t) for t in retry_policy.retry_for)
            # Simplified check for retryable
            
            if is_retryable and attempt < retry_policy.max_attempts:
                delay = compute_retry_delay(retry_policy, attempt)
                task.retries = attempt
                task.state = "pending"
                task.lease_expires_at = datetime.now() + timedelta(seconds=delay)
                task.worker_id = None # release back to pool
                task.started_at = None
                await self.backend.update_task(task)
                await self.backend.append_progress(task.execution_id, ExecutionProgress(
                     step=task.step_name, status="failed", detail=str(e)
                ))
                self.metrics.task_failed(
                    queue=task.queue,
                    step_name=task.step_name,
                    kind=task.kind,
                    reason=str(e),
                    retrying=True,
                )
            else:
                # Fatal failure
                task.state = "failed"
                task.error = self.serializer.dumps(e)
                task.completed_at = datetime.now()
                await self.backend.update_task(task)
                await self.backend.move_task_to_dead_letter(task, str(e))
                await self.backend.append_progress(task.execution_id, ExecutionProgress(
                     step=task.step_name, status="failed", detail=str(e)
                ))
                self.metrics.task_failed(
                    queue=task.queue,
                    step_name=task.step_name,
                    kind=task.kind,
                    reason=str(e),
                    retrying=False,
                )
                self.metrics.dead_lettered(
                    queue=task.queue,
                    step_name=task.step_name,
                    kind=task.kind,
                    reason=str(e),
                )
                if not execution: # pyrefly: ignore
                    raise ValueError(f"Execution {task.execution_id} not found")

                if task.kind == "orchestrator":
                    execution.state = "failed"
                    execution.error = task.error
                    execution.completed_at = datetime.now()
                    await self.backend.update_execution(execution)
                    if self.notification_backend:
                        await self.notification_backend.notify_execution_updated(task.execution_id, "failed")

                if self.notification_backend:
                    await self.notification_backend.notify_task_updated(task.id, "failed")

        finally:
            if heartbeat_stop:
                heartbeat_stop.set()
            if heartbeat_task:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
            sem.release()
            current_execution_id.reset(token_exec)
            current_task_id.reset(token_task)
            current_executor.reset(token_executor)
            current_worker_semaphore.reset(token_sem)
            current_permit_holder.reset(token_permit)
            current_worker_id.reset(token_worker)
            # logger.info(f"DEBUG: Finished _handle_task for {task.step_name} ({task.id})")

# Context var for executor instance
current_executor: ContextVar[Optional[Senpuki]] = ContextVar("senpuki_executor", default=None)



Senpuki.backends = Backends
Senpuki.notifications = Notifications

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
        await asyncio.sleep(d.total_seconds())
