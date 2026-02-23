from __future__ import annotations
from typing import TYPE_CHECKING, Any, Callable, TypeVar, cast
import functools
import logging
from stent import Stent

logger = logging.getLogger(__name__)

_HAS_OTEL = False

# Try to import opentelemetry at runtime
try:
    from opentelemetry import trace as _trace  # type: ignore[import-not-found]
    from opentelemetry.trace import Status as _Status, StatusCode as _StatusCode  # type: ignore[import-not-found]
    _HAS_OTEL = True
except ImportError:
    # Provide stub values for when otel is not installed
    _trace = None
    _Status = None
    _StatusCode = None

# Type hints for when type checking (opentelemetry may not be installed at runtime)
if TYPE_CHECKING:
    from opentelemetry import trace  # type: ignore[import-not-found]
    from opentelemetry.trace import Status, StatusCode, Tracer  # type: ignore[import-not-found]
    # Create type aliases that are only used during type checking
    TraceModule = Any
    StatusType = Any
    StatusCodeType = Any

F = TypeVar('F', bound=Callable[..., Any])


def instrument(tracer_provider: Any = None) -> bool:
    """
    Instruments the Stent library with OpenTelemetry.
    Returns True if instrumentation was installed, False otherwise.
    """
    if not _HAS_OTEL or _trace is None:
        logger.warning("OpenTelemetry not installed; skipping Stent instrumentation.")
        return False

    tracer = _trace.get_tracer("stent", tracer_provider=tracer_provider)
    
    _instrument_executor(tracer)
    return True


def _instrument_executor(tracer: Any) -> None:
    """
    Internal function to instrument the executor methods.
    
    Note: This function is only called when _HAS_OTEL is True,
    so _trace, _Status, and _StatusCode are guaranteed to be non-None.
    """
    # Idempotency check
    if getattr(Stent.dispatch, "_is_otel_instrumented", False):
        return

    # Local references to ensure type checker knows these are non-None
    # (they are guaranteed to be set because instrument() guards this call)
    trace_mod = cast(Any, _trace)
    status_cls = cast(Any, _Status)
    status_code_cls = cast(Any, _StatusCode)

    original_dispatch = Stent.dispatch
    original_handle_task = Stent._handle_task
    
    @functools.wraps(original_dispatch)
    async def dispatch_wrapper(self: Stent, fn: Any, *args: Any, **kwargs: Any) -> str:
        # Resolve name properly if it's a wrapped function
        name = "unknown"
        if hasattr(fn, "__name__"):
            name = fn.__name__
        
        with tracer.start_as_current_span(f"stent.dispatch {name}", kind=trace_mod.SpanKind.PRODUCER) as span:
            span.set_attribute("stent.function", name)
            
            try:
                exec_id = await original_dispatch(self, fn, *args, **kwargs)
                span.set_attribute("stent.execution_id", exec_id)
                return exec_id
            except Exception as e:
                span.record_exception(e)
                span.set_status(status_cls(status_code_cls.ERROR, str(e)))
                raise

    setattr(dispatch_wrapper, "_is_otel_instrumented", True)
    Stent.dispatch = dispatch_wrapper  # type: ignore[method-assign]
    
    @functools.wraps(original_handle_task)
    async def handle_task_wrapper(self: Stent, task: Any, worker_id: str, *args: Any, **kwargs: Any) -> None:
        # Consumer span
        with tracer.start_as_current_span(f"stent.execute {task.step_name}", kind=trace_mod.SpanKind.CONSUMER) as span:
             span.set_attribute("stent.task_id", task.id)
             span.set_attribute("stent.execution_id", task.execution_id)
             span.set_attribute("stent.step", task.step_name)
             span.set_attribute("stent.worker_id", worker_id)
             
             try:
                 await original_handle_task(self, task, worker_id, *args, **kwargs)
                 
                 # Check task status after execution
                 if task.state == "failed":
                     span.set_status(status_cls(status_code_cls.ERROR, str(task.error)))
                 elif task.state == "completed":
                     span.set_status(status_cls(status_code_cls.OK))
                     
             except Exception as e:
                 span.record_exception(e)
                 span.set_status(status_cls(status_code_cls.ERROR, str(e)))
                 raise

    setattr(handle_task_wrapper, "_is_otel_instrumented", True)
    Stent._handle_task = handle_task_wrapper  # type: ignore[method-assign]
