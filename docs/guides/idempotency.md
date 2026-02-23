# Idempotency & Caching

This guide covers how to use Stent's idempotency and caching features to prevent duplicate work and optimize performance.

## Overview

Stent provides two related but distinct features:

| Feature | Purpose | Scope | Duration |
|---------|---------|-------|----------|
| **Idempotency** | Prevent duplicate execution | Per function + args | Permanent |
| **Caching** | Reuse expensive results | Per function + args | Permanent (or TTL) |

Both work by generating a key from the function name, version, and arguments, then checking if a result already exists for that key.

## Idempotency

Idempotency ensures a function with the same arguments only executes once, regardless of how many times it's called.

### Enabling Idempotency

```python
from stent import Stent

@Stent.durable(idempotent=True)
async def send_welcome_email(user_id: str) -> bool:
    # Even if called multiple times with same user_id,
    # email is only sent once
    await email_service.send(user_id, "Welcome!")
    return True
```

### How It Works

1. When called, Stent generates a key from:
   - Function name
   - Version (if set)
   - Serialized arguments

2. Checks if key exists in idempotency table

3. If exists: returns stored result immediately

4. If not: executes function, stores result with key

```
Call: send_welcome_email("user-123")
       |
       v
Generate key: "module:send_welcome_email|None|["user-123"]"
       |
       v
Check idempotency table
       |
       +---[key exists]---> Return stored result
       |
       +---[key missing]---> Execute function
                                   |
                                   v
                             Store result with key
                                   |
                                   v
                             Return result
```

### Use Cases

1. **Payment processing**:
```python
@Stent.durable(idempotent=True)
async def charge_customer(order_id: str, amount: int) -> str:
    # Safe to retry - won't double-charge
    return await payment_gateway.charge(order_id, amount)
```

2. **Email sending**:
```python
@Stent.durable(idempotent=True)
async def send_order_confirmation(order_id: str) -> bool:
    # Won't send duplicate emails
    return await email_service.send_confirmation(order_id)
```

3. **External API calls**:
```python
@Stent.durable(idempotent=True)
async def create_external_resource(resource_id: str, config: dict) -> dict:
    # Won't create duplicate resources
    return await external_api.create(resource_id, config)
```

### Custom Idempotency Keys

Use `idempotency_key_func` to customize key generation:

```python
@Stent.durable(
    idempotent=True,
    idempotency_key_func=lambda order_id, **_: f"process_order:{order_id}"
)
async def process_order(order_id: str, timestamp: str, retry_count: int) -> dict:
    # Key only uses order_id
    # Different timestamp/retry_count won't cause re-execution
    ...
```

#### Examples

```python
# Include only specific args
@Stent.durable(
    idempotent=True,
    idempotency_key_func=lambda user_id, action, **_: f"{user_id}:{action}"
)
async def user_action(user_id: str, action: str, metadata: dict) -> bool:
    # metadata changes don't affect idempotency
    ...

# Include external correlation ID
@Stent.durable(
    idempotent=True,
    idempotency_key_func=lambda request, **_: request["correlation_id"]
)
async def process_request(request: dict) -> dict:
    # Uses external system's correlation ID
    ...

# Time-windowed idempotency
@Stent.durable(
    idempotent=True,
    idempotency_key_func=lambda user_id, **_: f"{user_id}:{datetime.now().strftime('%Y-%m-%d')}"
)
async def daily_task(user_id: str) -> bool:
    # One execution per user per day
    ...
```

## Caching

Caching stores function results for reuse across different executions.

### Enabling Caching

```python
@Stent.durable(cached=True)
async def expensive_computation(data_hash: str) -> bytes:
    # Result is cached - subsequent calls return immediately
    return await compute_expensive_result(data_hash)
```

### How It Works

1. Generate cache key from function name, version, and arguments
2. Check cache table for existing result
3. If hit: return cached result
4. If miss: execute function, cache result, return result

### Use Cases

1. **Expensive computations**:
```python
@Stent.durable(cached=True)
async def generate_report(report_params: dict) -> str:
    # Report generation takes 10 minutes
    # Cached for reuse
    return await report_generator.generate(report_params)
```

2. **External data fetching**:
```python
@Stent.durable(cached=True, version="v1")
async def fetch_exchange_rates(date: str) -> dict:
    # Rates for a specific date don't change
    return await forex_api.get_rates(date)
```

3. **ML model inference**:
```python
@Stent.durable(cached=True, version="model-v2")
async def classify_document(doc_hash: str) -> dict:
    # Same document always gets same classification
    return await ml_model.classify(doc_hash)
```

### Versioning

Use `version` to invalidate cache when logic changes:

```python
# Original version
@Stent.durable(cached=True, version="v1")
async def process_data(data: dict) -> dict:
    return original_algorithm(data)

# After algorithm update - new version invalidates old cache
@Stent.durable(cached=True, version="v2")
async def process_data(data: dict) -> dict:
    return improved_algorithm(data)
```

## Idempotency vs Caching

While similar, they serve different purposes:

### Idempotency

```python
@Stent.durable(idempotent=True)
async def charge_customer(order_id: str, amount: int) -> str:
    # Purpose: Prevent duplicate charges
    # Behavior: Never re-executes with same args
    # Typical use: Side effects (payments, emails, etc.)
    return await payment_api.charge(order_id, amount)
```

### Caching

```python
@Stent.durable(cached=True)
async def compute_pricing(product_ids: list[str]) -> dict:
    # Purpose: Avoid redundant computation
    # Behavior: Returns cached result if available
    # Typical use: Pure computations, data fetching
    return await pricing_engine.calculate(product_ids)
```

### When to Use Which

| Scenario | Use |
|----------|-----|
| Payment processing | Idempotency |
| Email sending | Idempotency |
| External resource creation | Idempotency |
| Expensive computation | Caching |
| Static data fetching | Caching |
| Report generation | Caching |
| Both concerns | Both |

### Using Both

```python
@Stent.durable(
    idempotent=True,  # Don't re-execute
    cached=True,      # Reuse across executions
    version="v1"
)
async def process_and_store(data_id: str) -> dict:
    # Both idempotent (won't duplicate) and cached (efficient)
    result = await expensive_process(data_id)
    await store_result(data_id, result)
    return result
```

## Best Practices

### 1. Design for Idempotency

```python
# Bad: Not idempotent
@Stent.durable(idempotent=True)
async def increment_counter(counter_id: str) -> int:
    # Each call increments - not truly idempotent!
    return await db.increment(counter_id)

# Good: Truly idempotent
@Stent.durable(idempotent=True)
async def set_counter(counter_id: str, value: int) -> int:
    # Same args always produce same result
    return await db.set(counter_id, value)
```

### 2. Use External Idempotency Keys

```python
@Stent.durable(
    idempotent=True,
    idempotency_key_func=lambda order, **_: order["idempotency_key"]
)
async def process_order(order: dict) -> dict:
    # Client provides idempotency key
    # Enables safe retries from client side
    ...
```

### 3. Version When Changing Logic

```python
# When you change the function's behavior:
# 1. Update version string
# 2. Old cached results won't be used
# 3. New results will be cached with new key

@Stent.durable(cached=True, version="v3")  # Was v2
async def transform_data(data: dict) -> dict:
    # New transformation logic
    ...
```

### 4. Consider Key Size

```python
# Bad: Large args make large keys
@Stent.durable(cached=True)
async def process_large_data(huge_list: list[dict]) -> dict:
    # Key includes serialized huge_list - inefficient
    ...

# Good: Use hash or ID
@Stent.durable(cached=True)
async def process_large_data(data_hash: str) -> dict:
    # Key is just the hash - efficient
    data = await fetch_data(data_hash)
    return process(data)
```

### 5. Handle Cache Misses Gracefully

```python
@Stent.durable
async def workflow_with_caching():
    # First call computes result
    result1 = await expensive_computation("hash-1")
    
    # Second call uses cache
    result2 = await expensive_computation("hash-1")
    
    # Different args computes new result
    result3 = await expensive_computation("hash-2")
```

## Cache Invalidation

Stent doesn't provide automatic cache invalidation. Strategies:

### 1. Version Bumping

```python
# Increment version to invalidate all cached results
@Stent.durable(cached=True, version="v4")  # Bump from v3
async def process_data(data: dict) -> dict:
    ...
```

### 2. Time-Based Keys

```python
@Stent.durable(
    cached=True,
    idempotency_key_func=lambda data_id, **_: 
        f"{data_id}:{datetime.now().strftime('%Y-%m-%d')}"
)
async def daily_report(data_id: str) -> dict:
    # New cache entry each day
    ...
```

### 3. Include Version in Args

```python
@Stent.durable(cached=True)
async def fetch_with_version(resource_id: str, version: int) -> dict:
    # Cache key includes version
    # Caller controls invalidation by passing new version
    return await fetch_resource(resource_id)
```

## Debugging

### Check If Result Was Cached

The execution progress shows cache hits:

```python
state = await executor.state_of(exec_id)

for progress in state.progress:
    if progress.status == "cache_hit":
        print(f"Cache hit for {progress.step}")
```

### View Cache Contents

```python
# Direct database query (SQLite example)
import aiosqlite

async def view_cache():
    async with aiosqlite.connect("stent.db") as db:
        async with db.execute("SELECT key, created_at FROM cache") as cursor:
            async for row in cursor:
                print(f"Key: {row[0]}, Created: {row[1]}")
```

### Clear Cache

```python
# Manual cache clear (use with caution)
async def clear_cache():
    async with aiosqlite.connect("stent.db") as db:
        await db.execute("DELETE FROM cache")
        await db.commit()
```
