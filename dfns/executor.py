from __future__ import annotations
import asyncio
import functools
import uuid
import logging
from datetime import datetime, timedelta
from typing import Callable, Awaitable, Any, List, Literal, Optional
from contextvars import ContextVar

from dfns.core import (
    Result, RetryPolicy, ExecutionRecord, TaskRecord, ExecutionProgress, 
    ExecutionState, compute_retry_delay
)
from dfns.backend.base import Backend
from dfns.notifications.base import NotificationBackend
from dfns.registry import registry, FunctionMetadata
from dfns.utils.serialization import Serializer, JsonSerializer
from dfns.utils.idempotency import default_idempotency_key
from dfns.utils.time import parse_duration

from dataclasses import dataclass

logger = logging.getLogger(__name__)

class ExpiryError(TimeoutError):
    pass

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

# Backends helper
class Backends:
    @staticmethod
    def SQLiteBackend(path: str) -> Backend:
        from dfns.backend.sqlite import SQLiteBackend
        return SQLiteBackend(path)

    @staticmethod
    def MongoBackend(url: str, db_name: str) -> Backend:
        # Placeholder
        raise NotImplementedError("Mongo backend not implemented yet")

    @staticmethod
    def PostgresBackend(dsn: str) -> Backend:
        from dfns.backend.postgres import PostgresBackend
        return PostgresBackend(dsn)

class Notifications:
    @staticmethod
    def RedisBackend(url: str) -> NotificationBackend:
        from dfns.notifications.redis import RedisBackend
        return RedisBackend(url)

# Capture original sleep before any patching
_original_sleep = asyncio.sleep

current_execution_id: ContextVar[str | None] = ContextVar("dfns_execution_id", default=None)
current_task_id: ContextVar[str | None] = ContextVar("dfns_task_id", default=None)
current_worker_semaphore: ContextVar[asyncio.Semaphore | None] = ContextVar("dfns_worker_semaphore", default=None)
current_permit_holder: ContextVar[PermitHolder | None] = ContextVar("dfns_permit_holder", default=None)

class DFns:
    backends = Backends
    notifications = Notifications
    mute_async_sleep_notifications = False

    def __init__(
        self,
        backend: Backend,
        serializer: Serializer | Literal["json", "pickle"] = "json",
        notification_backend: NotificationBackend | None = None,
    ):
        self.backend = backend
        self.serializer: Serializer

        if isinstance(serializer, str):
            if serializer == "json":
                self.serializer = JsonSerializer()
            else:
                 # Local import to avoid circular dependency if pickle serializer was here
                from dfns.utils.serialization import PickleSerializer
                self.serializer = PickleSerializer()
        else:
            self.serializer = serializer
            
        self.notification_backend = notification_backend
        
        self._register_builtin_tasks()
        self._patch_asyncio_sleep()

    def _register_builtin_tasks(self):
        async def sleep_impl(duration: float):
            pass
            
        meta = FunctionMetadata(
            name="dfns.sleep",
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
        registry.register(meta)

    def _patch_asyncio_sleep(self):
        if getattr(asyncio.sleep, "_is_dfns_patch", False):
            return

        original_sleep = asyncio.sleep
        
        @functools.wraps(original_sleep)
        async def warning_sleep(delay, result=None):
            if current_execution_id.get() and not self.mute_async_sleep_notifications:
                 logger.warning(f"Detected asyncio.sleep({delay}) inside a durable function. Use 'await dfns.sleep(...)' to release worker capacity.")
            return await original_sleep(delay, result)
        
        warning_sleep._is_dfns_patch = True # pyrefly: ignore
        asyncio.sleep = warning_sleep # pyrefly: ignore

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
        # However, 'sleep' is defined AFTER 'DFns' class.
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
        meta = registry.get("dfns.sleep")
        if not meta:
             raise Exception("dfns.sleep not registered")
        
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
            name = registry.name_for_function(fn)
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
            registry.register(meta)

            @functools.wraps(fn)
            async def stub(*args, **kwargs):
                return await cls._call_durable_stub(meta, args, kwargs)

            return stub
        return decorator

    @staticmethod
    async def wrap(fn: Callable[..., Awaitable[Any]], args: tuple, kwargs: dict | None = None):
         # Helper to wrap non-durable functions as activities
         # We need a meta for them if we want to treat them as tasks.
         # For now, let's assume this is called inside a durable function and we treat it as an activity.
         # But wait, registry requires metadata.
         # So wrapping arbitrary functions on the fly is tricky without registering them.
         # This implementation assumes the user uses @durable. 
         # If wrap is used for external funcs, we might need a dynamic registration or special handling.
         # For simplicity, let's assume 'fn' here is already a @durable decorated function OR we create a temporary meta.
         # The example usage: DFns.wrap(send_email, ("...", ...)) suggests send_email might NOT be durable.
         
         # Let's create a dynamic meta if it's not registered.
         if kwargs is None:
             kwargs = {}
             
         name = registry.name_for_function(fn)
         meta = registry.get(name)
         if not meta:
             meta = FunctionMetadata(
                 name=name,
                 fn=fn,
                 cached=False,
                 retry_policy=RetryPolicy(),
                 tags=[],
                 priority=0,
                 queue=None,
                 idempotent=False,
                 idempotency_key_func=None,
                 version=None
             )
         
         return await DFns._call_durable_stub(meta, args, kwargs)

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
        executor = current_executor.get()
        if not executor:
            # Local fallback
            return await asyncio.gather(*[fn(item) for item in iterable])

        name = registry.name_for_function(fn)
        meta = registry.get(name)
        if not meta:
             # Assume wrapper handles dynamic registration if needed, but for batch map we need strict mode or handle it.
             # If fn is already the @durable decorated stub, getting its name via registry helper should work.
             # If it's a raw function, we need to register it on the fly?
             # For simplicity, require @durable.
             # If we call fn(item) it calls _call_durable_stub. 
             # We want to BYPASS _call_durable_stub's immediate scheduling.
             # But accessing 'fn' which is the wrapper is tricky to unwrap to get meta reliably if not in registry.
             # registry.name_for_function handles unwrapping if it's our stub.
             pass
        
        if not meta:
            # Fallback to standard gather if we can't find meta (e.g. dynamic wrapper issues)
            return await asyncio.gather(*[fn(item) for item in iterable])

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
                    completed = await executor._wait_for_task_internal(item.id)
                    if completed.state == "failed":
                        if completed.error:
                            err = executor.serializer.loads(completed.error)
                            if isinstance(err, BaseException):
                                 raise err
                            raise Exception(str(err))
                        raise Exception("Task failed")
                    
                    res = executor.serializer.loads(completed.result)
                    # Store cache/idempotency
                    if item.idempotency_key:
                        if meta.cached:
                            await executor.backend.set_cached_result(item.idempotency_key, completed.result)
                        if meta.idempotent:
                            await executor.backend.set_idempotency_result(item.idempotency_key, completed.result)
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
        Note: If you pass function calls (e.g. `dfns.gather(func(1), func(2))`), 
        they are scheduled immediately when called, not batched by gather.
        Use `dfns.map` for batch scheduling optimization if applicable.
        
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
        name = registry.name_for_function(fn)
        # Check registry
        meta = registry.get(name)
        if not meta:
            # Register on the fly if not decorated? Or fail? 
            # Better to assume it was decorated.
            pass

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
            tags=tags or (meta.tags if meta else []),
            priority=priority,
            queue=queue or (meta.queue if meta else None)
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
            retry_policy=meta.retry_policy if meta else RetryPolicy(),
            scheduled_for=scheduled_for
        )
        
        await self.backend.create_execution(record)
        await self.backend.create_task(task)
        
        return execution_id

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
            while True:
                task = await self.backend.get_task(task_id)
                if task and task.state in ("completed", "failed"):
                    return task
                if expiry and (datetime.now() - start).total_seconds() > expiry:
                        raise ExpiryError(f"Task {task_id} timed out")
                await _original_sleep(0.1)

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

    async def serve(
        self,
        *,
        worker_id: str | None = None,
        queues: List[str] | None = None,
        tags: List[str] | None = None,
        max_concurrency: int = 10,
        lease_duration: timedelta = timedelta(minutes=5),
        poll_interval: float = 1.0,
        cleanup_interval: float | None = 3600.0, # Default 1 hour
        retention_period: timedelta = timedelta(days=7),
    ):
        if not worker_id:
            worker_id = str(uuid.uuid4())
            
        sem = asyncio.Semaphore(max_concurrency)
        logger.info(f"Worker {worker_id} started. Queues: {queues}")
        
        cleanup_task = None
        if cleanup_interval is not None:
            cleanup_task = asyncio.create_task(self._cleanup_loop(retention_period, cleanup_interval))

        try:
            while True:
                # Check if we can acquire
                # We don't want to block here, just check if sem is full
                # But asyncio.Semaphore doesn't have a check without acquire. 
                # We acquire first, then claim.
                await sem.acquire()

                # Collect concurrency limits
                concurrency_limits = {}
                for name, meta in registry._registry.items():
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
                        # Run in background
                        asyncio.create_task(self._handle_task(task, worker_id, lease_duration, sem))
                    else:
                        sem.release()
                        await _original_sleep(poll_interval)
                except Exception as e:
                    sem.release()
                    logger.error(f"Error in worker loop: {e}")
                    await _original_sleep(poll_interval)
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

    async def _handle_task(
        self, 
        task: TaskRecord, 
        worker_id: str, 
        lease_duration: timedelta, 
        sem: asyncio.Semaphore
    ):
        token_exec = current_execution_id.set(task.execution_id)
        token_task = current_task_id.set(task.id)
        token_executor = current_executor.set(self)
        token_sem = current_worker_semaphore.set(sem)
        token_permit = current_permit_holder.set(PermitHolder(sem))
        
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
            
            meta = registry.get(task.step_name)
            if not meta:
                raise Exception(f"Function {task.step_name} not found")
                
            # Update progress
            await self.backend.append_progress(task.execution_id, ExecutionProgress(
                step=task.step_name, status="running", started_at=datetime.now()
            ))

            # Execute
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
                result_val = await meta.fn(*args, **kwargs)
            
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
            sem.release()
            current_execution_id.reset(token_exec)
            current_task_id.reset(token_task)
            current_executor.reset(token_executor)
            current_worker_semaphore.reset(token_sem)
            current_permit_holder.reset(token_permit)

# Context var for executor instance
current_executor: ContextVar[Optional[DFns]] = ContextVar("dfns_executor", default=None)



DFns.backends = Backends
DFns.notifications = Notifications

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
