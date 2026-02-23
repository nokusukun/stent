# CLI Backend Lifecycle

## Description
Updated the CLI runtime flow to explicitly initialize backend resources before executing commands and always close backend resources when command handling finishes (including error cases). This prevents leaked backend connections and aligns CLI behavior with the backend lifecycle contract.

## Key Changes
* `stent/cli.py`
  * Added `await backend.init_db()` before creating/using the executor.
  * Wrapped command dispatch in `try/finally` and added `await backend.close()` in `finally`.
  * Preserved existing command routing and exit-code behavior.
* `tests/test_cli.py`
  * Added regression tests verifying:
    * backend is initialized and closed on successful CLI command
    * backend is still closed when command execution raises

## Usage/Configuration
```bash
# No CLI flag changes are required.
# Lifecycle handling is automatic for all commands.
stent list
stent stats
stent dlq list
```
