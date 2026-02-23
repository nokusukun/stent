# Typecheck Hygiene and Dev Dependency Scope

## Description
Completed the typecheck hygiene pass for core package code and moved `pyrefly` out of runtime dependencies so it is dev-only tooling.

## Key Changes
* `pyproject.toml`
  * Removed `pyrefly` from `[project].dependencies`.
  * Kept `pyrefly` under `[dependency-groups].dev`.
* `stent/cli.py`
  * Adjusted Windows ANSI setup typing around `ctypes.windll` access.
  * Added explicit dict type annotations for stats counters.
* `stent/executor.py`
  * Added explicit cast in orchestrator failure update path to satisfy static typing.
* `tests/test_wait_for.py`
  * Added casts for test doubles/monkeypatches used by type checker.
* `examples/research_agent.py`
  * Tightened `asyncio.gather(..., return_exceptions=True)` result handling and list typing.

## Usage/Configuration
```bash
# Runtime install no longer includes pyrefly.
# Typecheck in dev environments:
pyrefly check
```
