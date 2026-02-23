from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any, Callable, Sequence

from stent.core import DeadLetterRecord, ExecutionProgress, ExecutionRecord, SignalRecord, TaskRecord, RetryPolicy

PlaceholderFn = Callable[[int], str]


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
    from stent.utils.serialization import get_exception_class
    
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


def retry_policy_to_json(policy: RetryPolicy | None) -> str:
    if not policy:
        return "{}"
    return json.dumps(
        {
            "max_attempts": policy.max_attempts,
            "backoff_factor": policy.backoff_factor,
            "initial_delay": policy.initial_delay,
            "max_delay": policy.max_delay,
            "jitter": policy.jitter,
        }
    )


def retry_policy_from_json(value: str | None) -> RetryPolicy:
    data = json.loads(value) if value else {}
    return RetryPolicy(
        max_attempts=data.get("max_attempts", 3),
        backoff_factor=data.get("backoff_factor", 2.0),
        initial_delay=data.get("initial_delay", 1.0),
        max_delay=data.get("max_delay", 60.0),
        jitter=data.get("jitter", 0.1),
    )


def execution_row_values(record: ExecutionRecord) -> tuple[Any, ...]:
    return (
        record.id,
        record.root_function,
        record.state,
        record.args,
        record.kwargs,
        record.result,
        record.error,
        record.retries,
        record.created_at,
        record.started_at,
        record.completed_at,
        record.expiry_at,
        json.dumps(record.tags),
        record.priority,
        record.queue,
    )


def task_row_values(task: TaskRecord) -> tuple[Any, ...]:
    return (
        task.id,
        task.execution_id,
        task.step_name,
        task.kind,
        task.parent_task_id,
        task.state,
        task.args,
        task.kwargs,
        task.result,
        task.error,
        task.retries,
        task.created_at,
        task.started_at,
        task.completed_at,
        task.worker_id,
        task.lease_expires_at,
        json.dumps(task.tags),
        task.priority,
        task.queue,
        task.idempotency_key,
        retry_policy_to_json(task.retry_policy),
        task.scheduled_for,
    )


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return value


def _loads_tags(value: Any) -> list[str]:
    if isinstance(value, str):
        return list(json.loads(value))
    return list(value or [])


def row_to_progress(row: Any) -> ExecutionProgress:
    return ExecutionProgress(
        step=row["step"],
        status=row["status"],
        started_at=_as_datetime(row["started_at"]),
        completed_at=_as_datetime(row["completed_at"]),
        detail=row["detail"],
    )


def row_to_execution(row: Any, progress: list[ExecutionProgress]) -> ExecutionRecord:
    return ExecutionRecord(
        id=row["id"],
        root_function=row["root_function"],
        state=row["state"],
        args=row["args"],
        kwargs=row["kwargs"],
        result=row["result"],
        error=row["error"],
        retries=row["retries"],
        created_at=_as_datetime(row["created_at"]) or datetime.now(),
        started_at=_as_datetime(row["started_at"]),
        completed_at=_as_datetime(row["completed_at"]),
        expiry_at=_as_datetime(row["expiry_at"]),
        progress=progress,
        tags=_loads_tags(row["tags"]),
        priority=row["priority"],
        queue=row["queue"],
    )


def row_to_task(row: Any) -> TaskRecord:
    return TaskRecord(
        id=row["id"],
        execution_id=row["execution_id"],
        step_name=row["step_name"],
        kind=row["kind"],
        parent_task_id=row["parent_task_id"],
        state=row["state"],
        args=row["args"],
        kwargs=row["kwargs"],
        result=row["result"],
        error=row["error"],
        retries=row["retries"],
        created_at=_as_datetime(row["created_at"]) or datetime.now(),
        started_at=_as_datetime(row["started_at"]),
        completed_at=_as_datetime(row["completed_at"]),
        worker_id=row["worker_id"],
        lease_expires_at=_as_datetime(row["lease_expires_at"]),
        tags=_loads_tags(row["tags"]),
        priority=row["priority"],
        queue=row["queue"],
        idempotency_key=row["idempotency_key"],
        retry_policy=retry_policy_from_json(row["retry_policy"]),
        scheduled_for=_as_datetime(row["scheduled_for"]),
    )


def row_to_signal(row: Any) -> SignalRecord:
    return SignalRecord(
        execution_id=row["execution_id"],
        name=row["name"],
        payload=row["payload"],
        created_at=_as_datetime(row["created_at"]) or datetime.now(),
        consumed=bool(row["consumed"]),
        consumed_at=_as_datetime(row["consumed_at"]),
    )


def row_to_dead_letter(row: Any) -> DeadLetterRecord:
    return DeadLetterRecord(
        task=task_record_from_json(row["data"]),
        reason=row["reason"],
        moved_at=_as_datetime(row["moved_at"]) or datetime.now(),
    )


def qmark_placeholder(_: int) -> str:
    return "?"


def dollar_placeholder(index: int) -> str:
    return f"${index}"


def build_filtered_query(
    *,
    base_select: str,
    filters: Sequence[tuple[str, Any | None]],
    placeholder: PlaceholderFn,
) -> tuple[str, list[Any]]:
    params: list[Any] = []
    conditions: list[str] = []
    for column, value in filters:
        if value is None:
            continue
        params.append(value)
        conditions.append(f"{column} = {placeholder(len(params))}")

    query = base_select
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    return query, params


def build_filtered_count_query(
    *,
    table: str,
    filters: Sequence[tuple[str, Any | None]],
    placeholder: PlaceholderFn,
) -> tuple[str, list[Any]]:
    return build_filtered_query(
        base_select=f"SELECT COUNT(*) FROM {table}",
        filters=filters,
        placeholder=placeholder,
    )


def build_filtered_list_query(
    *,
    table: str,
    filters: Sequence[tuple[str, Any | None]],
    order_by: str,
    limit: int,
    offset: int,
    placeholder: PlaceholderFn,
) -> tuple[str, list[Any]]:
    query, params = build_filtered_query(
        base_select=f"SELECT * FROM {table}",
        filters=filters,
        placeholder=placeholder,
    )
    params.append(limit)
    limit_token = placeholder(len(params))
    params.append(offset)
    offset_token = placeholder(len(params))
    query += f" ORDER BY {order_by} LIMIT {limit_token} OFFSET {offset_token}"
    return query, params
