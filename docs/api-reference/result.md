# Result API Reference

The `Result` type provides Rust-inspired explicit error handling. It's a generic dataclass that represents either a successful value (`Ok`) or an error (`Error`).

## Import

```python
from stent import Result
```

## Type Signature

```python
@dataclass
class Result(Generic[T, E]):
    ok: bool
    value: T | None
    error: E | None
```

## Creating Results

### `Result.Ok(value)`

Create a successful result.

```python
@classmethod
def Ok(cls, value: T) -> Result[T, E]:
```

#### Example

```python
result = Result.Ok(42)
print(result.ok)     # True
print(result.value)  # 42
print(result.error)  # None
```

### `Result.Error(error)`

Create an error result.

```python
@classmethod
def Error(cls, error: E) -> Result[T, E]:
```

#### Example

```python
result = Result.Error("Something went wrong")
print(result.ok)     # False
print(result.value)  # None
print(result.error)  # "Something went wrong"
```

## Instance Methods

### `or_raise()`

Extract the value or raise an exception if error.

```python
def or_raise(self) -> T:
```

#### Behavior

- If `ok` is `True`, returns `value`
- If `ok` is `False` and `error` is an exception, raises it
- If `ok` is `False` and `error` is not an exception, raises `Exception(str(error))`

#### Example

```python
# Success case
result = Result.Ok(42)
value = result.or_raise()  # Returns 42

# Error case with exception
result = Result.Error(ValueError("Invalid input"))
result.or_raise()  # Raises ValueError("Invalid input")

# Error case with string
result = Result.Error("Something went wrong")
result.or_raise()  # Raises Exception("Something went wrong")
```

## Usage Patterns

### Basic Pattern Matching

```python
@Stent.durable()
async def process_order(order_id: str) -> Result[dict, str]:
    order = await fetch_order(order_id)
    
    if order is None:
        return Result.Error(f"Order {order_id} not found")
    
    if order["status"] == "cancelled":
        return Result.Error("Cannot process cancelled order")
    
    processed = await do_processing(order)
    return Result.Ok(processed)

# Caller
result = await process_order("ORD-123")

if result.ok:
    print(f"Success: {result.value}")
else:
    print(f"Error: {result.error}")
```

### With Exception Errors

```python
@Stent.durable()
async def divide(a: int, b: int) -> Result[float, Exception]:
    try:
        return Result.Ok(a / b)
    except Exception as e:
        return Result.Error(e)

result = await divide(10, 0)

if not result.ok:
    # error is the actual ZeroDivisionError
    print(f"Division failed: {result.error}")
```

### Chaining Results

```python
@Stent.durable()
async def workflow() -> Result[str, Exception]:
    # Each step returns a Result
    user_result = await fetch_user("user-123")
    if not user_result.ok:
        return user_result  # Propagate error
    
    order_result = await create_order(user_result.value)
    if not order_result.ok:
        return order_result  # Propagate error
    
    payment_result = await process_payment(order_result.value)
    if not payment_result.ok:
        return payment_result  # Propagate error
    
    return Result.Ok(f"Order {order_result.value['id']} completed")
```

### Converting to Exceptions

```python
@Stent.durable()
async def main_workflow():
    result = await might_fail()
    
    # Convert Result to exception if needed
    value = result.or_raise()
    
    # Continue with value...
```

## Serialization

The `Result` type is automatically serialized by Stent's JSON serializer:

```python
# Result is serialized as:
{
    "__type__": "Result",
    "ok": true,
    "value": 42,
    "error": null
}
```

When deserialized, it becomes a `Result` instance again.

## Type Hints

Use proper generic type hints for better IDE support:

```python
from stent import Result

# Result with int value and str error
async def divide(a: int, b: int) -> Result[int, str]:
    if b == 0:
        return Result.Error("Division by zero")
    return Result.Ok(a // b)

# Result with complex types
async def process_users(ids: list[str]) -> Result[list[dict], Exception]:
    ...

# Result in orchestrator return
@Stent.durable()
async def main_workflow(data: dict) -> Result[dict, Exception]:
    ...
```

## Comparison with Exceptions

### Using Result

```python
@Stent.durable()
async def process_with_result() -> Result[str, str]:
    data = await fetch_data()
    if not data:
        return Result.Error("No data found")
    
    processed = await transform(data)
    if not processed:
        return Result.Error("Transform failed")
    
    return Result.Ok(processed)

# Caller must handle both cases explicitly
result = await process_with_result()
if result.ok:
    do_something(result.value)
else:
    handle_error(result.error)
```

### Using Exceptions

```python
@Stent.durable()
async def process_with_exceptions() -> str:
    data = await fetch_data()
    if not data:
        raise ValueError("No data found")
    
    processed = await transform(data)
    if not processed:
        raise RuntimeError("Transform failed")
    
    return processed

# Caller can use try/except or let exceptions propagate
try:
    value = await process_with_exceptions()
    do_something(value)
except (ValueError, RuntimeError) as e:
    handle_error(e)
```

### When to Use Each

**Use `Result` when:**
- You want explicit error handling at each step
- Errors are expected and part of normal control flow
- You want better type safety for error types
- You're composing many operations that can fail

**Use Exceptions when:**
- Errors are exceptional and unexpected
- You want errors to propagate automatically
- You're interacting with code that uses exceptions
- Simpler is better for your use case

Both approaches work well with Stent - use whichever fits your coding style and requirements.
