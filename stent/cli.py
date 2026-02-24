"""
Stent CLI - Command-line interface for managing Stent executions.

Usage:
    stent [--db PATH] <command> [options]

Commands:
    list        List executions
    show        Show execution details
    tasks       List/show tasks
    stats       Show queue statistics
    dlq         Dead-letter queue operations
    cleanup     Clean up old executions
    signal      Send signal to execution
    watch       Live monitoring dashboard

Environment:
    STENT_DB  Default database path (default: stent.sqlite)
"""
import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, cast

from stent import Stent, ExecutionState
from stent.core import TaskRecord, DeadLetterRecord

# ANSI color codes (fallback when rich is not available)
class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"

    @classmethod
    def disable(cls):
        for attr in ["RESET", "BOLD", "DIM", "RED", "GREEN", "YELLOW", "BLUE", "MAGENTA", "CYAN"]:
            setattr(cls, attr, "")


# Disable colors if not a TTY or on Windows without ANSI support
if not sys.stdout.isatty() or (os.name == "nt" and not os.environ.get("ANSICON")):
    try:
        # Try enabling ANSI on Windows 10+
        import ctypes
        windll = cast(Any, getattr(ctypes, "windll", None))
        if windll is not None:
            kernel32 = windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        else:
            Colors.disable()
    except Exception:
        Colors.disable()


def state_color(state: str) -> str:
    """Return colored state string."""
    colors = {
        "pending": Colors.YELLOW,
        "running": Colors.BLUE,
        "completed": Colors.GREEN,
        "failed": Colors.RED,
        "timed_out": Colors.RED,
        "cancelled": Colors.DIM,
        "cancelling": Colors.YELLOW,
    }
    color = colors.get(state, "")
    return f"{color}{state}{Colors.RESET}"


def format_duration(td: timedelta | None) -> str:
    """Format a timedelta as human-readable string."""
    if td is None:
        return "-"
    total_seconds = int(td.total_seconds())
    if total_seconds < 60:
        return f"{total_seconds}s"
    elif total_seconds < 3600:
        mins, secs = divmod(total_seconds, 60)
        return f"{mins}m {secs}s"
    else:
        hours, remainder = divmod(total_seconds, 3600)
        mins, secs = divmod(remainder, 60)
        return f"{hours}h {mins}m"


def format_time(dt: datetime | None) -> str:
    """Format datetime for display."""
    if dt is None:
        return "-"
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def format_time_short(dt: datetime | None) -> str:
    """Format datetime with just time for recent entries."""
    if dt is None:
        return "-"
    return dt.strftime("%H:%M:%S")


def truncate(s: str, length: int) -> str:
    """Truncate string with ellipsis."""
    if len(s) <= length:
        return s
    return s[:length - 3] + "..."


# ============================================================================
# EXECUTION COMMANDS
# ============================================================================

async def list_executions(executor: Stent, args):
    """List executions with filtering."""
    state_filter = args.state.lower() if args.state else None
    executions = await executor.list_executions(limit=args.limit, state=state_filter)
    
    if not executions:
        print(f"{Colors.DIM}No executions found.{Colors.RESET}")
        return

    # Header
    print(f"{Colors.BOLD}{'ID':<36} {'State':<12} {'Queue':<10} {'Started':<20} {'Duration':<10}{Colors.RESET}")
    print("-" * 90)
    
    for exc in executions:
        duration = None
        if exc.started_at:
            end_time = exc.completed_at or datetime.now()
            duration = end_time - exc.started_at
        
        queue = exc.queue or "default"
        started = format_time(exc.started_at)
        dur_str = format_duration(duration)
        
        print(f"{exc.id:<36} {state_color(exc.state):<22} {queue:<10} {started:<20} {dur_str:<10}")


async def show_execution(executor: Stent, args):
    """Show detailed execution info."""
    try:
        state = await executor.state_of(args.id)
    except ValueError:
        print(f"{Colors.RED}Execution {args.id} not found.{Colors.RESET}")
        return 1

    # Header
    print(f"\n{Colors.BOLD}Execution: {state.id}{Colors.RESET}")
    print("=" * 60)
    
    # Basic info
    print(f"  State:      {state_color(state.state)}")
    print(f"  Queue:      {state.queue or 'default'}")
    print(f"  Priority:   {state.priority}")
    if state.tags:
        print(f"  Tags:       {', '.join(state.tags)}")

    if state.counters:
        print(f"\n{Colors.BOLD}Counters{Colors.RESET}")
        for key in sorted(state.counters.keys()):
            print(f"  {key}: {state.counters[key]}")

    if state.custom_state:
        print(f"\n{Colors.BOLD}Custom State{Colors.RESET}")
        for key in sorted(state.custom_state.keys()):
            value_str = str(state.custom_state[key])
            if len(value_str) > 200:
                value_str = value_str[:200] + "..."
            print(f"  {key}: {value_str}")
    
    # Timing
    print(f"\n{Colors.BOLD}Timing{Colors.RESET}")
    print(f"  Started:    {format_time(state.started_at)}")
    print(f"  Completed:  {format_time(state.completed_at)}")
    if state.started_at:
        end_time = state.completed_at or datetime.now()
        duration = end_time - state.started_at
        print(f"  Duration:   {format_duration(duration)}")
    
    # Progress
    if state.progress:
        print(f"\n{Colors.BOLD}Progress ({len(state.progress)} steps){Colors.RESET}")
        for p in state.progress:
            timestamp = p.completed_at or p.started_at
            ts_str = format_time_short(timestamp)
            
            if p.status == "completed":
                icon = f"{Colors.GREEN}✓{Colors.RESET}"
            elif p.status == "failed":
                icon = f"{Colors.RED}✗{Colors.RESET}"
            elif p.status == "running":
                icon = f"{Colors.BLUE}▶{Colors.RESET}"
            elif p.status == "cache_hit":
                icon = f"{Colors.CYAN}⚡{Colors.RESET}"
            else:
                icon = f"{Colors.YELLOW}○{Colors.RESET}"
            
            step_name = truncate(p.step, 40)
            print(f"  [{ts_str}] {icon} {step_name}")
            if p.detail:
                print(f"             {Colors.DIM}{p.detail}{Colors.RESET}")
    
    # Result
    if state.result is not None:
        print(f"\n{Colors.BOLD}Result{Colors.RESET}")
        if state.result.ok:
            result_str = str(state.result.value)
            if len(result_str) > 200:
                result_str = result_str[:200] + "..."
            print(f"  {Colors.GREEN}OK:{Colors.RESET} {result_str}")
        else:
            error_str = str(state.result.error)
            if len(error_str) > 200:
                error_str = error_str[:200] + "..."
            print(f"  {Colors.RED}Error:{Colors.RESET} {error_str}")
    
    print()
    return 0


# ============================================================================
# TASK COMMANDS
# ============================================================================

async def list_tasks(executor: Stent, args):
    """List tasks with filtering."""
    tasks = await executor.backend.list_tasks(
        limit=args.limit,
        state=args.state.lower() if args.state else None
    )
    
    if not tasks:
        print(f"{Colors.DIM}No tasks found.{Colors.RESET}")
        return

    print(f"{Colors.BOLD}{'Task ID':<36} {'State':<10} {'Kind':<12} {'Step':<25} {'Queue':<10}{Colors.RESET}")
    print("-" * 100)
    
    for task in tasks:
        step = truncate(task.step_name, 25)
        queue = task.queue or "default"
        print(f"{task.id:<36} {state_color(task.state):<20} {task.kind:<12} {step:<25} {queue:<10}")


async def show_task(executor: Stent, args):
    """Show detailed task info."""
    task = await executor.backend.get_task(args.id)
    if not task:
        print(f"{Colors.RED}Task {args.id} not found.{Colors.RESET}")
        return 1

    print(f"\n{Colors.BOLD}Task: {task.id}{Colors.RESET}")
    print("=" * 60)
    
    print(f"  Execution:  {task.execution_id}")
    print(f"  Step:       {task.step_name}")
    print(f"  Kind:       {task.kind}")
    print(f"  State:      {state_color(task.state)}")
    print(f"  Queue:      {task.queue or 'default'}")
    print(f"  Priority:   {task.priority}")
    print(f"  Retries:    {task.retries}")
    
    if task.tags:
        print(f"  Tags:       {', '.join(task.tags)}")
    if task.worker_id:
        print(f"  Worker:     {task.worker_id}")
    if task.idempotency_key:
        print(f"  Idemp Key:  {task.idempotency_key}")
    
    print(f"\n{Colors.BOLD}Timing{Colors.RESET}")
    print(f"  Created:    {format_time(task.created_at)}")
    print(f"  Started:    {format_time(task.started_at)}")
    print(f"  Completed:  {format_time(task.completed_at)}")
    if task.scheduled_for:
        print(f"  Scheduled:  {format_time(task.scheduled_for)}")
    if task.lease_expires_at:
        print(f"  Lease exp:  {format_time(task.lease_expires_at)}")
    
    if task.retry_policy:
        rp = task.retry_policy
        print(f"\n{Colors.BOLD}Retry Policy{Colors.RESET}")
        print(f"  Max attempts: {rp.max_attempts}")
        print(f"  Backoff:      {rp.initial_delay}s × {rp.backoff_factor} (max {rp.max_delay}s)")
    
    print()
    return 0


# ============================================================================
# STATS COMMAND
# ============================================================================

async def show_stats(executor: Stent, args):
    """Show queue and execution statistics."""
    # Count executions by state
    exec_states = ["pending", "running", "completed", "failed", "timed_out", "cancelled"]
    exec_counts: dict[str, int] = {}
    for state in exec_states:
        exec_counts[state] = await executor.backend.count_executions(state=state)
    
    # Count tasks by state
    task_states = ["pending", "running", "completed", "failed"]
    task_counts: dict[str, int] = {}
    for state in task_states:
        task_counts[state] = await executor.backend.count_tasks(state=state)
    
    # Queue depths
    pending_tasks = await executor.backend.list_tasks(limit=1000, state="pending")
    queues: dict[str, int] = {}
    for t in pending_tasks:
        q = t.queue or "default"
        queues[q] = queues.get(q, 0) + 1
    
    # DLQ count
    dlq_count = await executor.backend.count_dead_tasks()
    
    # Running activities
    running_tasks = await executor.get_running_activities()
    
    # Print stats
    print(f"\n{Colors.BOLD}Stent Statistics{Colors.RESET}")
    print("=" * 50)
    
    print(f"\n{Colors.BOLD}Executions{Colors.RESET}")
    total_exec = sum(exec_counts.values())
    print(f"  Total:     {total_exec}")
    for state, count in exec_counts.items():
        if count > 0:
            print(f"  {state.capitalize():<10} {count}")
    
    print(f"\n{Colors.BOLD}Tasks{Colors.RESET}")
    total_tasks = sum(task_counts.values())
    print(f"  Total:     {total_tasks}")
    for state, count in task_counts.items():
        if count > 0:
            print(f"  {state.capitalize():<10} {count}")
    
    print(f"\n{Colors.BOLD}Queue Depths{Colors.RESET}")
    if queues:
        for queue, depth in sorted(queues.items()):
            bar = "█" * min(depth, 30)
            print(f"  {queue:<15} {depth:>5} {Colors.CYAN}{bar}{Colors.RESET}")
    else:
        print(f"  {Colors.DIM}All queues empty{Colors.RESET}")
    
    print(f"\n{Colors.BOLD}Dead Letter Queue{Colors.RESET}")
    print(f"  Count:     {dlq_count}")
    
    if running_tasks:
        print(f"\n{Colors.BOLD}Running Tasks ({len(running_tasks)}){Colors.RESET}")
        for t in running_tasks[:10]:
            step = truncate(t.step_name, 30)
            worker = t.worker_id or "unknown"
            print(f"  {step:<30} worker={worker}")
        if len(running_tasks) > 10:
            print(f"  ... and {len(running_tasks) - 10} more")
    
    print()


# ============================================================================
# DLQ COMMANDS
# ============================================================================

async def dlq_list(executor: Stent, args):
    """List dead-letter queue entries."""
    records = await executor.list_dead_letters(limit=args.limit)
    if not records:
        print(f"{Colors.DIM}Dead-letter queue is empty.{Colors.RESET}")
        return

    print(f"{Colors.BOLD}{'Task ID':<36} {'Step':<25} {'Moved At':<20} {'Reason':<30}{Colors.RESET}")
    print("-" * 115)
    
    for record in records:
        task = record.task
        step = truncate(task.step_name, 25)
        moved = format_time(record.moved_at)
        reason = truncate(record.reason or "", 30)
        print(f"{task.id:<36} {step:<25} {moved:<20} {reason:<30}")


async def dlq_show(executor: Stent, args):
    """Show dead-letter entry details."""
    record = await executor.get_dead_letter(args.id)
    if not record:
        print(f"{Colors.RED}Dead-letter task {args.id} not found.{Colors.RESET}")
        return 1

    task = record.task
    print(f"\n{Colors.BOLD}Dead-Letter Task: {task.id}{Colors.RESET}")
    print("=" * 60)
    
    print(f"  Execution:  {task.execution_id}")
    print(f"  Step:       {task.step_name}")
    print(f"  Kind:       {task.kind}")
    print(f"  Queue:      {task.queue or 'default'}")
    print(f"  Retries:    {task.retries}")
    
    if task.tags:
        print(f"  Tags:       {', '.join(task.tags)}")
    
    print(f"\n{Colors.BOLD}Dead-Letter Info{Colors.RESET}")
    print(f"  Moved At:   {format_time(record.moved_at)}")
    print(f"  Reason:     {record.reason}")
    
    print()
    return 0


async def dlq_replay(executor: Stent, args):
    """Replay a dead-letter entry."""
    try:
        new_id = await executor.replay_dead_letter(args.id, queue=args.queue)
        print(f"{Colors.GREEN}✓{Colors.RESET} Replayed dead-letter task {args.id}")
        print(f"  New task ID: {new_id}")
    except ValueError as e:
        print(f"{Colors.RED}Error:{Colors.RESET} {e}")
        return 1


async def dlq_discard(executor: Stent, args):
    """Discard a dead-letter entry."""
    if not args.force:
        confirm = input(f"Discard dead-letter task {args.id}? [y/N] ")
        if confirm.lower() != "y":
            print("Cancelled.")
            return

    success = await executor.discard_dead_letter(args.id)
    if success:
        print(f"{Colors.GREEN}✓{Colors.RESET} Discarded dead-letter task {args.id}")
    else:
        print(f"{Colors.RED}Error:{Colors.RESET} Task {args.id} not found")
        return 1


async def dlq_replay_all(executor: Stent, args):
    """Replay all dead-letter entries."""
    records = await executor.list_dead_letters(limit=1000)
    if not records:
        print(f"{Colors.DIM}Dead-letter queue is empty.{Colors.RESET}")
        return

    if not args.force:
        confirm = input(f"Replay {len(records)} dead-letter tasks? [y/N] ")
        if confirm.lower() != "y":
            print("Cancelled.")
            return

    replayed = 0
    for record in records:
        try:
            await executor.replay_dead_letter(record.task.id)
            replayed += 1
        except Exception as e:
            print(f"{Colors.RED}Error replaying {record.task.id}:{Colors.RESET} {e}")

    print(f"{Colors.GREEN}✓{Colors.RESET} Replayed {replayed}/{len(records)} tasks")


# ============================================================================
# CLEANUP COMMAND
# ============================================================================

async def cleanup(executor: Stent, args):
    """Clean up old executions and dead-letter entries."""
    cutoff = datetime.now() - timedelta(days=args.days)
    
    if args.dry_run:
        print(f"Dry run: Would clean up records older than {format_time(cutoff)}")
        return

    if not args.force:
        confirm = input(f"Delete records older than {args.days} days? [y/N] ")
        if confirm.lower() != "y":
            print("Cancelled.")
            return

    exec_count = 0
    dlq_count = 0
    
    if args.all or args.executions:
        exec_count = await executor.backend.cleanup_executions(cutoff)
        print(f"  Cleaned up {exec_count} executions")
    
    if args.all or args.dlq:
        dlq_count = await executor.backend.cleanup_dead_letters(cutoff)
        print(f"  Cleaned up {dlq_count} dead-letter entries")
    
    total = exec_count + dlq_count
    print(f"\n{Colors.GREEN}✓{Colors.RESET} Cleaned up {total} total records")


# ============================================================================
# SIGNAL COMMAND
# ============================================================================

async def send_signal(executor: Stent, args):
    """Send a signal to an execution."""
    # Parse payload
    payload = args.payload
    if args.json:
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as e:
            print(f"{Colors.RED}Invalid JSON:{Colors.RESET} {e}")
            return 1

    try:
        await executor.send_signal(args.execution_id, args.name, payload)
        print(f"{Colors.GREEN}✓{Colors.RESET} Sent signal '{args.name}' to execution {args.execution_id}")
    except Exception as e:
        print(f"{Colors.RED}Error:{Colors.RESET} {e}")
        return 1


# ============================================================================
# WATCH COMMAND
# ============================================================================

# ── Watch dashboard dataclasses ──────────────────────────────────────────

@dataclass
class DashboardCounts:
    exec_pending: int = 0
    exec_running: int = 0
    exec_completed: int = 0
    exec_failed: int = 0
    exec_timed_out: int = 0
    exec_cancelled: int = 0
    task_pending: int = 0
    task_running: int = 0
    task_completed: int = 0
    task_failed: int = 0
    dlq: int = 0


@dataclass
class ActiveExecution:
    id: str
    root_function: str
    state: str
    started_at: datetime | None
    steps_completed: int
    steps_total: int


@dataclass
class RunningTask:
    worker_id: str
    step_name: str
    execution_id: str
    started_at: datetime | None


@dataclass
class RecentCompletion:
    id: str
    root_function: str
    state: str
    duration: timedelta | None


@dataclass
class QueueDepth:
    queue: str
    count: int


@dataclass
class DashboardData:
    counts: DashboardCounts
    active_executions: list[ActiveExecution]
    running_tasks: list[RunningTask]
    recent_completions: list[RecentCompletion]
    queue_depths: list[QueueDepth]
    dlq_entries: list[DeadLetterRecord]
    gather_duration: float = 0.0


@dataclass
class ThroughputMetrics:
    tasks_per_min: float = 0.0
    avg_duration_s: float = 0.0
    error_rate_pct: float = 0.0


# ── Throughput tracker ───────────────────────────────────────────────────

class ThroughputTracker:
    """Ring buffer of (timestamp, completed_count, failed_count) snapshots."""

    def __init__(self, window: float = 60.0):
        self._window = window
        self._snapshots: deque[tuple[float, int, int]] = deque()
        self._durations: deque[float] = deque(maxlen=200)

    def record(self, completed: int, failed: int, durations: list[float]):
        now = time.monotonic()
        self._snapshots.append((now, completed, failed))
        self._durations.extend(durations)
        # Evict stale entries
        cutoff = now - self._window
        while self._snapshots and self._snapshots[0][0] < cutoff:
            self._snapshots.popleft()

    def metrics(self) -> ThroughputMetrics:
        if len(self._snapshots) < 2:
            avg_d = (sum(self._durations) / len(self._durations)) if self._durations else 0.0
            return ThroughputMetrics(avg_duration_s=avg_d)

        first = self._snapshots[0]
        last = self._snapshots[-1]
        elapsed = last[0] - first[0]
        if elapsed <= 0:
            return ThroughputMetrics()

        completed_delta = last[1] - first[1]
        failed_delta = last[2] - first[2]
        total_delta = completed_delta + failed_delta
        tpm = (completed_delta / elapsed) * 60.0 if elapsed > 0 else 0.0
        err = (failed_delta / total_delta * 100.0) if total_delta > 0 else 0.0
        avg_d = (sum(self._durations) / len(self._durations)) if self._durations else 0.0

        return ThroughputMetrics(tasks_per_min=max(0, tpm), avg_duration_s=avg_d, error_rate_pct=err)


# ── Data gathering ───────────────────────────────────────────────────────

async def _gather_dashboard_data(executor: Stent, *, active_limit: int = 10, recent_limit: int = 10) -> DashboardData:
    backend = executor.backend

    # Round 1: all counts + lists in one gather
    (
        exec_pend, exec_run, exec_comp, exec_fail, exec_to, exec_canc,
        task_pend, task_run, task_comp, task_fail,
        dlq_count,
        running_execs_raw, running_tasks_raw, recent_execs_raw,
        pending_tasks_raw, completed_tasks_raw,
    ) = await asyncio.gather(
        backend.count_executions(state="pending"),
        backend.count_executions(state="running"),
        backend.count_executions(state="completed"),
        backend.count_executions(state="failed"),
        backend.count_executions(state="timed_out"),
        backend.count_executions(state="cancelled"),
        backend.count_tasks(state="pending"),
        backend.count_tasks(state="running"),
        backend.count_tasks(state="completed"),
        backend.count_tasks(state="failed"),
        backend.count_dead_tasks(),
        backend.list_executions(limit=active_limit, state="running"),
        backend.list_tasks(limit=active_limit, state="running"),
        backend.list_executions(limit=recent_limit),
        backend.list_tasks(limit=1000, state="pending"),
        backend.list_tasks(limit=20, state="completed"),
    )

    counts = DashboardCounts(
        exec_pending=exec_pend, exec_running=exec_run, exec_completed=exec_comp,
        exec_failed=exec_fail, exec_timed_out=exec_to, exec_cancelled=exec_canc,
        task_pending=task_pend, task_running=task_run,
        task_completed=task_comp, task_failed=task_fail,
        dlq=dlq_count,
    )

    # Round 2: fetch full records for running executions to get progress
    active_executions: list[ActiveExecution] = []
    if running_execs_raw:
        full_records = await asyncio.gather(
            *(backend.get_execution(r.id) for r in running_execs_raw)
        )
        for rec in full_records:
            if rec is None:
                continue
            total = len(rec.progress)
            done = sum(1 for p in rec.progress if p.status in ("completed", "cache_hit"))
            active_executions.append(ActiveExecution(
                id=rec.id, root_function=rec.root_function, state=rec.state,
                started_at=rec.started_at, steps_completed=done, steps_total=total,
            ))

    # Running tasks
    now = datetime.now()
    running_tasks = [
        RunningTask(
            worker_id=t.worker_id or "unknown",
            step_name=t.step_name,
            execution_id=t.execution_id,
            started_at=t.started_at,
        )
        for t in running_tasks_raw
    ]

    # Recent completions (terminal states only)
    recent_completions: list[RecentCompletion] = []
    for exc in recent_execs_raw:
        if exc.state not in ("completed", "failed", "timed_out", "cancelled"):
            continue
        dur = None
        if exc.started_at:
            end = exc.completed_at or now
            dur = end - exc.started_at
        recent_completions.append(RecentCompletion(
            id=exc.id, root_function=exc.root_function,
            state=exc.state, duration=dur,
        ))

    # Queue depths
    queues: dict[str, int] = {}
    for t in pending_tasks_raw:
        q = t.queue or "default"
        queues[q] = queues.get(q, 0) + 1
    queue_depths = [QueueDepth(q, c) for q, c in sorted(queues.items())]

    # Durations from recently completed tasks (for throughput tracker)
    # stored on data so the watch loop can feed them to the tracker
    durations: list[float] = []
    for t in completed_tasks_raw:
        if t.started_at and t.completed_at:
            durations.append((t.completed_at - t.started_at).total_seconds())

    # DLQ entries
    dlq_entries: list[DeadLetterRecord] = []
    if dlq_count > 0:
        dlq_entries = await executor.list_dead_letters(limit=5)

    data = DashboardData(
        counts=counts, active_executions=active_executions,
        running_tasks=running_tasks, recent_completions=recent_completions,
        queue_depths=queue_depths, dlq_entries=dlq_entries,
    )
    # stash durations for tracker
    data._durations = durations  # type: ignore[attr-defined]
    return data


# ── Rendering primitives ─────────────────────────────────────────────────

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _visible_len(s: str) -> int:
    """Length of string ignoring ANSI escape sequences."""
    return len(_ANSI_RE.sub("", s))


def _pad(s: str, width: int) -> str:
    """Pad string to width, accounting for ANSI escapes."""
    vl = _visible_len(s)
    if vl >= width:
        return s
    return s + " " * (width - vl)


def _hline(w: int, left: str = "├", right: str = "┤") -> str:
    return left + "─" * (w - 2) + right


def _row(content: str, w: int) -> str:
    """Content padded inside │ ... │"""
    inner = w - 4  # "│ " + content + " │"
    return "│ " + _pad(content, inner) + " │"


def _progress_bar(completed: int, total: int, width: int = 6) -> str:
    if total == 0:
        return " " * width + " 0/0"
    filled = round(completed / total * width)
    bar = (
        f"{Colors.GREEN}{'█' * filled}{Colors.RESET}"
        f"{Colors.DIM}{'░' * (width - filled)}{Colors.RESET}"
    )
    return f"{bar} {completed}/{total}"


def _render_section(title: str, rows: list[str], w: int) -> list[str]:
    """Separator line + bold title + content rows."""
    lines = [_hline(w)]
    lines.append(_row(f"{Colors.BOLD}{title}{Colors.RESET}", w))
    for r in rows:
        lines.append(_row(r, w))
    return lines


# ── Dashboard renderer ───────────────────────────────────────────────────

def _render_dashboard(
    data: DashboardData,
    metrics: ThroughputMetrics,
    width: int,
    height: int,
) -> str:
    w = max(width, 60)
    now_str = datetime.now().strftime("%H:%M:%S")
    gather_str = f"{data.gather_duration:.1f}s"
    c = data.counts

    lines: list[str] = []

    # ── Header ──
    lines.append("┌" + "─" * (w - 2) + "┐")
    header_left = f"{Colors.BOLD}STENT WATCH{Colors.RESET}"
    header_right = f"{now_str}  {gather_str}"
    header_gap = w - 4 - _visible_len(header_left) - len(header_right)
    lines.append("│ " + header_left + " " * max(header_gap, 1) + header_right + " │")

    # ── Summary bar ──
    summary = (
        f"{Colors.YELLOW}● {c.exec_pending} pend{Colors.RESET}   "
        f"{Colors.BLUE}▶ {c.exec_running} run{Colors.RESET}   "
        f"{Colors.GREEN}✓ {c.exec_completed} ok{Colors.RESET}   "
        f"{Colors.RED}✗ {c.exec_failed} fail{Colors.RESET}   "
        f"{Colors.RED}☠ {c.dlq} dlq{Colors.RESET}"
    )
    lines.append(_hline(w))
    lines.append(_row(summary, w))

    # ── Throughput bar ──
    tpm_str = f"{metrics.tasks_per_min:.0f}" if metrics.tasks_per_min >= 1 else f"{metrics.tasks_per_min:.1f}"
    avg_str = f"{metrics.avg_duration_s:.1f}s" if metrics.avg_duration_s > 0 else "-"
    err_str = f"{metrics.error_rate_pct:.1f}%"
    throughput_line = f"Throughput [{tpm_str}/min]   Avg Duration [{avg_str}]   Err% [{err_str}]"
    lines.append(_hline(w))
    lines.append(_row(throughput_line, w))

    # Compute how many rows remain for sections
    # Used lines so far: top border + header + hline + summary + hline + throughput = 6
    # Bottom: hline + footer + bottom border = 3 (we add later)
    # Each section: 1 hline + 1 title + N rows
    used = len(lines) + 3  # bottom overhead
    available = max(height - used, 10)

    # Decide section row counts based on data
    section_data_counts = {
        "active": len(data.active_executions),
        "running": len(data.running_tasks),
        "recent": len(data.recent_completions),
        "queues": len(data.queue_depths),
        "dlq": len(data.dlq_entries),
    }
    section_rows = _allocate_rows(available, section_data_counts)

    inner = w - 4

    # ── Active Executions ──
    if section_rows["active"] > 0 or data.active_executions:
        rows: list[str] = []
        for exc in data.active_executions[:section_rows.get("active", 3)]:
            eid = exc.id[:8]
            now_t = datetime.now()
            dur = ""
            if exc.started_at:
                dur = format_duration(now_t - exc.started_at)
            func = truncate(exc.root_function, max(inner - 45, 10))
            bar = _progress_bar(exc.steps_completed, exc.steps_total)
            row_str = f"{Colors.DIM}{eid}{Colors.RESET}  {_pad(func, max(inner - 43, 10))} {_pad(state_color(exc.state), 22)} {dur:>5s}  {bar}"
            rows.append(row_str)
        if not rows:
            rows.append(f"{Colors.DIM}(none){Colors.RESET}")
        lines.extend(_render_section("ACTIVE EXECUTIONS", rows, w))

    # ── Running Tasks ──
    if section_rows["running"] > 0 or data.running_tasks:
        rows = []
        for task in data.running_tasks[:section_rows.get("running", 3)]:
            worker = truncate(task.worker_id, 12)
            step = truncate(task.step_name, max(inner - 40, 10))
            eid = task.execution_id[:8]
            dur = ""
            if task.started_at:
                dur = f"{(datetime.now() - task.started_at).total_seconds():.1f}s"
            rows.append(f"{Colors.DIM}{worker:<12}{Colors.RESET}  {_pad(step, max(inner - 40, 10))}  {eid}  {dur:>6s}")
        if not rows:
            rows.append(f"{Colors.DIM}(none){Colors.RESET}")
        lines.extend(_render_section("RUNNING TASKS", rows, w))

    # ── Recent Completions ──
    if section_rows["recent"] > 0 or data.recent_completions:
        rows = []
        for comp in data.recent_completions[:section_rows.get("recent", 3)]:
            eid = comp.id[:8]
            # ExecutionState doesn't have root_function; use "?" or the stored value
            func = truncate(comp.root_function, max(inner - 40, 10))
            dur = format_duration(comp.duration) if comp.duration else "-"
            icon = f"{Colors.GREEN}✓{Colors.RESET}" if comp.state == "completed" else f"{Colors.RED}✗{Colors.RESET}"
            rows.append(f"{Colors.DIM}{eid}{Colors.RESET}  {_pad(func, max(inner - 40, 10))} {_pad(state_color(comp.state), 22)} {dur:>6s}  {icon}")
        if not rows:
            rows.append(f"{Colors.DIM}(none){Colors.RESET}")
        lines.extend(_render_section("RECENT COMPLETIONS", rows, w))

    # ── Queue Depth ──
    if data.queue_depths:
        rows = []
        max_count = max(q.count for q in data.queue_depths) if data.queue_depths else 1
        bar_max = max(inner - 25, 5)
        for qd in data.queue_depths[:section_rows.get("queues", 3)]:
            bar_len = round(qd.count / max(max_count, 1) * bar_max) if max_count > 0 else 0
            bar = f"{Colors.CYAN}{'█' * bar_len}{Colors.RESET}"
            rows.append(f"{qd.queue:<15} {bar} {qd.count}")
        lines.extend(_render_section("QUEUE DEPTH", rows, w))

    # ── DLQ ──
    if data.dlq_entries:
        title = f"DEAD LETTER QUEUE ({data.counts.dlq})"
        rows = []
        for dlr in data.dlq_entries[:section_rows.get("dlq", 3)]:
            eid = dlr.task.id[:8]
            step = truncate(dlr.task.step_name, max(inner - 40, 10))
            moved = format_time_short(dlr.moved_at)
            reason = truncate(dlr.reason or "", max(inner - 35, 10))
            rows.append(f"{Colors.DIM}{eid}{Colors.RESET}  {step:<15}  {moved}  {Colors.RED}{reason}{Colors.RESET}")
        lines.extend(_render_section(title, rows, w))

    # ── Bottom border ──
    lines.append("└" + "─" * (w - 2) + "┘")

    # Footer
    footer = "Ctrl+C to exit"
    pad_left = (w - len(footer)) // 2
    lines.append(" " * pad_left + f"{Colors.DIM}{footer}{Colors.RESET}")

    return "\n".join(lines)


def _allocate_rows(available: int, data_counts: dict[str, int]) -> dict[str, int]:
    """Distribute available rows among sections proportionally.

    Each section costs 2 lines overhead (separator + title), then data rows.
    """
    sections = [k for k, v in data_counts.items() if v > 0]
    if not sections:
        # Still show active/running/recent with 1 row each
        return {k: 1 for k in data_counts}

    # Overhead per visible section: 2 lines (hline + title)
    overhead = len(sections) * 2
    remaining = max(available - overhead, len(sections))

    # First pass: everyone gets at least 1 row
    alloc = {k: 0 for k in data_counts}
    for k in sections:
        alloc[k] = 1
    remaining -= len(sections)

    # Distribute rest proportionally, capped at actual data count
    total_want = sum(max(data_counts[k] - 1, 0) for k in sections)
    if total_want > 0 and remaining > 0:
        for k in sections:
            want = max(data_counts[k] - 1, 0)
            extra = min(round(want / total_want * remaining), data_counts[k] - 1)
            alloc[k] += extra

    return alloc


# ── Watch loop ───────────────────────────────────────────────────────────

async def watch(executor: Stent, args):
    """Live monitoring dashboard."""
    tracker = ThroughputTracker()

    try:
        while True:
            t0 = time.monotonic()
            data = await _gather_dashboard_data(executor, active_limit=10, recent_limit=10)
            data.gather_duration = time.monotonic() - t0

            durations: list[float] = getattr(data, "_durations", [])
            tracker.record(data.counts.task_completed, data.counts.task_failed, durations)
            metrics = tracker.metrics()

            try:
                ts = os.get_terminal_size()
                width, height = max(ts.columns, 60), max(ts.lines, 20)
            except OSError:
                width, height = 80, 24

            frame = _render_dashboard(data, metrics, width, height)
            sys.stdout.write("\033[2J\033[H" + frame)
            sys.stdout.flush()

            await asyncio.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nExiting watch mode.")


# ============================================================================
# MAIN
# ============================================================================

async def main_async():
    parser = argparse.ArgumentParser(
        description="Stent CLI - Manage distributed durable functions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  stent list                          List recent executions
  stent list --state running          List running executions
  stent show <execution-id>           Show execution details
  stent stats                         Show queue statistics
  stent dlq list                      List dead-letter queue
  stent dlq replay <task-id>          Replay failed task
  stent cleanup --days 7              Clean up old records
  stent signal <exec-id> my_signal    Send signal
  stent watch                         Live monitoring
        """
    )
    
    default_db = os.environ.get("STENT_DB", "stent.sqlite")
    parser.add_argument(
        "--db", 
        default=default_db, 
        help=f"Database path or connection string (default: {default_db}, env: STENT_DB)"
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored output"
    )
    
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # list command
    list_parser = subparsers.add_parser("list", help="List executions")
    list_parser.add_argument("--limit", "-n", type=int, default=20, help="Number of executions (default: 20)")
    list_parser.add_argument("--state", "-s", help="Filter by state (pending, running, completed, failed)")
    
    # show command
    show_parser = subparsers.add_parser("show", help="Show execution details")
    show_parser.add_argument("id", help="Execution ID")
    
    # tasks command
    tasks_parser = subparsers.add_parser("tasks", help="Task operations")
    tasks_sub = tasks_parser.add_subparsers(dest="tasks_command", required=True)
    
    tasks_list = tasks_sub.add_parser("list", help="List tasks")
    tasks_list.add_argument("--limit", "-n", type=int, default=20, help="Number of tasks")
    tasks_list.add_argument("--state", "-s", help="Filter by state")
    
    tasks_show = tasks_sub.add_parser("show", help="Show task details")
    tasks_show.add_argument("id", help="Task ID")
    
    # stats command
    stats_parser = subparsers.add_parser("stats", help="Show queue statistics")
    
    # dlq command
    dlq_parser = subparsers.add_parser("dlq", help="Dead-letter queue operations")
    dlq_sub = dlq_parser.add_subparsers(dest="dlq_command", required=True)
    
    dlq_list_parser = dlq_sub.add_parser("list", help="List DLQ entries")
    dlq_list_parser.add_argument("--limit", "-n", type=int, default=20, help="Number of entries")
    
    dlq_show_parser = dlq_sub.add_parser("show", help="Show DLQ entry details")
    dlq_show_parser.add_argument("id", help="Dead-letter task ID")
    
    dlq_replay_parser = dlq_sub.add_parser("replay", help="Replay a DLQ entry")
    dlq_replay_parser.add_argument("id", help="Dead-letter task ID")
    dlq_replay_parser.add_argument("--queue", help="Override queue when replaying")
    
    dlq_discard_parser = dlq_sub.add_parser("discard", help="Discard a DLQ entry")
    dlq_discard_parser.add_argument("id", help="Dead-letter task ID")
    dlq_discard_parser.add_argument("--force", "-f", action="store_true", help="Skip confirmation")
    
    dlq_replay_all_parser = dlq_sub.add_parser("replay-all", help="Replay all DLQ entries")
    dlq_replay_all_parser.add_argument("--force", "-f", action="store_true", help="Skip confirmation")
    
    # cleanup command
    cleanup_parser = subparsers.add_parser("cleanup", help="Clean up old records")
    cleanup_parser.add_argument("--days", "-d", type=int, default=30, help="Delete records older than N days (default: 30)")
    cleanup_parser.add_argument("--executions", action="store_true", help="Clean up executions only")
    cleanup_parser.add_argument("--dlq", action="store_true", help="Clean up DLQ only")
    cleanup_parser.add_argument("--all", "-a", action="store_true", help="Clean up all (default if no specific flag)")
    cleanup_parser.add_argument("--dry-run", action="store_true", help="Show what would be deleted")
    cleanup_parser.add_argument("--force", "-f", action="store_true", help="Skip confirmation")
    
    # signal command
    signal_parser = subparsers.add_parser("signal", help="Send signal to execution")
    signal_parser.add_argument("execution_id", help="Execution ID")
    signal_parser.add_argument("name", help="Signal name")
    signal_parser.add_argument("payload", nargs="?", default="", help="Signal payload")
    signal_parser.add_argument("--json", "-j", action="store_true", help="Parse payload as JSON")
    
    # watch command
    watch_parser = subparsers.add_parser("watch", help="Live monitoring")
    watch_parser.add_argument("--interval", "-i", type=float, default=2.0, help="Refresh interval in seconds (default: 2)")
    
    args = parser.parse_args()
    
    # Handle no-color flag
    if args.no_color:
        Colors.disable()
    
    # Determine backend
    if "://" in args.db or "postgres" in args.db.lower():
        backend = Stent.backends.PostgresBackend(args.db)
    else:
        backend = Stent.backends.SQLiteBackend(args.db)

    await backend.init_db()
    executor = Stent(backend=backend)
    
    # Handle --all default for cleanup
    if args.command == "cleanup" and not args.executions and not args.dlq:
        args.all = True
    
    try:
        # Dispatch to command handlers
        result = 0
        if args.command == "list":
            await list_executions(executor, args)
        elif args.command == "show":
            result = await show_execution(executor, args) or 0
        elif args.command == "tasks":
            if args.tasks_command == "list":
                await list_tasks(executor, args)
            elif args.tasks_command == "show":
                result = await show_task(executor, args) or 0
        elif args.command == "stats":
            await show_stats(executor, args)
        elif args.command == "dlq":
            if args.dlq_command == "list":
                await dlq_list(executor, args)
            elif args.dlq_command == "show":
                result = await dlq_show(executor, args) or 0
            elif args.dlq_command == "replay":
                result = await dlq_replay(executor, args) or 0
            elif args.dlq_command == "discard":
                result = await dlq_discard(executor, args) or 0
            elif args.dlq_command == "replay-all":
                result = await dlq_replay_all(executor, args) or 0
        elif args.command == "cleanup":
            await cleanup(executor, args)
        elif args.command == "signal":
            result = await send_signal(executor, args) or 0
        elif args.command == "watch":
            await watch(executor, args)

        return result
    finally:
        await backend.close()


def main():
    try:
        result = asyncio.run(main_async())
        sys.exit(result or 0)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)


if __name__ == "__main__":
    main()
