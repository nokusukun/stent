# Operability & Observability

## Description
Phase 4 focuses on making Stent easier to operate in production. Workers now expose explicit readiness/draining handles, the dead-letter queue stores structured payloads with CLI/API tooling, and we ship first-class observability hooks (structured logging context, optional OpenTelemetry instrumentation, and a pluggable metrics recorder).

## Key Changes
* `stent/executor.py` – added `WorkerLifecycle`, graceful drain handling, DLQ replay APIs, structured logging filter, and metrics callbacks for task lifecycle events.
* `stent/backend/sqlite.py` / `stent/backend/postgres.py` – persist full task payloads in `dead_tasks`, plus new list/get/delete helpers for DLQ management.
* `stent/cli.py` – new `dlq list|show|replay` commands for operators.
* `stent/metrics.py`, `stent/backend/utils.py` – shared helpers for metrics and DLQ serialization.
* `README.md` – documented worker lifecycle coordination, DLQ tooling, structured logging, metrics, and the no-op OpenTelemetry instrumentation behavior.

## Usage/Configuration
```python
executor = Stent(backend=backend, metrics=PromMetrics())
lifecycle = executor.create_worker_lifecycle(name="worker-1")
asyncio.create_task(executor.serve(lifecycle=lifecycle))
await lifecycle.wait_until_ready()

# drain on shutdown
executor.request_worker_drain(lifecycle)
await lifecycle.wait_until_stopped()

# DLQ management
letters = await executor.list_dead_letters(limit=20)
if letters:
    await executor.replay_dead_letter(letters[0].id, queue="retry")
```

CLI helpers:

```bash
stent dlq list
stent dlq show <task_id>
stent dlq replay <task_id> --queue retry
```

Structured logging:

```python
from stent import install_structured_logging
install_structured_logging()  # adds stent_execution_id/task_id to log records
```
