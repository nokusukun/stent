# Stent Hardening Plan - v2 (Post-Initial Hardening)

This plan addresses remaining issues identified in `REVIEW.md` (v2). The critical issues from v1 have been resolved; this phase focuses on polish, performance, and code quality.

## Goals
- Eliminate remaining HIGH priority production risks
- Fix type errors for clean static analysis
- Improve exception handling fidelity
- Document production deployment requirements

---

## Phase 1 — HIGH Priority Fixes (3-5 days)

### 1.1 SQLite Connection Pooling
**Files**: `stent/backend/sqlite.py`

**Current**: Opens new connection per operation
**Target**: Persistent connection or small pool

**Tasks**:
- [ ] Create connection pool or persistent connection in `__init__`
- [ ] Add `async def close(self)` method
- [ ] Ensure thread-safety with SQLite's single-writer model
- [ ] Add connection health check/reconnect logic
- [ ] Benchmark before/after

**Estimated**: 1-2 days

---

### 1.2 Postgres Pool Lifecycle
**Files**: `stent/backend/postgres.py`

**Tasks**:
- [ ] Add `async def close(self)` to close pool
- [ ] Document that `init_db()` must be called before use
- [ ] Consider lazy pool initialization on first operation
- [ ] Add pool configuration options (min/max connections)

**Estimated**: 0.5 days

---

### 1.3 Clock Skew Documentation & Mitigation
**Files**: `stent/executor.py`, docs

**Tasks**:
- [ ] Add production documentation requiring NTP sync
- [ ] Add optional `use_db_time` parameter to backends
- [ ] Implement DB-time fetch for Postgres (`SELECT NOW()`)
- [ ] Implement DB-time fetch for SQLite (`SELECT datetime('now')`)
- [ ] Add clock drift warning when worker time differs significantly from DB

**Estimated**: 1-2 days

---

## Phase 2 — Type Safety & Code Quality (2-3 days)

### 2.1 Fix executor.py Type Errors
**Files**: `stent/executor.py`

**Tasks**:
- [ ] Fix `bytes | None` passed to `loads()` - add null checks
- [ ] Fix line 675 awaitable issue
- [ ] Remove all `pyrefly: ignore` comments with proper fixes
- [ ] Add type guards where needed

**Example fix**:
```python
# Before
res = executor.serializer.loads(completed.result)

# After
if completed.result is not None:
    res = executor.serializer.loads(completed.result)
else:
    res = None
```

**Estimated**: 1 day

---

### 2.2 Fix postgres.py Type Errors
**Files**: `stent/backend/postgres.py`

**Tasks**:
- [ ] Fix `PoolConnectionProxy` vs `Connection` type hints
- [ ] Use `asyncpg.Connection` protocol or `Any` with documentation

**Estimated**: 0.5 days

---

### 2.3 Fix telemetry.py Optional Import
**Files**: `stent/telemetry.py`

**Tasks**:
- [ ] Properly handle optional opentelemetry import for type checker
- [ ] Use `TYPE_CHECKING` guard for type hints
- [ ] Ensure graceful degradation when otel not installed

**Example**:
```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opentelemetry import trace
    from opentelemetry.trace import Status, StatusCode

_HAS_OTEL = False
try:
    from opentelemetry import trace
    from opentelemetry.trace import Status, StatusCode
    _HAS_OTEL = True
except ImportError:
    pass
```

**Estimated**: 0.5 days

---

## Phase 3 — Exception Handling & Serialization (1-2 days)

### 3.1 Improve Exception Serialization
**Files**: `stent/utils/serialization.py`

**Tasks**:
- [*] Include traceback in JSON-serialized exceptions
- [*] Preserve custom exception attributes where possible
- [*] Add `__traceback__` field to serialized form

**Target format**:
```python
{
    "__type__": "Exception",
    "cls": "ValueError",
    "message": "invalid input",
    "traceback": "Traceback (most recent call last):\n  File ...",
    "attributes": {"custom_field": "value"}
}
```

**Estimated**: 0.5 days

---

### 3.2 RetryPolicy.retry_for Serialization
**Files**: `stent/utils/serialization.py`, `stent/backend/utils.py`

**Tasks**:
- [*] Serialize `retry_for` as list of class names
- [*] Add registry of known exception classes for deserialization
- [*] Fall back to `(Exception,)` for unknown classes
- [*] Document limitation for custom exceptions

**Estimated**: 0.5 days

---

## Phase 4 — Operability Polish (1-2 days)

### 4.1 Dead Letter Cleanup
**Files**: `stent/backend/sqlite.py`, `stent/backend/postgres.py`, `stent/backend/base.py`

**Tasks**:
- [ ] Add `cleanup_dead_letters(older_than: datetime) -> int` to Backend protocol
- [ ] Implement in both backends
- [ ] Optionally include in `cleanup_executions()` or keep separate

**Estimated**: 0.5 days

---

### 4.2 Progress Table Management
**Files**: `stent/backend/sqlite.py`, `stent/backend/postgres.py`

**Tasks**:
- [ ] Add progress retention option (max entries per execution)
- [ ] Add progress compaction in cleanup routine
- [ ] Consider summary progress for completed executions

**Estimated**: 0.5 days

---

### 4.3 CLI Improvements
**Files**: `stent/cli.py`

**Tasks**:
- [ ] Add explicit `--backend sqlite|postgres` flag
- [ ] Add `--offset` for DLQ pagination
- [ ] Add `dlq discard` command
- [ ] Add `dlq stats` command (count by reason/step)

**Estimated**: 0.5 days

---

## Phase 5 — Documentation (1 day)

### 5.1 Production Deployment Guide
**Tasks**:
- [ ] Document NTP/clock sync requirements
- [ ] Document recommended Postgres configuration
- [ ] Document SQLite limitations (dev/test only)
- [ ] Document Redis notification benefits
- [ ] Add example K8s deployment with health probes

---

### 5.2 API Documentation
**Tasks**:
- [ ] Document `@durable()` vs instance registry behavior
- [ ] Document expiry semantics (with/without delay)
- [ ] Document pickle security considerations
- [ ] Add migration guide from v1

---

## Phase 6 — Code Quality (Optional, 2-3 days)

### 6.1 Backend Refactoring
**Files**: `stent/backend/sqlite.py`, `stent/backend/postgres.py`

**Tasks**:
- [ ] Extract shared row-mapping logic to `backend/utils.py`
- [ ] Create `BaseBackend` with common methods
- [ ] Reduce duplication (~40% code shared)

**Estimated**: 1-2 days

---

### 6.2 Test Coverage
**Tasks**:
- [ ] Add tests for clock skew scenarios
- [ ] Add tests for connection pool exhaustion
- [ ] Add tests for exception serialization round-trip
- [ ] Add integration tests with real Postgres

**Estimated**: 1-2 days

---

## Summary

| Phase | Priority | Effort | Impact |
|-------|----------|--------|--------|
| 1. HIGH fixes | Critical | 3-5 days | Production safety |
| 2. Type safety | Medium | 2-3 days | Code quality, CI |
| 3. Exception handling | Medium | 1-2 days | Debugging experience |
| 4. Operability | Low | 1-2 days | Operations polish |
| 5. Documentation | Medium | 1 day | User experience |
| 6. Refactoring | Low | 2-3 days | Maintainability |

**Minimum viable**: Phases 1-2 (5-8 days)
**Recommended**: Phases 1-5 (8-13 days)
**Complete**: All phases (10-16 days)

---

## Definition of Done

### Phase 1 Complete
- [ ] SQLite uses persistent connection; benchmarks show improvement
- [ ] Postgres backend has `close()` method
- [ ] Clock skew documented; optional DB-time implemented

### Phase 2 Complete
- [ ] Zero type errors with pyrefly/pyright
- [ ] All `pyrefly: ignore` comments removed

### Phase 3 Complete
- [ ] Exceptions include traceback in JSON serialization
- [ ] RetryPolicy round-trips correctly

### Phase 4 Complete
- [ ] Dead letters cleaned up with executions
- [ ] CLI has pagination and improved UX

### Phase 5 Complete
- [ ] Production deployment guide published
- [ ] All API edge cases documented

### Full Hardening Complete
- [ ] All tests pass
- [ ] Type checker clean
- [ ] Documentation complete
- [ ] Backend code deduplicated
