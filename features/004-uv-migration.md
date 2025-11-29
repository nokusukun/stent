# Migrate to uv

## Description
Switched the project's dependency management and build system to `uv`. This modernizes the workflow, replacing manual virtual environment management and potentially requirements files.

## Key Changes
*   Added `pyproject.toml` to define project metadata and dependencies.
*   Added `uv.lock` to lock dependency versions for reproducible builds.
*   Added `redis` and `aiosqlite` as runtime dependencies.
*   Added `pytest` as a development dependency.

## Usage/Configuration
To install dependencies:
```bash
uv sync
```

To run tests:
```bash
uv run pytest
```

To run an example:
```bash
uv run python examples/simple_flow.py
```
