# Developer Experience Hardening (Phase 3)

## Description
Phase 3 focuses on developer experience fixes called out in `REVIEW.md`: fail-fast behavior when dispatching unregistered functions, scoped registries for better test isolation, and clarified expiry/delay semantics.

## Key Changes
* `senpuki/executor.py`: adds `function_registry` injection on the executor constructor, fail-fast checks in `dispatch`, `map`, `wrap`, and `_handle_task`, and a named `UnregisteredFunctionError`.
* `senpuki/registry.py`: exposes `FunctionRegistry.copy()`/`items()` helpers so callers can clone or inspect registries without touching globals.
* `tests/test_execution.py`: new regression coverage for unregistered dispatches and per-executor registries.
* `README.md`: documents the stricter registration requirements, shows how to supply a custom registry, and clarifies that expiry timers begin after any scheduled delay.

## Usage/Configuration
```python
from senpuki import Senpuki
from senpuki.registry import registry

# Clone the global registry and tweak it for a test or tenant
custom_registry = registry.copy()
executor = Senpuki(backend=my_backend, function_registry=custom_registry)

@Senpuki.durable()
async def workflow():
    ...

# dispatch() now raises UnregisteredFunctionError if `workflow` is not decorated/registered.
exec_id = await executor.dispatch(workflow, delay="5m", expiry="1h")
# expiry countdown starts once the 5 minute delay has elapsed.
```
