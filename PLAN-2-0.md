# Stent Implementation Plan - 2.0

This plan turns the identified review items into an execution-ready roadmap with clear sequencing, file-level scope, test strategy, and completion criteria.

## Objectives

- Resolve correctness issues that can cause wrong runtime behavior (P0).
- Improve operability and runtime efficiency for workers and CLI workflows (P1).
- Reduce long-term maintenance cost with type and architecture cleanup (P2).
- Keep behavior changes covered by targeted tests before full-suite verification.

---

## Delivery Strategy

- Work in small, mergeable changesets by priority.
- For each item: code change -> targeted tests -> full regression (`pytest -q`) -> type check (`pyrefly check`).
- Avoid broad refactors until P0 and P1 are stable.
- Document all behavior changes in a new `features/XXX-*.md` entry when implementation is complete.

---

## Phase P0 - Correctness Fixes (Blockers)

### P0.1 Retry Delay Scheduling Semantics

**Problem**
- Retries are set back to `pending` with `lease_expires_at` delay, but claim logic checks `lease_expires_at` only for `running` tasks. This can cause immediate retry execution instead of delayed retry.

**Files**
- `stent/executor.py`
- `stent/backend/sqlite.py`
- `stent/backend/postgres.py`

**Implementation**
- In retry path, set:
  - `task.state = "pending"`
  - `task.scheduled_for = now + retry_delay`
  - clear worker lease ownership fields (`worker_id`, `lease_expires_at`, `started_at`).
- Ensure both backend claim queries continue honoring:
  - `scheduled_for IS NULL OR scheduled_for <= now`.
- Confirm no legacy logic still treats pending retries via `lease_expires_at`.

**Tests**
- Add test that a retrying task is not claimable before `scheduled_for`.
- Add test that task becomes claimable after scheduled time.
- Run existing retry tests for regression.

**Acceptance Criteria**
- Retries execute no earlier than computed retry delay.
- Existing retry behavior (attempt counts, error propagation) remains unchanged.

---

### P0.2 Idempotency Falsey Hit Handling

**Problem**
- One code path checks `if stored_val:` for idempotency result; falsey payloads can be treated as misses.

**Files**
- `stent/executor.py`

**Implementation**
- Replace truthy checks with explicit `is not None` for cache/idempotency payload retrieval.
- Audit nearby checks for consistency and use the same null-check pattern.

**Tests**
- Add cases where durable function returns falsey values (`0`, `False`, `""`, `[]`, `{}`) with idempotency enabled.
- Verify second call uses stored idempotency result and does not re-execute task.

**Acceptance Criteria**
- All falsey serialized results are treated as valid idempotency/cache hits.

---

### P0.3 Notification Wait Timeout Behavior

**Problem**
- Redis notification subscriptions swallow timeout and the caller may fetch and return still-pending task state rather than raising timeout.

**Files**
- `stent/notifications/redis.py`
- `stent/executor.py`

**Implementation**
- Make timeout explicit at notification boundary:
  - either re-raise `asyncio.TimeoutError`, or
  - yield a terminal timeout signal handled by caller.
- In `_wait_for_task_internal`, convert timeout into `ExpiryError` consistently.
- Ensure both notification mode and polling mode have matching timeout semantics.

**Tests**
- Add test using notification backend stub where no completion arrives before expiry.
- Verify `wait_for` and task wait functions raise `ExpiryError`.

**Acceptance Criteria**
- Expired waits always fail with timeout error, never with stale pending task state.

---

### P0.4 Cancelled Execution Terminal-State Consistency

**Problem**
- `wait_for` treats `cancelled` as terminal, but `result_of` does not, causing inconsistent behavior.

**Files**
- `stent/executor.py`

**Implementation**
- Add `cancelled` to terminal states in `result_of`.
- Define expected returned error/result shape for cancelled executions and keep it consistent with failed/timed_out handling.

**Tests**
- Add/adjust tests for cancelled execution path through `wait_for` and `result_of`.

**Acceptance Criteria**
- `wait_for` and `result_of` agree on cancellation terminal behavior.

---

## Phase P1 - Operability and Performance Improvements

### P1.1 Worker Tag Filtering End-to-End

**Problem**
- `serve(..., tags=...)` passes tags to claim API but claim queries ignore them.

**Files**
- `stent/backend/sqlite.py`
- `stent/backend/postgres.py`
- (if needed) helper utilities for tag matching

**Implementation**
- Implement tag filter semantics for both backends:
  - define clear matching rule (recommended: worker tags intersects task tags, with empty task tags as globally eligible).
- Keep queue and schedule filtering behavior unchanged.

**Tests**
- Multi-worker tests with disjoint tags.
- Ensure untagged tasks still route as expected.

**Acceptance Criteria**
- Workers only claim tasks eligible by tag policy.

---

### P1.2 CLI Backend Lifecycle Management

**Problem**
- CLI creates backend/executor but does not explicitly initialize or close backend resources.

**Files**
- `stent/cli.py`

**Implementation**
- Ensure `await backend.init_db()` before command execution.
- Ensure `await backend.close()` in `finally` block.
- Preserve current exit codes and command behavior.

**Tests**
- Add CLI-level async tests with backend spies/fakes if available.
- Manual verification for both SQLite and Postgres modes.

**Acceptance Criteria**
- No leaked backend resources after CLI command exits.

---

### P1.3 Replace CLI Full Scans with Count APIs

**Problem**
- CLI stats executes broad list scans (`limit=10000`) to count records.

**Files**
- `stent/backend/base.py`
- `stent/backend/sqlite.py`
- `stent/backend/postgres.py`
- `stent/cli.py`

**Implementation**
- Extend backend protocol with:
  - execution count by state
  - dead-letter count
- Implement SQL count queries in both backends.
- Update CLI stats to use counts directly.

**Tests**
- Backend count correctness tests.
- CLI stats output tests for representative states.

**Acceptance Criteria**
- CLI stats no longer depends on large list scans for simple counts.

---

### P1.4 Duration Parser Validation Hardening

**Problem**
- Current parser can accept partial matches from malformed strings.

**Files**
- `stent/utils/time.py`
- related tests

**Implementation**
- Enforce full-string parse (recommended: anchored regex or iterative consume-to-end parsing).
- Reject malformed values cleanly with `ValueError`.
- Preserve existing valid syntax support.

**Tests**
- Positive cases: `"10s"`, `"1h30m"`, `"2d8h"`, dict/timedelta inputs.
- Negative cases: invalid unit, trailing junk, empty string, unsupported formats.

**Acceptance Criteria**
- Only fully valid duration strings are accepted.

---

## Phase P2 - Maintainability and Quality Improvements

### P2.1 UTC Utility Consistency

**Problem**
- `now_utc()` name implies UTC but returns naive local time.

**Files**
- `stent/utils/time.py`
- any call sites using `now_utc()`

**Implementation Options**
- Option A (preferred): return timezone-aware UTC datetime.
- Option B: rename function to reflect local/naive behavior.

**Acceptance Criteria**
- Naming and behavior are consistent and documented.

---

### P2.2 Typecheck Hygiene and Dependency Scope

**Problem**
- `pyrefly check` reports errors; `pyrefly` is also listed in runtime deps.

**Files**
- `pyproject.toml`
- `stent/cli.py`
- `stent/executor.py`
- tests/examples flagged by type checker

**Implementation**
- Move `pyrefly` to dev dependency only.
- Resolve high-value type issues in core library modules first.
- Reduce or remove suppressions in production code paths.

**Acceptance Criteria**
- Type checker passes for core package; suppressions reduced.

---

### P2.3 Incremental Executor Split (No Behavior Change)

**Problem**
- `executor.py` is too large and mixes many responsibilities.

**Files**
- `stent/executor.py`
- new module files under `stent/` (to be introduced)

**Implementation**
- Extract in stages behind same public API:
  1. wait/poll helpers
  2. signal handling
  3. worker lifecycle and task handling
  4. dispatch/map orchestration helpers
- Keep `Stent` external API unchanged.

**Acceptance Criteria**
- No public API change; all tests pass; easier module-level ownership.

---

### P2.4 Backend Deduplication

**Problem**
- SQLite and Postgres backends duplicate mapping/serialization/query patterns.

**Files**
- `stent/backend/sqlite.py`
- `stent/backend/postgres.py`
- `stent/backend/utils.py`
- optional new shared backend helper module

**Implementation**
- Extract shared row mapping and policy serialization helpers.
- Keep database-specific SQL and transaction semantics local to each backend.

**Acceptance Criteria**
- Reduced duplication without hiding backend-specific behaviors.

---

## Test and Verification Plan

### Per-Item Validation
- Run narrow tests for touched behavior first.
- Add regression tests for each bug fixed.

### Full Validation Gate
- `pytest -q`
- `pyrefly check`

### Suggested Targeted Test Groupings
- Retry/idempotency/timeout/cancel: `tests/test_execution.py`, `tests/test_wait_for.py`, `tests/test_map.py`, `tests/test_signals.py`
- Backend behavior: `tests/test_backend_correctness.py`
- Duration parsing: existing time/scheduling tests (and new parser tests)

---

## Rollout Order and Milestones

### Milestone 1 (P0 Complete)
- P0.1-P0.4 merged with tests.
- Full test suite green.

### Milestone 2 (P1 Complete)
- P1.1-P1.4 merged with tests.
- CLI behavior and stats verified on SQLite and Postgres.

### Milestone 3 (P2 Complete)
- Type and structure improvements merged incrementally.
- No regressions in behavior or public API.

---

## Risk Register and Mitigations

- **Retry scheduling semantic drift**
  - Mitigation: explicit regression tests around not-before scheduling and attempt counts.
- **Cross-backend behavior divergence**
  - Mitigation: mirror test scenarios for SQLite and Postgres paths where feasible.
- **Refactor-induced regressions in executor split**
  - Mitigation: behavior-preserving extraction only; avoid logic changes during file moves.
- **Type cleanup scope creep**
  - Mitigation: prioritize core package typing; defer low-impact example typing.

---

## Definition of Done

- All P0 items completed with regression tests.
- P1 items merged with clear user-facing behavior improvements.
- P2 items completed or explicitly deferred with rationale.
- `pytest -q` passes.
- `pyrefly check` passes for intended scope.
- A new feature note is added in `features/` following project numbering convention.
