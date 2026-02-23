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
result = Result.Ok(42)
print(result.ok)     # True
print(result.value)  # 42
print(result.error)  # None
print(bool(result))  # True
```

### `Result.Error(error)`

Create an error result.

```python
result = Result.Error("Something went wrong")
print(result.ok)     # False
print(result.value)  # None
print(result.error)  # "Something went wrong"
print(bool(result))  # False
```

## Instance Methods

### `unwrap()`

Extract the value or raise an exception if error.

```python
def unwrap(self) -> T:
```

#### Behavior

- If `ok` is `True`, returns `value`
- If `ok` is `False` and `error` is an exception, raises it
- If `ok` is `False` and `error` is not an exception, raises `Exception(str(error))`

#### Example

```python
Result.Ok(42).unwrap()                          # Returns 42
Result.Error(ValueError("bad")).unwrap()         # Raises ValueError
Result.Error("Something went wrong").unwrap()    # Raises Exception
```

### `or_raise()`

Alias for `unwrap()`.

```python
value = result.or_raise()  # Same as result.unwrap()
```

### `unwrap_or(default)`

Return the value if ok, otherwise return `default`.

```python
def unwrap_or(self, default: T) -> T:
```

#### Example

```python
Result.Ok(42).unwrap_or(0)       # 42
Result.Error("err").unwrap_or(0)  # 0
```

### `map(fn)`

Apply a function to the value if ok, pass through errors unchanged.

```python
def map(self, fn: Callable[[T], U]) -> Result[U, E]:
```

#### Example

```python
Result.Ok(5).map(lambda x: x * 2)       # Result.Ok(10)
Result.Error("err").map(lambda x: x * 2) # Result.Error("err")

# Chain transforms
result = (
    Result.Ok(42)
    .map(str)
    .map(lambda s: f"value: {s}")
)
# Result.Ok("value: 42")
```

### `flat_map(fn)`

Apply a function that returns a Result to the value if ok, pass through errors unchanged.

```python
def flat_map(self, fn: Callable[[T], Result[U, E]]) -> Result[U, E]:
```

#### Example

```python
def safe_divide(x):
    if x == 0:
        return Result.Error("division by zero")
    return Result.Ok(100 / x)

Result.Ok(5).flat_map(safe_divide)   # Result.Ok(20.0)
Result.Ok(0).flat_map(safe_divide)   # Result.Error("division by zero")
Result.Error("err").flat_map(safe_divide)  # Result.Error("err")
```

### `__bool__()`

Results are truthy when ok, falsy when error. Note: `Result.Ok(0)` and `Result.Ok("")` are still truthy.

```python
if result:
    print("success")
else:
    print("error")

# Falsey values are still truthy Results
bool(Result.Ok(0))     # True
bool(Result.Ok(""))    # True
bool(Result.Ok(None))  # True
bool(Result.Error(0))  # False
```

## Usage Patterns

### Basic Pattern Matching

```python
result = await process_order("ORD-123")

if result.ok:
    print(f"Success: {result.value}")
else:
    print(f"Error: {result.error}")
```

### Chaining with map/flat_map

```python
@Stent.durable
async def workflow() -> Result:
    return (
        await fetch_user("user-123")
    ).flat_map(
        lambda user: await create_order(user)
    ).map(
        lambda order: order["id"]
    )
```

### Unwrap with Default

```python
# Get value or use a default
count = result.unwrap_or(0)
name = result.unwrap_or("anonymous")
```

### Converting to Exceptions

```python
@Stent.durable
async def main_workflow():
    result = await might_fail()
    value = result.unwrap()  # Raises if error
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

async def divide(a: int, b: int) -> Result[int, str]:
    if b == 0:
        return Result.Error("Division by zero")
    return Result.Ok(a // b)

async def process_users(ids: list[str]) -> Result[list[dict], Exception]:
    ...
```

## Comparison with Exceptions

### When to Use Each

**Use `Result` when:**
- You want explicit error handling at each step
- Errors are expected and part of normal control flow
- You want to chain transforms with `map`/`flat_map`

**Use Exceptions when:**
- Errors are exceptional and unexpected
- You want errors to propagate automatically
- Simpler is better for your use case

Both approaches work well with Stent.
