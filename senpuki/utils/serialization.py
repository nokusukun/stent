from typing import Protocol, Any, Dict
import json
import pickle
import base64
from senpuki.core import Result, RetryPolicy

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
             # Simplified for now, passing as dict mostly works if receiver expects it or rehydrates manually
             # Ideally we should hydrate it back.
             return {
                 "__type__": "RetryPolicy",
                 "max_attempts": obj.max_attempts,
                 "backoff_factor": obj.backoff_factor,
                 "initial_delay": obj.initial_delay,
                 "max_delay": obj.max_delay,
                 "jitter": obj.jitter,
                 # retry_for is tricky in JSON as it contains classes.
                 # For simplicity in this demo, we might skip full serialization of retry_for in JSON
                 # or rely on registry lookups.
             }
        if isinstance(obj, Exception):
            return {
                "__type__": "Exception",
                "message": str(obj),
                "cls": obj.__class__.__name__
            }
        # bytes handling for args/kwargs if they were pre-serialized?
        # But args/kwargs here are usually python objects.
        return str(obj)

    def _object_hook(self, dct: Dict[str, Any]) -> Any:
        if "__type__" in dct:
            t = dct["__type__"]
            if t == "Result":
                return Result(ok=dct["ok"], value=dct["value"], error=dct["error"])
            if t == "Exception":
                # Rehydrating exceptions perfectly is hard; returning a proxy or string
                return Exception(f"{dct['cls']}: {dct['message']}")
            if t == "RetryPolicy":
                 # We can't easily restore retry_for types from JSON without a map.
                 # Using defaults for retry_for.
                 return RetryPolicy(
                     max_attempts=dct["max_attempts"],
                     backoff_factor=dct["backoff_factor"],
                     initial_delay=dct["initial_delay"],
                     max_delay=dct["max_delay"],
                     jitter=dct["jitter"]
                 )
        return dct
