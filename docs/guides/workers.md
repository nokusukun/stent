# Worker Configuration

This guide covers how to configure and deploy Stent workers for optimal performance and reliability.

## Starting a Worker

Workers are started with `executor.serve()`:

```python
import asyncio
from stent import Stent

async def main():
    backend = Stent.backends.SQLiteBackend("workflow.db")
    await backend.init_db()
    
    executor = Stent(backend=backend)
    
    # Start worker (runs indefinitely)
    await executor.serve()

asyncio.run(main())
```

## Worker Configuration Options

### Basic Options

```python
await executor.serve(
    worker_id="worker-1",           # Unique identifier for this worker
    queues=["default", "high"],     # Only process these queues
    tags=["billing"],               # Only process tasks with these tags
    max_concurrency=50,             # Max concurrent tasks
)
```

### Polling Configuration

```python
await executor.serve(
    poll_interval=0.5,              # Min polling interval (seconds)
    poll_interval_max=5.0,          # Max polling interval (backoff)
    poll_backoff_factor=2.0,        # Backoff multiplier
)
```

When no tasks are available, polling interval increases exponentially:
- First poll: 0.5s
- Second poll: 1.0s
- Third poll: 2.0s
- ... capped at 5.0s

When a task is claimed, interval resets to minimum.

### Lease Configuration

```python
from datetime import timedelta

await executor.serve(
    lease_duration=timedelta(minutes=5),     # Task lock duration
    heartbeat_interval=timedelta(minutes=2), # Lease renewal frequency
)
```

**Lease behavior:**
- Worker claims task, sets `lease_expires_at` to now + 5 minutes
- Every 2 minutes, worker renews lease
- If worker crashes, lease expires and another worker can claim

### Cleanup Configuration

```python
await executor.serve(
    cleanup_interval=3600.0,                 # Run cleanup every hour
    retention_period=timedelta(days=7),      # Delete data older than 7 days
)
```

**Cleanup behavior:**
- Runs periodically on one worker
- Removes completed executions older than retention period
- Helps manage database size

## Complete Configuration Example

```python
from datetime import timedelta
from stent import Stent, WorkerLifecycle

async def run_production_worker():
    backend = Stent.backends.PostgresBackend(
        "postgresql://user:pass@localhost/stent"
    )
    await backend.init_db()
    
    executor = Stent(
        backend=backend,
        notification_backend=Stent.notifications.RedisBackend(
            "redis://localhost:6379"
        ),
        poll_min_interval=0.1,
        poll_max_interval=3.0,
        poll_backoff_factor=1.5,
    )
    
    lifecycle = executor.create_worker_lifecycle(name="worker-1")
    
    await executor.serve(
        worker_id="worker-1",
        queues=["default", "high_priority"],
        tags=["billing", "notifications"],
        max_concurrency=50,
        poll_interval=0.5,
        poll_interval_max=5.0,
        poll_backoff_factor=2.0,
        lease_duration=timedelta(minutes=10),
        heartbeat_interval=timedelta(minutes=4),
        cleanup_interval=7200.0,  # 2 hours
        retention_period=timedelta(days=30),
        lifecycle=lifecycle,
    )
```

## Queues and Tags

### Queue-Based Routing

Assign tasks to specific queues and route workers:

```python
# Function definitions
@Stent.durable(queue="critical")
async def critical_operation():
    ...

@Stent.durable(queue="background")
async def background_job():
    ...

@Stent.durable  # Default queue
async def normal_task():
    ...
```

```python
# Dedicated critical worker
await executor.serve(
    worker_id="critical-worker",
    queues=["critical"],
    max_concurrency=10,
)

# Background worker
await executor.serve(
    worker_id="background-worker",
    queues=["background"],
    max_concurrency=100,
)

# General worker (all queues)
await executor.serve(
    worker_id="general-worker",
    # No queues filter = process all queues
    max_concurrency=50,
)
```

### Tag-Based Routing

Use tags for cross-cutting concerns:

```python
# Functions with tags
@Stent.durable(tags=["billing"])
async def charge_customer():
    ...

@Stent.durable(tags=["billing", "notifications"])
async def send_invoice():
    ...

@Stent.durable(tags=["notifications"])
async def send_alert():
    ...
```

```python
# Billing worker (billing-tagged + untagged tasks)
await executor.serve(
    worker_id="billing-worker",
    tags=["billing"],
)

# Notification worker
await executor.serve(
    worker_id="notification-worker",
    tags=["notifications"],
)
```

## Worker Lifecycle

### Readiness and Draining

Use `WorkerLifecycle` for graceful startup/shutdown:

```python
async def managed_worker():
    executor = Stent(backend=backend)
    
    # Create lifecycle handle
    lifecycle = executor.create_worker_lifecycle(name="worker-1")
    
    # Start worker
    worker_task = asyncio.create_task(
        executor.serve(lifecycle=lifecycle)
    )
    
    # Wait for worker to be ready (useful for health checks)
    await lifecycle.wait_until_ready()
    print("Worker is ready to accept tasks")
    
    # ... run for a while ...
    
    # Initiate graceful shutdown
    executor.request_worker_drain(lifecycle)
    print("Draining - finishing in-flight tasks")
    
    # Wait for completion
    await lifecycle.wait_until_stopped()
    print("Worker stopped cleanly")
```

### Health Checks

Check worker status for health endpoints:

```python
@app.get("/health")
async def health_check():
    status = executor.worker_status_overview()
    
    if not status["ready"]:
        return JSONResponse(
            {"status": "not_ready", "workers": status["workers"]},
            status_code=503
        )
    
    if status["draining"]:
        return JSONResponse(
            {"status": "draining", "workers": status["workers"]},
            status_code=503
        )
    
    return {"status": "healthy", "workers": status["workers"]}
```

### Kubernetes Integration

```python
async def kubernetes_worker():
    executor = Stent(backend=backend)
    lifecycle = executor.create_worker_lifecycle(name=os.environ["HOSTNAME"])
    
    # Start worker
    worker_task = asyncio.create_task(
        executor.serve(lifecycle=lifecycle)
    )
    
    # Wait for readiness (readiness probe can check lifecycle.state)
    await lifecycle.wait_until_ready()
    
    # Handle SIGTERM for graceful shutdown
    def handle_sigterm():
        executor.request_worker_drain(lifecycle)
    
    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGTERM, handle_sigterm)
    
    # Wait for worker to complete
    await worker_task
```

## Scaling Workers

### Horizontal Scaling

Run multiple worker processes/containers:

```bash
# Start multiple workers
python worker.py --worker-id=worker-1 &
python worker.py --worker-id=worker-2 &
python worker.py --worker-id=worker-3 &
```

Each worker:
- Needs a unique `worker_id`
- Shares the same database backend
- Automatically coordinates task claiming

### Vertical Scaling

Increase `max_concurrency` for more parallelism per worker:

```python
# Low concurrency for CPU-intensive tasks
await executor.serve(max_concurrency=4)

# High concurrency for I/O-bound tasks
await executor.serve(max_concurrency=100)
```

### Queue-Based Scaling

Scale different queues independently:

```python
# 1 critical worker with low concurrency
await executor.serve(
    queues=["critical"],
    max_concurrency=5,
)

# 10 background workers with high concurrency
for i in range(10):
    await executor.serve(
        worker_id=f"background-{i}",
        queues=["background"],
        max_concurrency=50,
    )
```

## Concurrency Tuning

### max_concurrency

Controls concurrent tasks per worker:

```python
# For CPU-bound tasks
await executor.serve(max_concurrency=4)  # Match CPU cores

# For I/O-bound tasks
await executor.serve(max_concurrency=100)  # Many concurrent I/O

# For memory-intensive tasks
await executor.serve(max_concurrency=2)  # Prevent OOM
```

### Function-Level Rate Limiting

Use `max_concurrent` for per-function limits:

```python
@Stent.durable(max_concurrent=5)
async def rate_limited_api():
    # Max 5 concurrent calls ACROSS ALL WORKERS
    ...
```

This is checked at task claim time - workers won't claim tasks that would exceed the limit.

## Lease Configuration

### lease_duration

How long a worker "owns" a task:

```python
# Short lease for quick tasks
await executor.serve(lease_duration=timedelta(minutes=2))

# Long lease for slow tasks
await executor.serve(lease_duration=timedelta(hours=1))
```

**Guidelines:**
- Too short: Tasks get claimed by other workers mid-execution
- Too long: Crashed tasks take long to recover

### heartbeat_interval

How often to renew the lease:

```python
await executor.serve(
    lease_duration=timedelta(minutes=10),
    heartbeat_interval=timedelta(minutes=4),  # Renew at 40% of lease
)
```

**Rule of thumb:** heartbeat_interval = lease_duration / 2 to 3

## Monitoring Workers

### Logging

Configure structured logging:

```python
import logging
from stent import install_structured_logging

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(stent_execution_id)s] %(message)s"
)

install_structured_logging(logging.getLogger())
```

### Metrics

Implement custom metrics:

```python
class PrometheusMetrics:
    def task_claimed(self, *, queue, step_name, kind):
        TASKS_CLAIMED.labels(queue=queue, step=step_name, kind=kind).inc()
    
    def task_completed(self, *, queue, step_name, kind, duration_s):
        TASKS_COMPLETED.labels(queue=queue, step=step_name).inc()
        TASK_DURATION.labels(queue=queue, step=step_name).observe(duration_s)
    
    def task_failed(self, *, queue, step_name, kind, reason, retrying):
        TASKS_FAILED.labels(queue=queue, step=step_name, retrying=retrying).inc()
    
    def dead_lettered(self, *, queue, step_name, kind, reason):
        DEAD_LETTERS.labels(queue=queue, step=step_name).inc()
    
    def lease_renewed(self, *, task_id, success):
        if success:
            LEASE_RENEWALS.inc()
        else:
            LEASE_FAILURES.inc()

executor = Stent(backend=backend, metrics=PrometheusMetrics())
```

### Queue Depth Monitoring

```python
async def monitor_queues():
    while True:
        depth = await executor.queue_depth()
        print(f"Total pending: {depth}")
        
        for queue in ["default", "critical", "background"]:
            depth = await executor.queue_depth(queue=queue)
            print(f"Queue {queue}: {depth} pending")
        
        await asyncio.sleep(60)
```

## Best Practices

1. **Use unique worker IDs** - Helps with debugging and metrics

2. **Configure appropriate lease duration** - Match your longest expected task

3. **Use Redis notifications in production** - Reduces latency and database load

4. **Implement graceful shutdown** - Use `WorkerLifecycle` for clean termination

5. **Monitor queue depth** - Alert when queues grow too large

6. **Scale based on workload** - More workers for higher throughput

7. **Separate queues for different workloads** - Prevents head-of-line blocking

8. **Test worker failure scenarios** - Ensure tasks recover after crashes
