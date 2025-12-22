# Max Workflow Duration

## Description
Added `max_duration` parameter to `executor.dispatch()` as a more descriptive alias for `expiry`. This allows users to specify the maximum allowed runtime for a workflow before it is automatically timed out.

## Key Changes
*   **`senpuki/executor.py`**: Updated `dispatch` method to accept `max_duration`.
    *   `max_duration` works exactly like `expiry`.
    *   Added validation to prevent providing both `expiry` and `max_duration`.
*   **Tests**: Added `tests/test_max_duration.py` to verify:
    *   Successful execution within duration.
    *   Timeout behavior when execution exceeds duration.
    *   Error raising when both parameters are used.

## Usage/Configuration
```python
# Dispatch with a 6-hour time limit
await executor.dispatch(my_workflow, max_duration="6h")

# Also supports timedelta objects
from datetime import timedelta
await executor.dispatch(my_workflow, max_duration=timedelta(minutes=30))
```
