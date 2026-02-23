# Monitoring & Observability

This guide covers how to monitor Stent workflows using the CLI, structured logging, metrics, and OpenTelemetry integration.

## Command Line Interface (CLI)

Stent includes a CLI for inspecting and managing workflows.

### Configuration

The CLI connects to your database via `--db` flag or `STENT_DB` environment variable:

```bash
# Environment variable
export STENT_DB="postgresql://user:pass@localhost/stent"

# Or pass directly
stent --db stent.sqlite list
```

### Listing Executions

```bash
# List recent executions
stent list

# List more executions
stent list --limit 50

# Filter by state
stent list --state pending
stent list --state running
stent list --state completed
stent list --state failed

# Output:
# ID                                   | State      | Started At
# -----------------------------------------------------------------
# 550e8400-e29b-41d4-a716-446655440000 | completed  | 2024-01-15T10:30:00
# 550e8400-e29b-41d4-a716-446655440001 | running    | 2024-01-15T10:35:00
```

### Showing Execution Details

```bash
stent show 550e8400-e29b-41d4-a716-446655440000

# Output:
# ID: 550e8400-e29b-41d4-a716-446655440000
# State: completed
# Started At: 2024-01-15T10:30:00
# Completed At: 2024-01-15T10:31:30
#
# Progress:
# [10:30:00] > order_workflow (running)
# [10:30:05] + validate_order (completed)
# [10:30:10] + charge_payment (completed)
# [10:30:15] + send_notification (completed)
# [10:31:30] + order_workflow (completed)
#
# Counters:
# - progress: 42
#
# Custom State:
# - phase: completed
# - owner: billing-pipeline
#
# Result: {"order_id": "ORD-123", "status": "completed"}
```

### Quick Stats and Live Watch

```bash
# One-shot execution and queue stats
stent stats

# Live terminal dashboard
stent watch
stent watch --interval 1.0
```

`stats` and `watch` use backend count queries for scalable totals (executions by state, pending tasks, and dead-letter counts).

### Dead Letter Queue Management

```bash
# List dead letters
stent dlq list
stent dlq list --limit 100

# Output:
# Task ID                              | Execution                            | Step            | Reason
# -----------------------------------------------------------------------------------------------------------
# 660e8400-e29b-41d4-a716-446655440000 | 550e8400-e29b-41d4-a716-446655440000 | charge_payment  | ConnectionError: timeout

# Show details
stent dlq show 660e8400-e29b-41d4-a716-446655440000

# Output:
# Task ID: 660e8400-e29b-41d4-a716-446655440000
# Execution ID: 550e8400-e29b-41d4-a716-446655440000
# Step: charge_payment (activity)
# Queue: default
# Reason: ConnectionError: Connection timed out
# Moved At: 2024-01-15T10:30:00
# Retries: 3
# Tags: billing

# Replay a dead letter
stent dlq replay 660e8400-e29b-41d4-a716-446655440000

# Replay to a different queue
stent dlq replay 660e8400-e29b-41d4-a716-446655440000 --queue retry
```

## Structured Logging

Stent automatically injects context into log records.

### Setup

```python
import logging
from stent import install_structured_logging

# Configure logging format to include Stent context
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s "
           "[exec=%(stent_execution_id)s task=%(stent_task_id)s] "
           "%(message)s"
)

# Install the filter
install_structured_logging(logging.getLogger())
```

### Available Context

| Attribute | Description |
|-----------|-------------|
| `stent_execution_id` | Current execution ID |
| `stent_task_id` | Current task ID |
| `stent_worker_id` | Worker processing the task |

### Example Output

```
2024-01-15 10:30:00 INFO [exec=550e8400... task=660e8400...] Starting payment processing
2024-01-15 10:30:01 INFO [exec=550e8400... task=660e8400...] Payment authorized
2024-01-15 10:30:02 INFO [exec=550e8400... task=660e8400...] Payment captured
```

### JSON Logging

For log aggregation systems:

```python
import json
import logging

class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "message": record.getMessage(),
            "execution_id": getattr(record, "stent_execution_id", None),
            "task_id": getattr(record, "stent_task_id", None),
            "worker_id": getattr(record, "stent_worker_id", None),
        })

handler = logging.StreamHandler()
handler.setFormatter(JsonFormatter())
logging.getLogger().addHandler(handler)
```

## Metrics

Stent provides a `MetricsRecorder` protocol for custom metrics implementations.

### Protocol Definition

```python
class MetricsRecorder(Protocol):
    def task_claimed(self, *, queue: str | None, step_name: str, kind: str) -> None:
        """Called when a worker claims a task."""
        ...
    
    def task_completed(
        self,
        *,
        queue: str | None,
        step_name: str,
        kind: str,
        duration_s: float,
    ) -> None:
        """Called when a task completes successfully."""
        ...
    
    def task_failed(
        self,
        *,
        queue: str | None,
        step_name: str,
        kind: str,
        reason: str,
        retrying: bool,
    ) -> None:
        """Called when a task fails."""
        ...
    
    def dead_lettered(
        self,
        *,
        queue: str | None,
        step_name: str,
        kind: str,
        reason: str,
    ) -> None:
        """Called when a task is moved to DLQ."""
        ...
    
    def lease_renewed(self, *, task_id: str, success: bool) -> None:
        """Called when a lease renewal is attempted."""
        ...
```

### Prometheus Example

```python
from prometheus_client import Counter, Histogram, Gauge

# Define metrics
TASKS_CLAIMED = Counter(
    "stent_tasks_claimed_total",
    "Total tasks claimed by workers",
    ["queue", "step", "kind"]
)

TASKS_COMPLETED = Counter(
    "stent_tasks_completed_total",
    "Total tasks completed",
    ["queue", "step", "kind"]
)

TASK_DURATION = Histogram(
    "stent_task_duration_seconds",
    "Task execution duration",
    ["queue", "step"],
    buckets=[0.1, 0.5, 1, 2, 5, 10, 30, 60, 120]
)

TASKS_FAILED = Counter(
    "stent_tasks_failed_total",
    "Total tasks failed",
    ["queue", "step", "retrying"]
)

DEAD_LETTERS = Counter(
    "stent_dead_letters_total",
    "Tasks moved to dead letter queue",
    ["queue", "step"]
)

class PrometheusMetrics:
    def task_claimed(self, *, queue, step_name, kind):
        TASKS_CLAIMED.labels(
            queue=queue or "default",
            step=step_name,
            kind=kind
        ).inc()
    
    def task_completed(self, *, queue, step_name, kind, duration_s):
        TASKS_COMPLETED.labels(
            queue=queue or "default",
            step=step_name,
            kind=kind
        ).inc()
        TASK_DURATION.labels(
            queue=queue or "default",
            step=step_name
        ).observe(duration_s)
    
    def task_failed(self, *, queue, step_name, kind, reason, retrying):
        TASKS_FAILED.labels(
            queue=queue or "default",
            step=step_name,
            retrying=str(retrying)
        ).inc()
    
    def dead_lettered(self, *, queue, step_name, kind, reason):
        DEAD_LETTERS.labels(
            queue=queue or "default",
            step=step_name
        ).inc()
    
    def lease_renewed(self, *, task_id, success):
        pass  # Optional

# Use with executor
executor = Stent(backend=backend, metrics=PrometheusMetrics())
```

### StatsD Example

```python
import statsd

class StatsDMetrics:
    def __init__(self, host="localhost", port=8125, prefix="stent"):
        self.client = statsd.StatsClient(host, port, prefix=prefix)
    
    def task_claimed(self, *, queue, step_name, kind):
        self.client.incr(f"tasks.claimed.{queue or 'default'}.{step_name}")
    
    def task_completed(self, *, queue, step_name, kind, duration_s):
        self.client.incr(f"tasks.completed.{queue or 'default'}.{step_name}")
        self.client.timing(f"tasks.duration.{queue or 'default'}.{step_name}", duration_s * 1000)
    
    def task_failed(self, *, queue, step_name, kind, reason, retrying):
        metric = "retrying" if retrying else "failed"
        self.client.incr(f"tasks.{metric}.{queue or 'default'}.{step_name}")
    
    def dead_lettered(self, *, queue, step_name, kind, reason):
        self.client.incr(f"dlq.{queue or 'default'}.{step_name}")
    
    def lease_renewed(self, *, task_id, success):
        if success:
            self.client.incr("leases.renewed")
        else:
            self.client.incr("leases.failed")
```

## OpenTelemetry Integration

Stent supports distributed tracing via OpenTelemetry.

### Setup

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

from stent import Stent
from stent.telemetry import instrument

# Configure OpenTelemetry
provider = TracerProvider()
processor = BatchSpanProcessor(OTLPSpanExporter(endpoint="http://localhost:4317"))
provider.add_span_processor(processor)
trace.set_tracer_provider(provider)

# Instrument Stent
success = instrument()
if success:
    print("Stent instrumented with OpenTelemetry")
else:
    print("OpenTelemetry not installed, skipping instrumentation")

# Create executor as normal
executor = Stent(backend=backend)
```

### Spans Created

| Span Name | Kind | Attributes |
|-----------|------|------------|
| `stent.dispatch {function}` | PRODUCER | `stent.function`, `stent.execution_id` |
| `stent.execute {step}` | CONSUMER | `stent.task_id`, `stent.execution_id`, `stent.step`, `stent.worker_id` |

### Trace Propagation

Spans are automatically linked:

```
stent.dispatch order_workflow
    └── stent.execute order_workflow
            ├── stent.execute validate_order
            ├── stent.execute charge_payment
            └── stent.execute send_notification
```

### Jaeger Example

```python
from opentelemetry.exporter.jaeger.thrift import JaegerExporter

exporter = JaegerExporter(
    agent_host_name="localhost",
    agent_port=6831,
)
provider.add_span_processor(BatchSpanProcessor(exporter))
```

## Programmatic Monitoring

### Queue Depth

```python
# Total pending tasks
total = await executor.queue_depth()

# Per-queue depth
default_depth = await executor.queue_depth(queue="default")
critical_depth = await executor.queue_depth(queue="critical")
```

### Running Activities

```python
running = await executor.get_running_activities()

for task in running:
    print(f"Task {task.id}: {task.step_name}")
    print(f"  Worker: {task.worker_id}")
    print(f"  Started: {task.started_at}")
    print(f"  Lease expires: {task.lease_expires_at}")
```

### Execution Progress

```python
state = await executor.state_of(exec_id)

print(f"Execution: {state.id}")
print(f"State: {state.state}")
print(f"Progress: {state.progress_str}")

for progress in state.progress:
    print(f"  {progress.step}: {progress.status}")
    if progress.started_at:
        print(f"    Started: {progress.started_at}")
    if progress.completed_at:
        print(f"    Completed: {progress.completed_at}")
    if progress.detail:
        print(f"    Detail: {progress.detail}")
```

### Worker Status

```python
status = executor.worker_status_overview()

print(f"Ready: {status['ready']}")
print(f"Draining: {status['draining']}")
for worker in status['workers']:
    print(f"  {worker['name']}: {worker['state']}")
```

## Alerting Strategies

### Queue Growth

```python
async def monitor_queue_growth():
    while True:
        depth = await executor.queue_depth()
        if depth > THRESHOLD:
            await send_alert(f"Queue depth critical: {depth}")
        await asyncio.sleep(60)
```

### DLQ Growth

```python
async def monitor_dlq():
    while True:
        dead_letters = await executor.list_dead_letters(limit=1)
        if len(dead_letters) > DLQ_THRESHOLD:
            await send_alert(f"DLQ has {len(dead_letters)} items")
        await asyncio.sleep(300)
```

### Failed Executions

```python
async def monitor_failures():
    while True:
        failed = await executor.list_executions(limit=100, state="failed")
        if len(failed) > FAILURE_THRESHOLD:
            await send_alert(f"{len(failed)} recent failures")
        await asyncio.sleep(60)
```

## Dashboard Recommendations

### Key Metrics to Display

1. **Throughput**: Tasks completed per minute
2. **Latency**: Task duration percentiles (p50, p95, p99)
3. **Error Rate**: Failed tasks / total tasks
4. **Queue Depth**: Pending tasks over time
5. **DLQ Size**: Dead-lettered tasks
6. **Worker Health**: Ready/draining workers

### Grafana Dashboard Example

```json
{
  "panels": [
    {
      "title": "Task Throughput",
      "type": "graph",
      "targets": [
        {"expr": "rate(stent_tasks_completed_total[5m])"}
      ]
    },
    {
      "title": "Task Duration (p95)",
      "type": "graph",
      "targets": [
        {"expr": "histogram_quantile(0.95, rate(stent_task_duration_seconds_bucket[5m]))"}
      ]
    },
    {
      "title": "Error Rate",
      "type": "graph",
      "targets": [
        {"expr": "rate(stent_tasks_failed_total[5m]) / rate(stent_tasks_completed_total[5m])"}
      ]
    }
  ]
}
```
