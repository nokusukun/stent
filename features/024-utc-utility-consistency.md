# UTC Utility Consistency

## Description
Aligned `now_utc()` behavior with its name by returning a timezone-aware UTC datetime instead of a naive local timestamp.

## Key Changes
* `stent/utils/time.py`
  * Updated `now_utc()` to return `datetime.now(timezone.utc)`.
* `tests/test_core.py`
  * Added regression test asserting `now_utc()` returns a timezone-aware datetime with `timezone.utc`.

## Usage/Configuration
```python
from stent.utils.time import now_utc

ts = now_utc()
assert ts.tzinfo is not None
```
