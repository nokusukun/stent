# Stent Production Hardening Plan - 3.0

This plan turns the latest architecture review into a production-focused execution roadmap, based on confirmed decisions and scale targets.

## Confirmed Decisions

- `expiry=0` means no timeout (wait indefinitely).
- Signal semantics are latest-value-wins for the same `(execution_id, signal_name)`.
- Workload target for `Stent.map(...)` is up to ~300,000 items.
- Input is trusted (internal), not multi-tenant adversarial.
- Time model should be naive UTC.
- Wall clock timing is acceptable (no monotonic/SLO precision requirements).

## Open Product/Behavior Decision

- Retry policy persistence for `retry_for` exception classes across restarts is not yet decided.
  - Current state: DB row serialization helpers preserve numeric backoff fields but not `retry_for` names.
  - Impact: retry behavior can drift to defaults after process restart depending on how tasks are reloaded.

---

## Objectives

- Make runtime behavior explicit and documented for timeout/signal semantics.
- Ensure system can handle very large fan-out (`map`) without memory/DB pressure spikes.
- Close reliability gaps that can create behavior drift after restart.
- Keep API compatibility stable while improving operational safety.

---

## Phase P3.1 - Contract Clarity and Guardrails

### P3.1.1 Timeout Contract Documentation (`expiry=0`)

**Goal**
- Make timeout semantics explicit and test-protected.

**Changes**
- Document in code/docs that:
  - `expiry is None` -> no timeout
  - `expiry == 0` -> no timeout (legacy/intentional)
  - `expiry > 0` -> bounded wait
- Add explicit tests to lock this in for both task wait and execution wait.

**Files**
- `stent/executor_wait.py`
- `tests/test_wait_for.py`
- docs/readme location where wait behavior is described

**Acceptance**
- Behavior is specified and covered by tests.

---

### P3.1.2 Signal Contract Documentation (Latest-Value-Wins)

**Goal**
- Make signal overwrite behavior an intentional, discoverable contract.

**Changes**
- Document that same-name signals overwrite previous payload for an execution.
- Add tests demonstrating:
  - two sends before consumption -> consumer receives last value
  - send-before-wait and send-during-wait both align with latest-value behavior
- Optional: add debug log when an existing signal key is overwritten.

**Files**
- `stent/executor_signals.py`
- backend signal upsert paths in:
  - `stent/backend/sqlite.py`
  - `stent/backend/postgres.py`
- `tests/test_signals.py`

**Acceptance**
- Latest-value-wins behavior is stable, documented, and regression-tested.

---

## Phase P3.2 - Map Scalability for 300k Fan-Out

### P3.2.1 Chunked Scheduling and Waiting

**Problem**
- Current map path materializes full argument/results structures and can produce write-amplification with large item counts.

**Goal**
- Bound memory and DB load while preserving return ordering and behavior.

**Changes**
- In `execute_map_batch`:
  - Process input in configurable chunks (default recommendation: 1000).
  - For each chunk:
    - evaluate cache/idempotency
    - batch create tasks
    - await completion for chunk
    - store chunk results in final array at stable indices
- Preserve output order exactly.
- Keep permit release semantics safe across chunk boundaries.

**Files**
- `stent/executor_orchestration.py`
- optional executor config surface if chunk size is exposed

**Tests**
- Add high-volume-ish deterministic tests (thousands scale in CI) verifying:
  - order stability
  - cache/idempotency hits mixed with scheduled tasks
  - no behavior change for failures/exception propagation

**Acceptance**
- No unbounded in-memory growth with large inputs.
- Existing `map` behavior remains functionally equivalent.

---

### P3.2.2 Progress Write Throttling for Batch Map

**Goal**
- Reduce excessive per-item progress writes under massive fan-out.

**Changes**
- Replace per-task dispatched progress writes with either:
  - per-chunk aggregate progress entries, or
  - configurable sampling.
- Ensure observability remains useful without N=300k insert pressure.

**Files**
- `stent/executor_orchestration.py`
- optionally backend progress helper if batching is introduced

**Acceptance**
- Significant reduction in progress rows for large map jobs with no loss of terminal correctness.

---

## Phase P3.3 - Retry Policy Persistence Consistency

### P3.3.1 Decision + Implementation

**Decision Needed**
- Should `retry_for` be persisted for backend task rows so retries behave identically across restarts?

**Recommendation**
- Yes, persist class names in DB JSON similarly to dead-letter serialization path.

**If Accepted, Changes**
- Extend `retry_policy_to_json` / `retry_policy_from_json` to include `retry_for` class names.
- Reuse exception-name-to-class resolution already used in JSON dead-letter helpers.
- Backward compatibility:
  - missing `retry_for` defaults to `[Exception]`.

**Files**
- `stent/backend/utils.py`
- `stent/backend/sqlite.py`
- `stent/backend/postgres.py`
- tests in `tests/test_backend_correctness.py`

**Acceptance**
- Retry behavior does not drift after restart for custom retry policies.

---

## Phase P3.4 - Time Model Normalization (Naive UTC)

### P3.4.1 Unified Naive-UTC Time Helpers

**Problem**
- Current code mixes direct `datetime.now()` calls and a timezone-aware `now_utc()` utility.

**Goal**
- Standardize all persisted/comparison times to naive UTC.

**Changes**
- Introduce/adjust utility helper for naive UTC now (e.g. `datetime.utcnow()` wrapper).
- Replace critical scheduler/lease/timeout persistence writes to use the same helper.
- Confirm backend row parsing remains consistent and does not attach local timezone assumptions.

**Files**
- `stent/utils/time.py`
- `stent/executor.py`
- `stent/executor_worker.py`
- `stent/executor_orchestration.py`
- `stent/executor_signals.py`
- `stent/backend/utils.py`

**Acceptance**
- No mixed aware/naive datetimes in core flows.
- Persisted timestamps follow one documented convention: naive UTC.

---

## Phase P3.5 - Internal Safety Guardrails

### P3.5.1 Query Builder Guardrails

**Goal**
- Prevent accidental future misuse of shared SQL query builders.

**Changes**
- Add internal assertions/comments stating `table` and `order_by` inputs must be static constants.
- Keep API internal (module-scoped usage only).

**Files**
- `stent/backend/utils.py`

**Acceptance**
- Reduced risk of unsafe dynamic SQL reuse in future changes.

---

## Test and Verification Plan

### Targeted Tests Per Phase
- Wait semantics: `tests/test_wait_for.py`
- Signal semantics: `tests/test_signals.py`
- Map behavior/perf-sensitive correctness: `tests/test_map.py`, `tests/test_execution.py`
- Backend serialization/mapping: `tests/test_backend_correctness.py`
- CLI impacts from counts/progress if touched: `tests/test_cli.py`

### Full Gate
- `pytest -q`
- `pyrefly check`

### Optional Perf Validation (recommended)
- Add a non-CI benchmark script for map chunk sizes (`1k`, `5k`, `10k`) and memory profile.

---

## Rollout Order

1. P3.1 contracts/tests (low risk, clarifies expected behavior)
2. P3.2 map scalability (highest production impact)
3. P3.3 retry persistence consistency (correctness across restarts)
4. P3.4 time normalization (consistency cleanup)
5. P3.5 guardrails/documentation polish

---

## Risks and Mitigations

- **Map refactor changes behavior**
  - Mitigation: lock ordering/error semantics with regression tests before chunking.
- **Retry serialization backward compatibility**
  - Mitigation: tolerant deserializer defaults; include mixed old/new payload tests.
- **Time normalization regressions**
  - Mitigation: change in narrow slices; verify sorting/claiming/expiry tests after each slice.

---

## Definition of Done

- Signal and timeout contracts are explicit and test-covered.
- Map path supports large fan-out without unbounded memory growth.
- Retry policy persistence behavior is explicitly decided and implemented.
- Naive-UTC time model is documented and consistent in core flows.
- `pytest -q` and `pyrefly check` pass.
- Feature documentation added for each completed slice in `features/`.
