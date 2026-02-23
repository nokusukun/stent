# Notifications API Reference

Stent supports optional notification backends for low-latency task completion notifications. Without a notification backend, Stent uses polling to detect task completion.

## Why Use Notifications?

By default, when an orchestrator waits for a child task:

1. Orchestrator polls the database periodically
2. Polling interval backs off exponentially (0.1s to 5s)
3. Latency can be up to `poll_max_interval` seconds

With Redis notifications:

1. Task completion publishes a message
2. Orchestrator receives notification immediately
3. Latency is typically milliseconds

**Recommendation**: Use Redis notifications in production for best performance.

## NotificationBackend Protocol

```python
from typing import Protocol, AsyncIterator, Dict, Any

class NotificationBackend(Protocol):
    async def notify_task_completed(self, task_id: str) -> None: ...
    async def notify_task_updated(self, task_id: str, state: str) -> None: ...
    
    def subscribe_to_task(
        self,
        task_id: str,
        *,
        expiry: float | None = None,
    ) -> AsyncIterator[Dict[str, Any]]: ...
    
    async def notify_execution_updated(self, execution_id: str, state: str) -> None: ...
    
    def subscribe_to_execution(
        self,
        execution_id: str,
        *,
        expiry: float | None = None,
    ) -> AsyncIterator[Dict[str, Any]]: ...
```

---

## Redis Backend

The Redis backend uses Redis Pub/Sub for real-time notifications.

### Installation

Redis support is included with Stent:

```bash
pip install stent
# redis package is included in dependencies
```

### Creation

```python
from stent import Stent

notification_backend = Stent.notifications.RedisBackend("redis://localhost:6379")

executor = Stent(
    backend=Stent.backends.PostgresBackend(dsn),
    notification_backend=notification_backend
)
```

### Connection String Format

```
redis://[[username:]password@]host[:port][/database]
```

Examples:

```python
# Local Redis
Stent.notifications.RedisBackend("redis://localhost:6379")

# With authentication
Stent.notifications.RedisBackend("redis://:password@localhost:6379")

# With database number
Stent.notifications.RedisBackend("redis://localhost:6379/1")

# Redis Cluster (connection string)
Stent.notifications.RedisBackend("redis://node1:6379,node2:6379,node3:6379")

# Redis with SSL
Stent.notifications.RedisBackend("rediss://localhost:6379")
```

### How It Works

1. **Task Completion**:
   ```
   Worker completes task
       |
       v
   notify_task_completed(task_id)
       |
       v
   PUBLISH stent:task:{task_id} {"state": "completed"}
   ```

2. **Orchestrator Waiting**:
   ```
   Orchestrator awaits child task
       |
       v
   subscribe_to_task(task_id)
       |
       v
   SUBSCRIBE stent:task:{task_id}
       |
       v
   Receives message, continues execution
   ```

### Channels

The Redis backend uses the following Pub/Sub channels:

| Channel Pattern | Purpose |
|----------------|---------|
| `stent:task:{task_id}` | Task state updates |
| `stent:execution:{execution_id}` | Execution state updates |

### Message Format

```json
{
    "task_id": "uuid-string",
    "state": "completed"  // or "failed"
}
```

```json
{
    "execution_id": "uuid-string", 
    "state": "completed"  // or "failed", "timed_out", "cancelled"
}
```

---

## Usage Examples

### Basic Setup

```python
import asyncio
from stent import Stent

async def main():
    backend = Stent.backends.PostgresBackend(
        "postgresql://user:pass@localhost/stent"
    )
    await backend.init_db()
    
    executor = Stent(
        backend=backend,
        notification_backend=Stent.notifications.RedisBackend(
            "redis://localhost:6379"
        )
    )
    
    # Now task completions trigger immediate notifications
    worker = asyncio.create_task(executor.serve())
    
    exec_id = await executor.dispatch(my_workflow)
    
    # wait_for uses notifications for instant updates
    result = await executor.wait_for(exec_id)
```

### Without Notifications (Polling Fallback)

```python
# When notification_backend is None, Stent uses adaptive polling
executor = Stent(
    backend=backend,
    notification_backend=None,  # Default
    poll_min_interval=0.1,      # Start polling at 100ms
    poll_max_interval=5.0,      # Max 5 seconds between polls
    poll_backoff_factor=2.0,    # Double interval each poll
)
```

### Hybrid Approach

You can run without Redis and add it later:

```python
# Development: no Redis needed
executor = Stent(backend=sqlite_backend)

# Production: add Redis for performance
executor = Stent(
    backend=postgres_backend,
    notification_backend=redis_backend
)
```

---

## Configuration Best Practices

### Redis Configuration

For production, configure Redis appropriately:

```
# redis.conf

# Disable persistence if using only for notifications
save ""
appendonly no

# Set memory limit
maxmemory 256mb
maxmemory-policy allkeys-lru

# Tune for pub/sub workload
tcp-keepalive 60
timeout 0
```

### High Availability

For production, consider Redis Sentinel or Cluster:

```python
# Using Redis Sentinel
notification_backend = Stent.notifications.RedisBackend(
    "redis://sentinel-host:26379/mymaster"
)
```

### Connection Management

The Redis backend manages connections internally:

- Creates new pubsub connections for each subscription
- Cleans up connections when subscriptions end
- Uses the `redis-py` async client

---

## Polling vs Notifications Comparison

| Aspect | Polling | Redis Notifications |
|--------|---------|---------------------|
| Latency | 100ms - 5s | ~1ms |
| DB Load | Higher (constant queries) | Lower |
| Complexity | None | Requires Redis |
| Reliability | Always works | Depends on Redis |
| Scalability | DB-limited | Very high |

### When to Use Each

**Polling (no notifications):**
- Development environment
- Simple deployments
- When Redis isn't available
- Low-throughput workloads

**Redis notifications:**
- Production deployments
- Low-latency requirements
- High-throughput workloads
- When you already have Redis

---

## Custom Notification Backend

Implement the protocol for custom notification systems:

```python
from typing import AsyncIterator, Dict, Any

class MyNotificationBackend:
    def __init__(self, config):
        self.config = config
    
    async def notify_task_completed(self, task_id: str) -> None:
        # Publish task completion
        await self.publish(f"task:{task_id}", {"state": "completed"})
    
    async def notify_task_updated(self, task_id: str, state: str) -> None:
        await self.publish(f"task:{task_id}", {"state": state})
    
    async def subscribe_to_task(
        self,
        task_id: str,
        *,
        expiry: float | None = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        # Subscribe and yield messages
        async for message in self.subscribe(f"task:{task_id}", timeout=expiry):
            yield message
            if message.get("state") in ("completed", "failed"):
                break
    
    async def notify_execution_updated(self, execution_id: str, state: str) -> None:
        await self.publish(f"execution:{execution_id}", {"state": state})
    
    async def subscribe_to_execution(
        self,
        execution_id: str,
        *,
        expiry: float | None = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        async for message in self.subscribe(f"execution:{execution_id}", timeout=expiry):
            yield message
            if message.get("state") in ("completed", "failed", "timed_out", "cancelled"):
                break
```

### Requirements

1. **Low latency**: Notifications should be delivered quickly
2. **Reliable delivery**: Messages shouldn't be lost (though Stent falls back to polling)
3. **Proper cleanup**: Subscriptions should clean up on completion or timeout
4. **Async support**: All methods must be async
