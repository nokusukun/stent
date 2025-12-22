# DFns Project Roadmap & TODOS

This document tracks planned improvements, feature requests, and architectural changes for the DFns durable execution framework.

## 1. Backends & Storage

### Production-Grade Backends
- [ ] **PostgreSQL Backend:** Implement a backend using `asyncpg` or `SQLAlchemy` (async). SQLite is great for dev/testing, but Postgres is essential for production concurrency and reliability (SKIP LOCKED support).
- [ ] **Redis Backend (State):** Implement a pure Redis backend for high-throughput, lower-latency use cases where long-term durability is less critical or provided by Redis persistence (AOF/RDB).
- [ ] **DynamoDB Backend:** Serverless-friendly backend for AWS deployments.

### Optimizations
- [ ] **Connection Pooling:** Ensure backends (especially SQL ones) manage connection pools efficiently.
- [ ] **Payload Offloading:** Support offloading large payloads (args/results) to blob storage (S3, GCS) instead of storing them directly in the database row.
- [ ] **Compression:** Optional compression (zlib/zstd) for stored payloads to save space.

## 2. Core Workflow Features

### Orchestration Patterns
- [x] **Fan-out / Fan-in:** Implement a first-class `dfns.gather()` or `dfns.map()` to parallelize tasks efficiently and wait for all results.
- [ ] **Child Workflows:** specialized API to start a workflow from within another and wait for its completion as a single atomic step (handling cancellation propagation).
- [ ] **External Signals/Events:** API to pause a workflow until an external event (e.g., webhook, human approval) is received (`await dfns.wait_for_signal("approval")`).
- [ ] **Cron / Scheduled Triggers:** Native support for recurring workflows (e.g., `@DFns.durable(schedule="@daily")`).
- [x] **Rate Limiting:** Global/Cluster-wide rate limiting for specific tasks or queues (e.g., "max 10/s for `send_email`") or `max_concurrent=2` for `use_gpu` that only allows n durable functions to run at a time.

### Advanced Logic
- [ ] **Continue-As-New:** Mechanism to restart a workflow with new arguments, clearing history to prevent unbounded growth for infinite loops.
- [ ] **Sagas / Compensation:** Built-in decorators or context managers to define compensation logic for undoing operations on failure (`try...except...compensate`).

## 3. Reliability & Performance

- [ ] **Worker Autoscale Hooks:** Signals or metrics to help external scalers (K8s HPA) know when to add more workers (based on queue depth/latency).
- [ ] **Sticky Queues:** Optimization to cache workflow state on a specific worker and route task updates there to avoid database re-fetches.
- [ ] **Deadlock Detection:** Better detection of stuck workflows or circular dependencies.
- [ ] **Backpressure:** Mechanism for workers to reject tasks if overloaded.

## 4. Observability & Operations

- [ ] **Web Dashboard:** A UI to search executions, view their state (timeline/Gantt chart), inspect payloads, and retry/cancel manually.
- [ ] **Structured Logging:** Switch to JSON logging by default for production environments.
- [ ] **Metrics:** Emit Prometheus/StatsD metrics:
    - `dfns_tasks_started_total`
    - `dfns_tasks_failed_total`
    - `dfns_queue_depth`
    - `dfns_e2e_latency_seconds`
- [ ] **OpenTelemetry Tracing:** Instrument the executor to emit spans for each task/workflow, linking distributed traces across services.
- [ ] **CLI Tool:** A `dfns` CLI for common ops:
    - `dfns list`
    - `dfns show <id>`
    - `dfns cancel <id>`
    - `dfns retry <id>`

## 5. Developer Experience

- [ ] **Packaging:** Create `pyproject.toml` and configure build system (Hatch/Poetry/Setuptools) for PyPI distribution.
- [ ] **Type Stubs:** Ensure full PEP 561 compliance (typed library).
- [ ] **Local Dev Server:** A standalone server mode (like `temporal server start-dev`) that runs an in-memory or SQLite backend + UI for easy local development.
- [ ] **Debugger Support:** Investigate if we can attach a debugger to a replay of a workflow execution.

## 6. Testing & Quality Assurance

- [ ] **Determinism Checker:** A test runner mode that verifies workflow code is deterministic (e.g., warns on `random.random()` usage inside orchestrator).
- [ ] **Simulation Testing:** A deterministic simulation engine (handling time skipping) to run long scenarios (e.g., "run for 10 years") in milliseconds to catch edge cases.
- [ ] **Stress/Load Benchmarks:** Standard scripts to measure throughput (tasks/sec) on different backends.
