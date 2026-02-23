# Hardening Plan for Stent

This plan turns the findings in `REVIEW.md` into an actionable implementation checklist. Read `REVIEW.md` first for context, rationale, and file references.

## Goals
- Make core execution/task lifecycle data-consistent.
- Eliminate duplicate execution under concurrency and long-running tasks.
- Improve operability (shutdown, observability, DLQ tooling).
- Clarify and harden public API behavior (DX).

## Phase 0 — Validation & Baseline (1–2 days)
- Confirm current expected behavior via existing tests.
- Add/identify load-style tests for claim loop and waiter polling.
- Define “production minimum” target: supported backend(s), notification requirements, durability semantics.

## Phase 1 — Correctness First (Critical) (3–7 days)

### Transactional integrity
- Add backend-level transaction support, or a single atomic method:
  - `create_execution_with_root_task(record, task)`
- Ensure failure cannot create orphaned executions or tasks.
- Add regression tests for partial failures.

### Task claim correctness (SQLite)
- Fix race conditions in `claim_next_task` (locking/atomic claim).
- Ensure concurrency limit checks are consistent with claim semantics.
- Add stress test running N workers claiming from SQLite.

### Schema indexes
- Add missing indexes for hot query paths in both backends.
- Validate query plans (Postgres `EXPLAIN`, SQLite query analysis if available).

## Phase 2 — Execution Semantics & Leases (High) (3–7 days)

### Lease renewal (heartbeat)
- Add a lease-renewal mechanism for long-running task execution.
- Define heartbeat interval vs lease duration defaults.
- Ensure reclaim only happens when heartbeat stops.
- Add tests for: long activity > lease, renewal keeps ownership.

### Waiting strategy (reduce polling)
- Make polling adaptive (backoff) when no notification backend is configured.
- Document production recommendation: Redis notifications strongly preferred.
- Add config knobs: poll min/max, backoff strategy.

### Remove global `asyncio.sleep` patching
- Replace with safer approach (see options in `REVIEW.md`).
- Add tests ensuring third-party asyncio code is not affected.

## Phase 3 — DX Improvements (2–5 days)

### Fail-fast errors
- Make `dispatch()` and internal scheduling fail fast when functions are unregistered.
- Decide explicit contract for `wrap()`:
  - Remove it, require `@durable`, or implement supported dynamic registration.

### Registry scoping
- Allow `FunctionRegistry` injection per executor.
- Ensure tests can isolate registries without global state bleed.

### Clarify expiry/delay semantics
- Define expiry relative to dispatch vs scheduled start.
- Update docs/examples accordingly.

## Phase 4 — Operability (2–7 days)

### Graceful worker shutdown/draining
- Add a stop/drain signal to `serve()`.
- Expose “ready” and “draining” statuses for orchestrators (K8s readiness/liveness use).

### Dead-letter tooling
- Store structured DLQ payloads (not `str(task)`).
- Add basic APIs and CLI commands:
  - list DLQ
  - inspect DLQ item
  - replay / requeue

### Observability
- Provide structured logging with execution_id/task_id context.
- Ensure OpenTelemetry instrumentation is optional and import-safe.
- Add metrics hooks (even if only interfaces initially).

## Definition of Done
- All tests pass (existing + new regression/stress tests).
- SQLite claim loop proven safe under concurrency.
- No orphaned executions/tasks on partial failures.
- Lease renewal prevents duplicate execution for long tasks.
- Public API fails fast on misconfiguration.
- Clear production guidance documented (recommended backend, notifications, serializer safety).
