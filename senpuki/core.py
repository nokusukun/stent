from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Generic, TypeVar, Any, Literal, Optional, List
import random

T = TypeVar("T")
E = TypeVar("E")

@dataclass
class Result(Generic[T, E]):
    ok: bool
    value: T | None
    error: E | None

    @classmethod
    def Ok(cls, value: T) -> "Result[T, E]":
        return cls(ok=True, value=value, error=None)

    @classmethod
    def Error(cls, error: E) -> "Result[T, E]":
        return cls(ok=False, value=None, error=error)

    def or_raise(self) -> T:
        if not self.ok:
            if isinstance(self.error, BaseException):
                raise self.error
            raise Exception(self.error)
        return self.value  # type: ignore

@dataclass
class RetryPolicy:
    max_attempts: int = 3
    backoff_factor: float = 2.0
    initial_delay: float = 1.0
    max_delay: float = 60.0
    jitter: float = 0.1
    retry_for: tuple[type[BaseException], ...] = (Exception,)

def compute_retry_delay(policy: RetryPolicy, attempt: int, *, rng: random.Random = random.Random()) -> float:
    # attempt is 1-based (1 = first retry)
    # wait = initial * (factor ^ (attempt - 1))
    delay = policy.initial_delay * (policy.backoff_factor ** (attempt - 1))
    delay = min(delay, policy.max_delay)
    
    if policy.jitter > 0:
        # randomize between delay * (1 - jitter) and delay * (1 + jitter) ?? 
        # Or usually just +/- jitter or 0 to jitter. 
        # Prompt says "fraction of delay to randomize (0-1)".
        # Let's assume standard implementation: delay +/- (delay * jitter)
        jitter_amount = delay * policy.jitter
        delay = delay + rng.uniform(-jitter_amount, jitter_amount)
    
    return max(0.0, delay)

@dataclass
class ExecutionProgress:
    step: str
    status: Literal["dispatched", "running", "completed", "failed", "cache_hit"]
    started_at: datetime | None = None
    completed_at: datetime | None = None
    detail: str | None = None

@dataclass
class ExecutionRecord:
    id: str
    root_function: str
    state: Literal["pending", "running", "completed", "failed", "timed_out", "cancelling", "cancelled"]
    args: bytes
    kwargs: bytes
    retries: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    expiry_at: datetime | None
    progress: List[ExecutionProgress]
    tags: List[str]
    priority: int
    queue: str | None
    result: bytes | None = None
    error: bytes | None = None

@dataclass
class TaskRecord:
    id: str
    execution_id: str
    step_name: str
    kind: Literal["orchestrator", "activity", "signal"]
    parent_task_id: str | None
    state: Literal["pending", "running", "completed", "failed"]
    args: bytes
    kwargs: bytes
    retries: int
    created_at: datetime
    tags: List[str]
    priority: int
    queue: str | None
    retry_policy: RetryPolicy | None
    result: bytes | None = None
    error: bytes | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    worker_id: str | None = None
    lease_expires_at: datetime | None = None
    idempotency_key: str | None = None
    scheduled_for: datetime | None = None

@dataclass
class SignalRecord:
    execution_id: str
    name: str
    payload: bytes
    created_at: datetime
    consumed: bool = False
    consumed_at: datetime | None = None

@dataclass
class DeadLetterRecord:
    task: TaskRecord
    reason: str
    moved_at: datetime

    @property
    def id(self) -> str:
        return self.task.id

    @property
    def execution_id(self) -> str:
        return self.task.execution_id

@dataclass
class ExecutionState:
    id: str
    state: str
    result: Result[Any, Any] | None
    started_at: datetime | None
    completed_at: datetime | None
    retries: int
    progress: List[ExecutionProgress]
    tags: List[str]
    priority: int
    queue: str | None
    counters: dict[str, int | float] = field(default_factory=dict)
    custom_state: dict[str, Any] = field(default_factory=dict)

    @property
    def progress_str(self) -> str:
        # Simple implementation
        parts: List[str] = []
        for p in self.progress:
            if not p.completed_at:
                continue
            duration = ""
            if p.completed_at and p.started_at:
                diff = (p.completed_at - p.started_at).total_seconds()
                duration = f"({diff:.1f}s)"
            elif p.started_at:
                 duration = "(...)"
            parts.append(f"{p.step}{duration}")
        return f"{self.progress[0].step if len(self.progress) else '(begin)'} > " + " > ".join(parts)
