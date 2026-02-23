# Exception Handling & Serialization Improvements

## Description

Implements Phase 3 of the hardening plan, enhancing JSON serialization for exceptions and RetryPolicy objects. This improves debugging experience by preserving tracebacks and custom exception attributes, and enables full round-trip serialization of RetryPolicy including the `retry_for` exception tuple.

## Key Changes

### `stent/utils/serialization.py`
- Added `_EXCEPTION_REGISTRY` - a registry mapping exception class names to classes for deserialization
- Added `register_exception()` function to register custom exception classes
- Added `get_exception_class()` function to look up exception classes with fallback to `Exception`
- Added `_extract_exception_attributes()` to capture custom exception attributes
- Added `_format_traceback()` to format exception tracebacks as strings
- Added `DeserializedException` wrapper class that preserves original exception info
- Updated `JsonSerializer._default()` to serialize exceptions with traceback, cls, message, and attributes
- Updated `JsonSerializer._default()` to serialize `RetryPolicy.retry_for` as list of class names
- Added `_deserialize_exception()` to reconstruct exceptions with full fidelity
- Added `_deserialize_retry_policy()` to reconstruct RetryPolicy including `retry_for`

### `stent/backend/utils.py`
- Updated `_retry_policy_to_dict()` to include `retry_for` as list of class names
- Updated `_retry_policy_from_dict()` to reconstruct `retry_for` using the exception registry

## Usage/Configuration

### Exception Serialization

Exceptions are now serialized with full context:

```python
from stent.utils.serialization import JsonSerializer

serializer = JsonSerializer()

try:
    raise ValueError("invalid input")
except ValueError as e:
    data = serializer.dumps(e)
    # {"__type__": "Exception", "cls": "ValueError", "message": "invalid input", 
    #  "traceback": "Traceback (most recent call last):...", "attributes": {}}
    
    restored = serializer.loads(data)
    # For built-in exceptions, restores the actual type (ValueError)
    # Traceback available via restored._original_traceback
```

### Custom Exception Registration

Register custom exceptions for proper deserialization:

```python
from stent.utils.serialization import register_exception

class MyAppError(Exception):
    def __init__(self, msg, error_code):
        super().__init__(msg)
        self.error_code = error_code

# Register for deserialization
register_exception(MyAppError)

# Now MyAppError will be properly reconstructed with its attributes
```

### RetryPolicy Serialization

RetryPolicy now fully round-trips including `retry_for`:

```python
from stent.core import RetryPolicy
from stent.utils.serialization import JsonSerializer

serializer = JsonSerializer()

policy = RetryPolicy(
    max_attempts=5,
    retry_for=(ValueError, TypeError, KeyError)
)

data = serializer.dumps(policy)
# {"__type__": "RetryPolicy", ..., "retry_for": ["ValueError", "TypeError", "KeyError"]}

restored = serializer.loads(data)
# restored.retry_for == (ValueError, TypeError, KeyError)
```

## Serialized Exception Format

```json
{
    "__type__": "Exception",
    "cls": "ValueError",
    "message": "invalid input",
    "traceback": "Traceback (most recent call last):\n  File ...",
    "attributes": {"custom_field": "value"}
}
```

## Limitations

- Custom exception classes must be registered via `register_exception()` for proper type restoration
- Unregistered exceptions deserialize to `DeserializedException` wrapper
- Exception attributes must be JSON-serializable to be preserved
- Traceback is stored as a string, not a live traceback object
