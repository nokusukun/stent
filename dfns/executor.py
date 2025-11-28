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

logger = logging.getLogger(__name__)

current_execution_id: ContextVar[str | None] = ContextVar("dfns_execution_id", default=None)
current_task_id: ContextVar[str | None] = ContextVar("dfns_task_id", default=None)
current_worker_semaphore: ContextVar[asyncio.Semaphore | None] = ContextVar("dfns_worker_semaphore", default=None)

class DFns:
    def __init__(
        self,
        backend: Backend,
        serializer: Serializer | Literal["json", "pickle"] = "json",
        notification_backend: NotificationBackend | None = None,
    ):
        self.backend = backend
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
            
        res = executor.serializer.loads(completed_task.result)
        
        # Store cache/idempotency if enabled and key was generated after successful execution
        if key:
            if meta.cached:
                await executor.backend.set_cached_result(key, completed_task.result)
                logger.debug(f"Stored cached result for {meta.name} with key {key}")
            if meta.idempotent:
                await executor.backend.set_idempotency_result(key, completed_task.result)
                logger.debug(f"Stored idempotency result for {meta.name} with key {key}")
            
        return res

    async def dispatch(
        self,
        fn: Callable[..., Awaitable[Any]],
        *args,
        timeout: str | timedelta | None = None,
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

        if isinstance(timeout, str):
            timeout = parse_duration(timeout)
        
        timeout_at = datetime.now() + timeout if timeout else None
        
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
            timeout_at=timeout_at,
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
            retry_policy=meta.retry_policy if meta else RetryPolicy()
        )
        
        await self.backend.create_execution(record)
        await self.backend.create_task(task)
        
        return execution_id

    async def _schedule_activity(self, meta: FunctionMetadata, args: tuple, kwargs: dict, idempotency_key: str | None) -> TaskRecord:
        exec_id = current_execution_id.get()
        parent_id = current_task_id.get()
        
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
            idempotency_key=idempotency_key # Store the key received from _call_durable_stub
        )
        
        await self.backend.create_task(task)
        await self.backend.append_progress(exec_id, ExecutionProgress(
            step=meta.name, status="dispatched"
        ))
        
        completed_task = await self._wait_for_task(task_id)
        
        return completed_task # Return completed_task, _call_durable_stub handles errors and result processing

    async def _wait_for_task(self, task_id: str, timeout: float | None = None) -> TaskRecord:
        # Release semaphore to allow other tasks to run while we wait (prevent deadlock in low concurrency)
        sem = current_worker_semaphore.get()
        if sem:
            sem.release()
            
        try:
            if self.notification_backend:
                # Subscribe and wait
                it = self.notification_backend.subscribe_to_task(task_id, timeout=timeout)
                async for _ in it:
                    pass 
                # After loop (completion or timeout), fetch latest
                task = await self.backend.get_task(task_id)
                return task
            else:
                # Poll
                start = datetime.now()
                while True:
                    task = await self.backend.get_task(task_id)
                    if task and task.state in ("completed", "failed"):
                        return task
                    if timeout and (datetime.now() - start).total_seconds() > timeout:
                         raise TimeoutError(f"Task {task_id} timed out")
                    await asyncio.sleep(0.1)
        finally:
            if sem:
                await sem.acquire()

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

    async def wait_for(self, execution_id: str, timeout: float | None = None) -> Result[Any, Any]:
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
            it = self.notification_backend.subscribe_to_execution(execution_id, timeout=timeout)
            try:
                async for _ in it:
                    pass
            except asyncio.TimeoutError:
                raise TimeoutError(f"Timed out waiting for execution {execution_id}")
        else:
            # Polling fallback
            start = datetime.now()
            while True:
                state = await self.state_of(execution_id)
                if state.state in ("completed", "failed", "timed_out", "cancelled"):
                    break
                
                if timeout and (datetime.now() - start).total_seconds() > timeout:
                    raise TimeoutError(f"Timed out waiting for execution {execution_id}")
                
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
    ):
        if not worker_id:
            worker_id = str(uuid.uuid4())
            
        sem = asyncio.Semaphore(max_concurrency)
        logger.info(f"Worker {worker_id} started. Queues: {queues}")
        
        while True:
            # Check if we can acquire
            # We don't want to block here, just check if sem is full
            # But asyncio.Semaphore doesn't have a check without acquire. 
            # We acquire first, then claim.
            await sem.acquire()

            try:
                task = await self.backend.claim_next_task(
                    worker_id=worker_id,
                    queues=queues,
                    tags=tags,
                    now=datetime.now()
                )
                
                if task:
                    # Run in background
                    asyncio.create_task(self._handle_task(task, worker_id, lease_duration, sem))
                else:
                    sem.release()
                    await asyncio.sleep(poll_interval)
            except Exception as e:
                sem.release()
                logger.error(f"Error in worker loop: {e}")
                await asyncio.sleep(poll_interval)

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
        
        try:
            # Check execution state or timeout
            execution = await self.backend.get_execution(task.execution_id)
            if not execution:
                raise ValueError(f"Execution {task.execution_id} not found")
            
            if execution.timeout_at and datetime.now() > execution.timeout_at:
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
            if execution.timeout_at:
                remaining = (execution.timeout_at - datetime.now()).total_seconds()
                if remaining <= 0:
                     # Already timed out
                     raise TimeoutError("Execution timed out before start")
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

# Context var for executor instance
current_executor: ContextVar[Optional[DFns]] = ContextVar("dfns_executor", default=None)

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

class Notifications:
    @staticmethod
    def RedisBackend(url: str) -> NotificationBackend:
        from dfns.notifications.redis import RedisBackend
        return RedisBackend(url)

DFns.backends = Backends
DFns.notifications = Notifications
