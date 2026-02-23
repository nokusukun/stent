# Stent Library Review (Principal Engineer) - Post-Hardening

## Scope
This review focuses on architecture, developer experience (DX), and production readiness risks after the hardening work.

## Executive Summary
Stent is a distributed durable-functions library for Python (conceptually similar to Temporal / Azure Durable Functions). Since the initial review, significant hardening work has been completed. The codebase now addresses the most critical production issues:

**Fixed Since Last Review:**
- Transactional integrity via `create_execution_with_root_task()` (executor.py:673, sqlite.py:191, postgres.py:190)
- SQLite claim race fixed with `BEGIN IMMEDIATE` + atomic claim (sqlite.py:350, 389-405)
- Database indexes added for hot query paths (sqlite.py:97-100, postgres.py:92-95)
- Lease renewal/heartbeat implemented (executor.py:1227-1258, 1336-1346)
- Global `asyncio.sleep` patching removed - now uses `_original_sleep` reference (executor.py:146)
- Adaptive polling with backoff (executor.py:1111-1118, 1174-1186)
- `UnregisteredFunctionError` for fail-fast DX (executor.py:52-60)
- Registry injection support (executor.py:167, 171)
- Graceful shutdown with `WorkerLifecycle` (executor.py:77-121)
- DLQ now stores structured JSON (backend/utils.py:59-84, sqlite.py:487-494)
- CLI for DLQ management (cli.py:54-83)
- Metrics protocol for observability (metrics.py)

**Verdict:**
- Prototyping / internal tooling: Ready
- Production workloads: Ready with caveats (see remaining issues below)
- High-scale production: Requires additional work

---

## What's Good

### Core Architecture
- **Backend abstraction** via `Backend` Protocol (backend/base.py:5) - clean separation
- **Intuitive DX** with `@Stent.durable()` decorator (executor.py:309-346)
- **Comprehensive feature set**: retries/backoff, leases, scheduling, queues/tags, caching, idempotency, signals
- **Connection pooling** in Postgres backend via `asyncpg.create_pool()` (postgres.py:19)
- **Atomic transactions** for execution+task creation (postgres.py:193-195, sqlite.py:193-200)

### Operability
- **WorkerLifecycle** for graceful shutdown coordination (executor.py:77-121)
- **Health status overview** via `worker_status_overview()` (executor.py:229-246)
- **DLQ tooling** with list/show/replay via CLI (cli.py:54-83)
- **Metrics protocol** for Prometheus/StatsD integration (metrics.py:6-42)
- **Structured logging** with context injection (executor.py:27-46)

### Safety
- **Lease heartbeat** prevents duplicate execution of long-running tasks (executor.py:1227-1258)
- **Atomic task claiming** in both backends (sqlite.py:350, postgres.py:356)
- **Proper indexes** on hot query paths (sqlite.py:97-100)

---

## Remaining Architecture / Design Concerns

### 1) Global Registry Singleton (MEDIUM)
File: `registry.py:41`
```python
registry = FunctionRegistry()
```

**Status**: Partially mitigated - registry injection is now supported (executor.py:167), but the default still uses a module-level singleton.

**Remaining Risks:**
- Test pollution across test modules if not explicitly using isolated registries
- Multiple `Stent` instances in same process share decorators by default

**Recommendation:**
- Document the isolation pattern explicitly
- Consider making `@Stent.durable()` instance-bound rather than class-bound for clearer scoping

### 2) `@Stent.durable()` is Class-Level, Registry is Instance-Level (MEDIUM)
File: `executor.py:309-346`
```python
@classmethod
def durable(cls, ...):
    reg = cls.default_registry  # Uses class-level registry
```

But `Stent.__init__` allows injecting a different registry:
```python
self.registry = function_registry or self.default_registry
```

**Risk**: Functions decorated with `@Stent.durable()` always register to the class-level `default_registry`, not the instance registry. This creates subtle bugs when using custom registries.

**Recommendation:**
- Either make `durable()` an instance method (breaking change)
- Or document this limitation clearly and provide an explicit `executor.register(fn, ...)` method for custom registries

### 3) Progress Table Unbounded Growth (LOW)
File: `executor.py:1332, 1387, 1434, etc.`

Every step appends to `execution_progress` table. Long-running workflows with many steps will accumulate unbounded progress records.

**Recommendation:**
- Add progress retention/compaction during cleanup
- Or cap progress entries per execution (e.g., keep last N)

---

## DX (Developer Experience) Issues

### 1) Sleep Semantics Could Be Confusing (LOW)
File: `executor.py:274-307`

`Stent.sleep()` is a static method that looks up `current_executor` from context. If called outside a durable context, it falls back to `asyncio.sleep`. This dual behavior could surprise users.

**Recommendation:**
- Consider raising an error outside durable context rather than silent fallback
- Or document the fallback behavior prominently

### 2) `wrap()` vs `durable()` Distinction Unclear (LOW)
File: `executor.py:348-360`

`wrap()` requires prior `@durable()` registration and raises `UnregisteredFunctionError` if not found. The relationship between these two APIs is not immediately clear.

**Recommendation:**
- Document when to use `wrap()` vs calling the decorated function directly
- Consider renaming to `call_registered()` or similar for clarity

### 3) CLI Backend Detection Heuristic (LOW)
File: `cli.py:112-115`
```python
if "://" in args.db or "postgres" in args.db:
    backend = Stent.backends.PostgresBackend(args.db)
else:
    backend = Stent.backends.SQLiteBackend(args.db)
```

Simple heuristic could misclassify paths containing "postgres" or "://".

**Recommendation:**
- Add explicit `--backend` flag: `--backend sqlite|postgres`
- Or use more robust detection (e.g., URL parsing)

---

## Production Pitfalls (Remaining)

### HIGH 1) SQLite Connection-Per-Operation Pattern
Files: `sqlite.py` (multiple methods)

Every SQLite operation opens a new connection:
```python
async with aiosqlite.connect(self.db_path) as db:
```

**Issue**: Under moderate load, this creates significant overhead and potential file locking contention.

**Recommendation:**
- Use a persistent connection or small connection pool
- Consider using a single `aiosqlite` connection with serialized access for SQLite's single-writer model

### HIGH 2) No Connection Pool Lifecycle Management for Postgres
File: `postgres.py:17-19`
```python
async def init_db(self):
    if not self.pool:
        self.pool = await asyncpg.create_pool(self.dsn)
```

**Issues:**
- Pool is created lazily on first `init_db()` call
- No explicit `close()` method to clean up the pool
- If `init_db()` isn't called, first operation will fail with `assert self.pool is not None`

**Recommendation:**
- Add explicit `async def close(self)` method
- Document that `init_db()` must be called before use
- Consider auto-initialization on first operation (lazy but safe)

### HIGH 3) Clock Skew Sensitivity
Files: `executor.py:959, 1285, sqlite.py:331-332, postgres.py:319-320`

Leases, scheduling, and expiry all use `datetime.now()` from the worker's perspective:
```python
if now is None:
    now = datetime.now()
expires_at = now + lease_duration
```

**Risk**: Workers with clock skew relative to each other or the DB can cause:
- Premature lease expiration (duplicate execution)
- Tasks remaining stuck (if worker clock is behind)

**Recommendation:**
- Document strict NTP requirements
- Consider adding a DB-time option: `SELECT CURRENT_TIMESTAMP` for Postgres, `SELECT datetime('now')` for SQLite
- At minimum, log warnings when significant clock drift is detected

### MEDIUM 1) Expiry Semantics Ambiguity
File: `executor.py:627-634`
```python
expiry_at = (datetime.now() + expiry) if expiry else None
if scheduled_for and expiry:
    # expiry_at is absolute from scheduled start
    expiry_at = scheduled_for + expiry
```

**Issue**: When both `delay` and `expiry` are set, expiry is relative to scheduled start. Without delay, expiry is relative to dispatch time. This inconsistency could confuse users.

**Recommendation:**
- Document this clearly
- Consider always making expiry relative to actual start time (when task begins running)

### MEDIUM 2) Redis Notification Reliability
File: `notifications/redis.py:23-54`

Redis pub/sub is fire-and-forget. If the subscriber isn't listening when the message is published, it's lost.

**Current mitigation**: The wait loop re-checks DB state after subscription ends.

**Remaining risk**: Race condition window between checking DB and subscribing where a notification could be missed.

**Recommendation:**
- Document this limitation
- Consider using Redis Streams for reliable delivery if needed
- The current polling fallback adequately handles this for most cases

### MEDIUM 3) Error Serialization Limitations
File: `utils/serialization.py:47-55`
```python
if isinstance(obj, Exception):
    return {
        "__type__": "Exception",
        "message": str(obj),
        "cls": obj.__class__.__name__
    }
```

**Issue**: Exception serialization loses:
- Stack trace
- Custom exception attributes
- Exception chaining (`__cause__`, `__context__`)

**Recommendation:**
- Include `traceback.format_exception()` output in serialized form
- Store exception attributes (for custom exceptions with data)
- Document that pickle serializer preserves more exception detail

### MEDIUM 4) Retry Policy `retry_for` Not Serializable in JSON
File: `utils/serialization.py:43-46`
```python
# retry_for is tricky in JSON as it contains classes.
# For simplicity in this demo, we might skip full serialization
```

**Issue**: `RetryPolicy.retry_for` (tuple of exception classes) is lost when using JSON serialization.

**Impact**: Tasks restored from DB with JSON serializer will have default `retry_for=(Exception,)`.

**Recommendation:**
- Document this limitation clearly
- Consider storing exception class names and re-resolving them on load
- Or always use pickle for retry_policy storage specifically

---

## Production Pitfalls (LOW Priority)

### LOW 1) Pickle Serializer Security
File: `utils/serialization.py:11-16`

Pickle allows arbitrary code execution. Using pickle with untrusted data is dangerous.

**Current Status**: JSON is the default, pickle is opt-in.

**Recommendation:**
- Add prominent warning in documentation
- Consider adding a `PickleSerializer(allow_untrusted=False)` flag that restricts unpickling

### LOW 2) Cleanup Doesn't Remove Dead Letters
File: `sqlite.py:534-547, postgres.py:512-530`

`cleanup_executions()` removes old executions, tasks, progress, and signals - but not dead letters.

**Recommendation:**
- Add `cleanup_dead_letters(older_than)` method
- Or include dead letters in execution cleanup

### LOW 3) `list_dead_tasks` Only Returns Recent Items
Files: `sqlite.py:503-511, postgres.py:482-489`

DLQ listing is limited and has no pagination beyond `limit`.

**Recommendation:**
- Add `offset` parameter for pagination
- Add filtering by step_name, queue, or time range

---

## Code Quality Issues

### 1) Type Annotations with `pyrefly: ignore` Comments
File: `executor.py:481, 484, 493-496, 760, 764, 831`

Multiple `pyrefly: ignore` comments suggest type checking issues.

**Recommendation:**
- Audit and fix these rather than suppressing
- Ensure `TaskRecord.result` is consistently `bytes | None` and handle accordingly

### 2) Duplicate DateTime Conversion Logic
Files: `sqlite.py:614-656` vs `backend/utils.py:23-32`

DateTime conversion logic is duplicated between SQLite row mapping and the utils module.

**Recommendation:**
- Consolidate datetime handling into `backend/utils.py`
- Reuse in both backends

### 3) Backend Code Duplication
Files: `sqlite.py` and `postgres.py`

Significant structural duplication between backends (~60% similar).

**Recommendation:**
- Extract shared logic into a base class or mixins
- Share row-to-record mapping logic

---

## Missing Production Features (Future Roadmap)

### 1) Workflow Cancellation
Current support is partial - `cancelling`/`cancelled` states exist but there's no public API to trigger cancellation.

### 2) Workflow Versioning
`FunctionMetadata.version` exists but isn't used for routing or migration.

### 3) Rate Limiting
No built-in rate limiting for activities beyond `max_concurrent` per step.

### 4) Distributed Tracing Integration
`telemetry.py` exists but integration guidance is missing.

### 5) Activity Timeouts
Individual activity timeout (separate from execution expiry) isn't implemented.

---

## Top Priority Fixes (Recommended Order)

1. **Add Postgres pool cleanup** - Add `close()` method to PostgresBackend
2. **Document clock skew requirements** - Add to production deployment guide
3. **Improve exception serialization** - Include traceback in JSON serialization
4. **Add dead letter cleanup** - Include in retention cleanup or separate API
5. **Fix type annotations** - Remove `pyrefly: ignore` suppressions properly
6. **SQLite connection pooling** - Consider persistent connection for better performance

---

## Summary

The codebase has improved significantly since the initial review. The critical production issues (transactional integrity, claim races, missing indexes, lease renewal) have been addressed. The library is now suitable for production use with awareness of the remaining caveats:

| Category | Status |
|----------|--------|
| Correctness | Good - transactions and locking are solid |
| Performance | Adequate - indexes added, but SQLite connection pattern could improve |
| Operability | Good - metrics, structured logging, CLI, graceful shutdown |
| DX | Good - clear errors, intuitive API |
| Observability | Adequate - metrics protocol exists, needs tracing guidance |

**Production Readiness: 8/10** (up from ~5/10 in initial review)

Remaining work is primarily polish, documentation, and edge case handling rather than fundamental architectural issues.
