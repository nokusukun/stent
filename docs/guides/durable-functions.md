# Defining Durable Functions

This guide covers everything you need to know about defining durable functions with the `@Stent.durable()` decorator.

## Basic Definition

Every function you want Stent to manage must be decorated with `@Stent.durable()`:

```python
from stent import Stent

@Stent.durable()
async def my_function(arg1: str, arg2: int) -> str:
    # Your async code here
    return f"{arg1}: {arg2}"
```

### Requirements

1. **Must be async**: Durable functions must use `async def`
2. **Serializable arguments**: All arguments must be JSON-serializable (or pickle-serializable if using pickle)
3. **Serializable return value**: The return value must also be serializable

## Decorator Options

### `cached`

Cache results for reuse across different executions.

```python
@Stent.durable(cached=True)
async def expensive_computation(data_hash: str) -> bytes:
    # First call computes the result
    # Subsequent calls with same arguments return cached result
    result = await compute_expensive_thing(data_hash)
    return result
```

**How it works:**
- A cache key is generated from function name, version, and arguments
- Results are stored in the cache table
- Future calls with matching key return the cached result immediately

**Use cases:**
- Expensive computations
- External API calls that return stable data
- Report generation

### `retry_policy`

Configure automatic retries on failure.

```python
from stent import Stent, RetryPolicy

@Stent.durable(
    retry_policy=RetryPolicy(
        max_attempts=5,
        initial_delay=1.0,
        backoff_factor=2.0,
        max_delay=60.0,
        jitter=0.1,
        retry_for=(ConnectionError, TimeoutError)
    )
)
async def unreliable_api_call(url: str) -> dict:
    response = await http_client.get(url)
    return response.json()
```

See [RetryPolicy API Reference](../api-reference/retry-policy.md) for full details.

### `tags`

Add tags for worker filtering.

```python
@Stent.durable(tags=["billing", "high-priority"])
async def charge_customer(customer_id: str, amount: int) -> str:
    ...

# Worker that only processes billing tasks
await executor.serve(tags=["billing"])
```

**Use cases:**
- Route tasks to specialized workers
- Separate workloads by domain
- Implement priority lanes

### `priority`

Set task priority (higher values = processed first).

```python
@Stent.durable(priority=10)
async def urgent_task():
    ...

@Stent.durable(priority=0)  # Default
async def normal_task():
    ...

@Stent.durable(priority=-10)
async def background_task():
    ...
```

Workers process tasks in priority order when multiple tasks are available.

### `queue`

Assign function to a specific queue.

```python
@Stent.durable(queue="critical")
async def critical_operation():
    ...

@Stent.durable(queue="background")
async def background_job():
    ...

# Workers can subscribe to specific queues
await executor.serve(queues=["critical"])  # Only critical tasks
await executor.serve(queues=["background"])  # Only background tasks
await executor.serve()  # All queues
```

### `idempotent`

Enable idempotency to prevent duplicate execution.

```python
@Stent.durable(idempotent=True)
async def send_welcome_email(user_id: str) -> bool:
    # Only executes once per unique user_id
    await email_service.send(user_id, "Welcome!")
    return True
```

**How it works:**
- An idempotency key is generated from function name, version, and arguments
- If a result exists for that key, it's returned without re-executing
- Different from caching: idempotency is permanent, caching can have TTL

### `idempotency_key_func`

Provide a custom function to generate idempotency keys.

```python
@Stent.durable(
    idempotent=True,
    idempotency_key_func=lambda order_id, **_: f"process:{order_id}"
)
async def process_order(order_id: str, timestamp: str, retries: int) -> dict:
    # Key only uses order_id, ignoring timestamp and retries
    # This means re-calling with different timestamp/retries won't re-execute
    ...
```

**Use cases:**
- Ignore certain arguments for idempotency
- Create composite keys
- Match external idempotency schemes

### `version`

Version string for cache/idempotency keys.

```python
@Stent.durable(cached=True, version="v2")
async def process_data(data: dict) -> dict:
    # Changing version invalidates previous cache entries
    ...
```

**When to change version:**
- Function logic changes significantly
- Return format changes
- You want to invalidate cached results

### `max_concurrent`

Limit concurrent executions across the entire cluster.

```python
@Stent.durable(max_concurrent=5)
async def call_rate_limited_api(data: dict) -> dict:
    # Only 5 concurrent calls across all workers
    return await api.call(data)
```

**How it works:**
- Workers check the count of running tasks for this function
- If at limit, the task stays pending
- Other tasks continue to be processed

**Use cases:**
- Protect external rate-limited APIs
- Control resource usage
- Prevent system overload

## Function Registration

### Automatic Registration

Functions decorated with `@Stent.durable()` are automatically registered in the global registry:

```python
from stent import Stent, registry

@Stent.durable()
async def my_function():
    ...

# Function is now registered
meta = registry.get("__main__:my_function")
print(meta.name)  # "__main__:my_function"
```

### Function Names

Stent generates function names from the module and qualified name:

```python
# In myapp/tasks.py
@Stent.durable()
async def process_item():
    ...

# Registered as: "myapp.tasks:process_item"

# For nested classes
class MyClass:
    @Stent.durable()
    async def method(self):
        ...

# Registered as: "myapp.tasks:MyClass.method"
```

### Custom Registry

Use a custom registry for isolation:

```python
from stent.registry import FunctionRegistry

# Create isolated registry
custom_registry = FunctionRegistry()

# Register functions manually
from stent.registry import FunctionMetadata

meta = FunctionMetadata(
    name="custom:my_function",
    fn=my_function,
    cached=False,
    retry_policy=RetryPolicy(),
    tags=[],
    priority=0,
    queue=None,
    idempotent=False,
    idempotency_key_func=None,
    version=None,
    max_concurrent=None
)
custom_registry.register(meta)

# Use with executor
executor = Stent(backend=backend, function_registry=custom_registry)
```

**Use cases:**
- Testing with isolated registries
- Multi-tenant applications
- Plugin systems

## Argument Serialization

### JSON Serializer (Default)

The default JSON serializer supports:

- Primitives: `str`, `int`, `float`, `bool`, `None`
- Collections: `list`, `dict`, `tuple` (converted to list)
- `Result` type
- `Exception` (serialized as message + class name)

```python
@Stent.durable()
async def json_compatible(
    name: str,
    count: int,
    options: dict,
    items: list[str]
) -> dict:
    ...
```

### Pickle Serializer

For complex Python objects, use pickle:

```python
executor = Stent(backend=backend, serializer="pickle")

@Stent.durable()
async def with_complex_args(
    df: pandas.DataFrame,
    model: sklearn.Model,
    config: MyCustomClass
) -> numpy.ndarray:
    ...
```

**Trade-offs:**
- Pro: Supports any Python object
- Con: Not human-readable in database
- Con: Version compatibility issues
- Con: Security concerns with untrusted data

### Custom Serializer

Implement the `Serializer` protocol:

```python
from typing import Any

class MySerializer:
    def dumps(self, obj: Any) -> bytes:
        # Serialize object to bytes
        ...
    
    def loads(self, data: bytes) -> Any:
        # Deserialize bytes to object
        ...

executor = Stent(backend=backend, serializer=MySerializer())
```

## Best Practices

### 1. Keep Functions Focused

```python
# Good: Single responsibility
@Stent.durable()
async def fetch_user(user_id: str) -> dict:
    return await db.get_user(user_id)

@Stent.durable()
async def send_email(email: str, subject: str, body: str) -> bool:
    return await email_client.send(email, subject, body)

# Bad: Too many responsibilities
@Stent.durable()
async def fetch_user_and_send_email_and_update_stats(...):
    ...
```

### 2. Use Appropriate Retry Policies

```python
# Network operations: More retries, short delays
@Stent.durable(
    retry_policy=RetryPolicy(
        max_attempts=5,
        initial_delay=0.5,
        retry_for=(ConnectionError, TimeoutError)
    )
)
async def api_call():
    ...

# Database operations: Fewer retries
@Stent.durable(
    retry_policy=RetryPolicy(
        max_attempts=3,
        initial_delay=1.0,
        retry_for=(DatabaseError,)
    )
)
async def db_operation():
    ...

# Validation: No retries
@Stent.durable(
    retry_policy=RetryPolicy(max_attempts=1)
)
async def validate_input(data: dict):
    ...
```

### 3. Make Side Effects Idempotent

```python
# Bad: Not idempotent
@Stent.durable()
async def increment_counter(counter_id: str):
    await db.execute("UPDATE counters SET value = value + 1 WHERE id = ?", counter_id)

# Good: Idempotent with explicit state
@Stent.durable(idempotent=True)
async def set_counter(counter_id: str, value: int):
    await db.execute("UPDATE counters SET value = ? WHERE id = ?", value, counter_id)
```

### 4. Document Function Contracts

```python
@Stent.durable(
    retry_policy=RetryPolicy(max_attempts=3),
    idempotent=True,
    tags=["billing"]
)
async def charge_customer(
    customer_id: str,
    amount_cents: int,
    currency: str = "USD"
) -> dict:
    """
    Charge a customer's payment method.
    
    Args:
        customer_id: The customer's unique identifier
        amount_cents: Amount to charge in cents
        currency: ISO currency code (default: USD)
    
    Returns:
        dict with keys: transaction_id, status, charged_at
    
    Raises:
        PaymentError: If payment fails after retries
    
    Note:
        This function is idempotent - safe to retry.
        Uses the billing tag for worker routing.
    """
    ...
```

### 5. Version Carefully

```python
# When changing function behavior, consider:

# Option 1: New version (invalidates cache)
@Stent.durable(cached=True, version="v2")  # Was "v1"
async def process_data(data: dict) -> dict:
    # New processing logic
    ...

# Option 2: New function (for significant changes)
@Stent.durable(cached=True)
async def process_data_v2(data: dict) -> dict:
    # Completely new approach
    ...
```
