from typing import Protocol, Any, Dict
import json
import pickle
import base64
import traceback
from senpuki.core import Result, RetryPolicy


# Registry of known exception classes for deserialization
# Maps fully qualified class name -> class
_EXCEPTION_REGISTRY: Dict[str, type[BaseException]] = {
    # Built-in exceptions
    "Exception": Exception,
    "ValueError": ValueError,
    "TypeError": TypeError,
    "KeyError": KeyError,
    "IndexError": IndexError,
    "AttributeError": AttributeError,
    "RuntimeError": RuntimeError,
    "IOError": IOError,
    "OSError": OSError,
    "FileNotFoundError": FileNotFoundError,
    "PermissionError": PermissionError,
    "TimeoutError": TimeoutError,
    "ConnectionError": ConnectionError,
    "NotImplementedError": NotImplementedError,
    "StopIteration": StopIteration,
    "AssertionError": AssertionError,
    "ImportError": ImportError,
    "ModuleNotFoundError": ModuleNotFoundError,
    "ZeroDivisionError": ZeroDivisionError,
    "OverflowError": OverflowError,
    "MemoryError": MemoryError,
    "RecursionError": RecursionError,
    "SystemError": SystemError,
    "UnicodeError": UnicodeError,
    "UnicodeDecodeError": UnicodeDecodeError,
    "UnicodeEncodeError": UnicodeEncodeError,
}


def register_exception(cls: type[BaseException], name: str | None = None) -> None:
    """Register an exception class for JSON deserialization.
    
    Args:
        cls: The exception class to register.
        name: Optional name to register under. Defaults to class __name__.
    """
    key = name or cls.__name__
    _EXCEPTION_REGISTRY[key] = cls


def get_exception_class(name: str) -> type[BaseException]:
    """Get an exception class from the registry, falling back to Exception."""
    return _EXCEPTION_REGISTRY.get(name, Exception)


def _extract_exception_attributes(exc: BaseException) -> Dict[str, Any]:
    """Extract custom attributes from an exception.
    
    Returns a dict of attributes that are not standard exception internals.
    """
    # Standard exception attributes to skip
    skip_attrs = {
        "args", "__dict__", "__cause__", "__context__", "__suppress_context__",
        "__traceback__", "__notes__", "add_note", "with_traceback",
    }
    
    attrs: Dict[str, Any] = {}
    for key in dir(exc):
        if key.startswith("_") or key in skip_attrs:
            continue
        try:
            val = getattr(exc, key)
            # Skip methods/callables
            if callable(val):
                continue
            # Try to ensure it's JSON-serializable
            json.dumps(val)
            attrs[key] = val
        except (TypeError, ValueError, AttributeError):
            # Not serializable, skip
            pass
    
    return attrs


def _format_traceback(exc: BaseException) -> str | None:
    """Format exception traceback as a string."""
    if exc.__traceback__ is None:
        return None
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


class Serializer(Protocol):
    def dumps(self, obj: Any) -> bytes: ...
    def loads(self, data: bytes) -> Any: ...


class PickleSerializer(Serializer):
    def dumps(self, obj: Any) -> bytes:
        return pickle.dumps(obj)

    def loads(self, data: bytes) -> Any:
        return pickle.loads(data)


class JsonSerializer(Serializer):
    def dumps(self, obj: Any) -> bytes:
        return json.dumps(obj, default=self._default).encode("utf-8")

    def loads(self, data: bytes) -> Any:
        return json.loads(data.decode("utf-8"), object_hook=self._object_hook)

    def _default(self, obj: Any) -> Any:
        if isinstance(obj, Result):
            return {
                "__type__": "Result",
                "ok": obj.ok,
                "value": obj.value,
                "error": obj.error
            }
        if isinstance(obj, RetryPolicy):
            return {
                "__type__": "RetryPolicy",
                "max_attempts": obj.max_attempts,
                "backoff_factor": obj.backoff_factor,
                "initial_delay": obj.initial_delay,
                "max_delay": obj.max_delay,
                "jitter": obj.jitter,
                "retry_for": [exc.__name__ for exc in obj.retry_for],
            }
        if isinstance(obj, BaseException):
            return {
                "__type__": "Exception",
                "cls": obj.__class__.__name__,
                "message": str(obj),
                "traceback": _format_traceback(obj),
                "attributes": _extract_exception_attributes(obj),
            }
        # bytes handling for args/kwargs if they were pre-serialized
        if isinstance(obj, bytes):
            return {"__type__": "bytes", "data": base64.b64encode(obj).decode("ascii")}
        return str(obj)

    def _object_hook(self, dct: Dict[str, Any]) -> Any:
        if "__type__" in dct:
            t = dct["__type__"]
            if t == "Result":
                return Result(ok=dct["ok"], value=dct["value"], error=dct["error"])
            if t == "Exception":
                return _deserialize_exception(dct)
            if t == "RetryPolicy":
                return _deserialize_retry_policy(dct)
            if t == "bytes":
                return base64.b64decode(dct["data"].encode("ascii"))
        return dct


class DeserializedException(Exception):
    """Wrapper for deserialized exceptions that preserves original info."""
    
    def __init__(
        self,
        original_cls: str,
        message: str,
        original_traceback: str | None = None,
        attributes: Dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.original_cls = original_cls
        self.original_traceback = original_traceback
        self.original_attributes = attributes or {}
        
        # Copy attributes onto the exception
        for key, val in self.original_attributes.items():
            if not hasattr(self, key):
                setattr(self, key, val)
    
    def __str__(self) -> str:
        return f"{self.original_cls}: {super().__str__()}"
    
    def __repr__(self) -> str:
        return f"DeserializedException({self.original_cls!r}, {super().__str__()!r})"
    
    def format_original_traceback(self) -> str:
        """Return the original traceback if available, or a placeholder."""
        if self.original_traceback:
            return self.original_traceback
        return f"(no traceback available for {self.original_cls})"


def _deserialize_exception(dct: Dict[str, Any]) -> BaseException:
    """Deserialize an exception from its JSON representation.
    
    Attempts to reconstruct the original exception type if registered,
    otherwise returns a DeserializedException wrapper.
    """
    cls_name = dct.get("cls", "Exception")
    message = dct.get("message", "")
    tb_str = dct.get("traceback")
    attributes = dct.get("attributes", {})
    
    exc_class = _EXCEPTION_REGISTRY.get(cls_name)
    
    if exc_class is not None and exc_class is not Exception:
        # Try to instantiate the actual exception class
        try:
            exc = exc_class(message)
            # Attach custom attributes
            for key, val in attributes.items():
                try:
                    setattr(exc, key, val)
                except AttributeError:
                    pass
            # Store traceback string as custom attribute
            if tb_str:
                exc._original_traceback = tb_str  # type: ignore
            return exc
        except Exception:
            # Fall through to DeserializedException
            pass
    
    # Return wrapped exception preserving all info
    return DeserializedException(
        original_cls=cls_name,
        message=message,
        original_traceback=tb_str,
        attributes=attributes,
    )


def _deserialize_retry_policy(dct: Dict[str, Any]) -> RetryPolicy:
    """Deserialize a RetryPolicy from its JSON representation."""
    retry_for_names = dct.get("retry_for", ["Exception"])
    
    # Look up exception classes from registry, fall back to Exception
    retry_for_classes: list[type[BaseException]] = []
    for name in retry_for_names:
        exc_class = _EXCEPTION_REGISTRY.get(name, Exception)
        if exc_class not in retry_for_classes:
            retry_for_classes.append(exc_class)
    
    # Ensure we have at least one class
    if not retry_for_classes:
        retry_for_classes = [Exception]
    
    return RetryPolicy(
        max_attempts=dct.get("max_attempts", 3),
        backoff_factor=dct.get("backoff_factor", 2.0),
        initial_delay=dct.get("initial_delay", 1.0),
        max_delay=dct.get("max_delay", 60.0),
        jitter=dct.get("jitter", 0.1),
        retry_for=tuple(retry_for_classes),
    )
