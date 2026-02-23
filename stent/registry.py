from dataclasses import dataclass
from typing import Callable, Awaitable, Any, List, ItemsView
from stent.core import RetryPolicy

@dataclass
class FunctionMetadata:
    name: str
    fn: Callable[..., Awaitable[Any]]
    cached: bool
    retry_policy: RetryPolicy
    tags: List[str]
    priority: int
    queue: str | None
    version: str | None
    idempotent: bool
    idempotency_key_func: Callable[..., str] | None
    max_concurrent: int | None = None

class FunctionRegistry:
    def __init__(self):
        self._registry: dict[str, FunctionMetadata] = {}

    def register(self, meta: FunctionMetadata) -> None:
        self._registry[meta.name] = meta

    def get(self, name: str) -> FunctionMetadata | None:
        return self._registry.get(name)

    def items(self) -> ItemsView[str, FunctionMetadata]:
        return self._registry.items()

    def copy(self) -> "FunctionRegistry":
        clone = FunctionRegistry()
        clone._registry = dict(self._registry)
        return clone

    def name_for_function(self, fn: Callable[..., Any]) -> str:
        # Simplistic implementation. In a real app we might need stable names regardless of import path.
        return f"{fn.__module__}:{fn.__qualname__}"

registry = FunctionRegistry()
