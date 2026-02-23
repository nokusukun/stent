# CLI Execution Context Display

## Description
Updated the execution details CLI view to display execution-scoped counters and custom state introduced by the execution context API.

## Key Changes
* `stent/cli.py`
  * `show_execution` now prints a `Counters` section when `state.counters` is present.
  * `show_execution` now prints a `Custom State` section when `state.custom_state` is present.
  * Output is sorted by key for stable readability.
* `tests/test_cli.py`
  * Added regression test verifying `show_execution` includes counters and custom state values in rendered output.

## Usage/Configuration
```bash
stent execution show <execution-id>
```

The output now includes `Counters` and `Custom State` sections when available.
