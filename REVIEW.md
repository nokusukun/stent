# Stent Library Review (Principal Engineer) - v2 Post-Hardening

## Scope
This review assesses the Stent library after the hardening work completed in v1. Focus areas: remaining production risks, code quality, and polish items.

## Executive Summary

Stent is a distributed durable-functions library for Python. The v1 hardening addressed critical production issues. The library is now **production-ready for moderate workloads** with awareness of the remaining caveats.

**Production Readiness: 8/10**

| Category | Score | Notes |
|----------|-------|-------|
| Correctness | 9/10 | Transactions, locking, leases all solid |
| Performance | 7/10 | Indexes added; SQLite connection pattern suboptimal |
| Operability | 8/10 | Metrics, logging, CLI, graceful shutdown |
| DX | 8/10 | Clear errors, intuitive API |
| Code Quality | 6/10 | Type errors, duplication, optional deps |

---

## What's Working Well

### Correctness (Fixed in v1)
- **Transactional integrity**: `create_execution_with_root_task()` prevents orphans (executor.py:673-678)
- **Atomic task claiming**: SQLite uses `BEGIN IMMEDIATE` + `RETURNING` (sqlite.py:350, 389-405)
- **Postgres row locking**: `FOR UPDATE SKIP LOCKED` (postgres.py:356)
- **Lease heartbeat**: Prevents duplicate execution of long tasks (executor.py:1227-1258)
- **Database indexes**: Added on hot query paths (sqlite.py:97-100, postgres.py:92-95)

### Operability (Fixed in v1)
- **Graceful shutdown**: `WorkerLifecycle` with ready/draining states (executor.py:77-121)
- **Health status**: `worker_status_overview()` for K8s probes (executor.py:229-246)
- **DLQ tooling**: CLI with list/show/replay (cli.py:54-83)
- **Metrics protocol**: `MetricsRecorder` for Prometheus/StatsD (metrics.py:6-42)
- **Structured logging**: Context injection with execution/task IDs (executor.py:27-46)

### DX (Fixed in v1)
- **Fail-fast errors**: `UnregisteredFunctionError` with helpful message (executor.py:52-60)
- **Registry injection**: Custom registries per executor (executor.py:167, 171)
- **Adaptive polling**: Backoff when no notifications (executor.py:1111-1118)
- **No global patching**: `asyncio.sleep` no longer monkey-patched (executor.py:146)

---

## Remaining Issues

### HIGH Priority

#### H1: SQLite Connection-Per-Operation
**File**: sqlite.py (all methods)
```python
async with aiosqlite.connect(self.db_path) as db:  # Opens new connection every call
```

**Impact**: 
- Overhead under load (~1-5ms per operation)
- File locking contention with concurrent workers
- Not suitable for high-throughput production

**Recommendation**: Use persistent connection or connection pool.

---

#### H2: Postgres Pool Missing Lifecycle Methods
**File**: postgres.py:17-19
```python
async def init_db(self):
    if not self.pool:
        self.pool = await asyncpg.create_pool(self.dsn)
```

**Issues**:
- No `close()` method to clean up pool
- Pool created lazily - operations fail if `init_db()` not called
- Resource leak on executor shutdown

**Recommendation**: Add `async def close(self)` and document lifecycle.

---

#### H3: Clock Skew Sensitivity
**Files**: executor.py:959, 1285; sqlite.py:331; postgres.py:319

All time-sensitive operations use `datetime.now()` from worker:
```python
if now is None:
    now = datetime.now()
```

**Impact**:
- Workers with clock drift can cause premature lease expiration
- Scheduled tasks may fire early/late
- Potential duplicate execution

**Recommendation**: 
- Document NTP requirement
- Consider DB-time option for critical paths
- Add clock drift detection/warning

---

### MEDIUM Priority

#### M1: Type Errors in Codebase
**File**: executor.py (7 errors)

```python
# Lines 490, 494, 496, 760, 764, 831
executor.serializer.loads(completed.result)  # result is bytes | None
```

**File**: postgres.py (3 errors)
```python
# Lines 188, 194, 195 - PoolConnectionProxy vs Connection type mismatch
```

**File**: telemetry.py (14 errors)
- Optional opentelemetry import not handled correctly for type checker

**Recommendation**: Fix type annotations properly instead of suppressing.

---

#### M2: Exception Serialization Loses Information
**File**: utils/serialization.py:47-55
```python
if isinstance(obj, Exception):
    return {
        "__type__": "Exception",
        "message": str(obj),
        "cls": obj.__class__.__name__
    }
```

**Lost**:
- Stack trace
- Custom exception attributes
- Exception chaining

**Recommendation**: Include `traceback.format_exc()` in serialized form.

---

#### M3: RetryPolicy.retry_for Not JSON-Serializable
**File**: utils/serialization.py:43-46
```python
# retry_for is tricky in JSON as it contains classes.
# For simplicity in this demo, we might skip full serialization
```

**Impact**: Tasks restored from DB lose custom `retry_for` configuration.

**Recommendation**: Store exception class names and re-resolve on load.

---

#### M4: Expiry Semantics Inconsistent
**File**: executor.py:627-634
```python
expiry_at = (datetime.now() + expiry) if expiry else None
if scheduled_for and expiry:
    expiry_at = scheduled_for + expiry  # Different calculation!
```

- Without delay: expiry relative to dispatch time
- With delay: expiry relative to scheduled start

**Recommendation**: Document clearly or make consistent (always relative to actual start).

---

#### M5: Redis Notification Race Window
**File**: notifications/redis.py:23-54

Redis pub/sub is fire-and-forget. Window exists between DB check and subscribe where notification can be missed.

**Current mitigation**: Polling fallback handles this.

**Recommendation**: Document limitation; consider Redis Streams for guaranteed delivery.

---

### LOW Priority

#### L1: Dead Letter Cleanup Missing
**Files**: sqlite.py:534-547, postgres.py:512-530

`cleanup_executions()` doesn't clean dead letters.

**Recommendation**: Add `cleanup_dead_letters(older_than)` or include in execution cleanup.

---

#### L2: Progress Table Unbounded
**File**: executor.py (append_progress calls)

Long workflows accumulate unlimited progress records.

**Recommendation**: Add retention/compaction or cap entries per execution.

---

#### L3: Backend Code Duplication
**Files**: sqlite.py, postgres.py

~60% structural similarity between backends.

**Recommendation**: Extract shared logic to base class or mixins.

---

#### L4: CLI Backend Detection Heuristic
**File**: cli.py:112-115
```python
if "://" in args.db or "postgres" in args.db:
```

Could misclassify paths containing "postgres".

**Recommendation**: Add explicit `--backend` flag.

---

#### L5: Pickle Serializer Security Warning
**File**: utils/serialization.py:11-16

Pickle allows arbitrary code execution.

**Current status**: JSON is default, pickle is opt-in.

**Recommendation**: Add prominent documentation warning.

---

#### L6: @Stent.durable() Uses Class Registry
**File**: executor.py:309-346
```python
@classmethod
def durable(cls, ...):
    reg = cls.default_registry  # Always class-level
```

But instances can have different registries (executor.py:171).

**Impact**: Decorated functions always go to class registry, not instance registry.

**Recommendation**: Document limitation; add `executor.register()` for custom registries.

---

## Code Quality Summary

| File | Lines | Issues |
|------|-------|--------|
| executor.py | 1517 | 7 type errors, some `pyrefly: ignore` |
| postgres.py | 631 | 3 type errors |
| sqlite.py | 658 | Clean |
| telemetry.py | 83 | 14 errors (optional import handling) |
| serialization.py | 76 | Missing traceback serialization |

---

## Comparison to v1 Review

| Issue | v1 Status | v2 Status |
|-------|-----------|-----------|
| Transactional integrity | CRITICAL | Fixed |
| SQLite claim race | CRITICAL | Fixed |
| Missing indexes | HIGH | Fixed |
| Lease renewal | HIGH | Fixed |
| DB polling overload | HIGH | Fixed (adaptive backoff) |
| asyncio.sleep patching | HIGH | Fixed (removed) |
| Unregistered function errors | MEDIUM | Fixed |
| Registry scoping | MEDIUM | Fixed (injection supported) |
| Graceful shutdown | MEDIUM | Fixed |
| DLQ operability | MEDIUM | Fixed |
| SQLite connection pattern | - | NEW (HIGH) |
| Postgres pool lifecycle | - | NEW (HIGH) |
| Clock skew | MEDIUM | Still open (HIGH) |
| Type errors | - | NEW (MEDIUM) |
| Exception serialization | - | NEW (MEDIUM) |

---

## Verdict

**Ready for production** with these caveats:
1. Use Postgres for production workloads (SQLite connection pattern limits throughput)
2. Ensure NTP sync across workers
3. Call `init_db()` before use; be aware pool isn't cleaned up on shutdown
4. JSON serializer loses some exception details

**Not yet ready for**:
- High-scale production (>1000 tasks/sec) - needs SQLite pooling or Postgres tuning
- Strict type-checked deployments - has unresolved type errors
