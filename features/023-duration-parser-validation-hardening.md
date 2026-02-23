# Duration Parser Validation Hardening

## Description
Hardened duration parsing so only fully valid duration strings are accepted. This prevents malformed inputs from being partially parsed and silently accepted.

## Key Changes
* `stent/utils/time.py`
  * `parse_duration` now validates the entire string (after trimming) using token-by-token parsing.
  * Rejects invalid formats when any unmatched content exists, including trailing junk and unsupported spacing.
  * Keeps existing supported composite forms such as `10s`, `1h30m`, and `2d8h`.
* `tests/test_core.py`
  * Expanded positive coverage for composite durations plus dict/timedelta inputs.
  * Added negative coverage for malformed strings (`""`, `"10x"`, `"10sfoo"`, `"foo10s"`, `"1h 30m"`).

## Usage/Configuration
```python
from stent.utils.time import parse_duration

parse_duration("10s")      # OK
parse_duration("1h30m")    # OK
parse_duration("2d8h")     # OK

parse_duration("10sfoo")   # raises ValueError
parse_duration("1h 30m")   # raises ValueError
```
