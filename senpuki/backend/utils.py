from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any

from senpuki.core import TaskRecord, RetryPolicy


def _encode_bytes(value: bytes | None) -> str | None:
    if value is None:
        return None
    return base64.b64encode(value).decode("ascii")


def _decode_bytes(value: str | None) -> bytes | None:
    if value is None:
        return None
    return base64.b64decode(value.encode("ascii"))


def _datetime_to_str(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _datetime_from_str(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _retry_policy_to_dict(policy: RetryPolicy | None) -> dict[str, Any] | None:
    if not policy:
        return None
    return {
        "max_attempts": policy.max_attempts,
        "backoff_factor": policy.backoff_factor,
        "initial_delay": policy.initial_delay,
        "max_delay": policy.max_delay,
        "jitter": policy.jitter,
        "retry_for": [exc.__name__ for exc in policy.retry_for],
    }


def _retry_policy_from_dict(data: dict[str, Any] | None) -> RetryPolicy | None:
    if not data:
        return None
    
    # Import here to avoid circular imports
    from senpuki.utils.serialization import get_exception_class
    
    # Deserialize retry_for exception classes
    retry_for_names = data.get("retry_for", ["Exception"])
    retry_for_classes: list[type[BaseException]] = []
    for name in retry_for_names:
        exc_class = get_exception_class(name)
        if exc_class not in retry_for_classes:
            retry_for_classes.append(exc_class)
    
    if not retry_for_classes:
        retry_for_classes = [Exception]
    
    return RetryPolicy(
        max_attempts=data.get("max_attempts", 3),
        backoff_factor=data.get("backoff_factor", 2.0),
        initial_delay=data.get("initial_delay", 1.0),
        max_delay=data.get("max_delay", 60.0),
        jitter=data.get("jitter", 0.1),
        retry_for=tuple(retry_for_classes),
    )


def task_record_to_json(task: TaskRecord) -> str:
    payload = {
        "id": task.id,
        "execution_id": task.execution_id,
        "step_name": task.step_name,
        "kind": task.kind,
        "parent_task_id": task.parent_task_id,
        "state": task.state,
        "args": _encode_bytes(task.args),
        "kwargs": _encode_bytes(task.kwargs),
        "result": _encode_bytes(task.result),
        "error": _encode_bytes(task.error),
        "retries": task.retries,
        "created_at": _datetime_to_str(task.created_at),
        "started_at": _datetime_to_str(task.started_at),
        "completed_at": _datetime_to_str(task.completed_at),
        "worker_id": task.worker_id,
        "lease_expires_at": _datetime_to_str(task.lease_expires_at),
        "tags": task.tags,
        "priority": task.priority,
        "queue": task.queue,
        "retry_policy": _retry_policy_to_dict(task.retry_policy),
        "idempotency_key": task.idempotency_key,
        "scheduled_for": _datetime_to_str(task.scheduled_for),
    }
    return json.dumps(payload)


def task_record_from_json(payload: str) -> TaskRecord:
    data = json.loads(payload)
    return TaskRecord(
        id=data["id"],
        execution_id=data["execution_id"],
        step_name=data["step_name"],
        kind=data["kind"],
        parent_task_id=data.get("parent_task_id"),
        state=data["state"],
        args=_decode_bytes(data.get("args")) or b"",
        kwargs=_decode_bytes(data.get("kwargs")) or b"",
        retries=data["retries"],
        created_at=_datetime_from_str(data.get("created_at")) or datetime.now(),
        tags=list(data.get("tags") or []),
        priority=data.get("priority", 0),
        queue=data.get("queue"),
        retry_policy=_retry_policy_from_dict(data.get("retry_policy")),
        result=_decode_bytes(data.get("result")),
        error=_decode_bytes(data.get("error")),
        started_at=_datetime_from_str(data.get("started_at")),
        completed_at=_datetime_from_str(data.get("completed_at")),
        worker_id=data.get("worker_id"),
        lease_expires_at=_datetime_from_str(data.get("lease_expires_at")),
        idempotency_key=data.get("idempotency_key"),
        scheduled_for=_datetime_from_str(data.get("scheduled_for")),
    )
