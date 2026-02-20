"""
Senpuki CLI - Command-line interface for managing Senpuki executions.

Usage:
    senpuki [--db PATH] <command> [options]

Commands:
    list        List executions
    show        Show execution details
    tasks       List/show tasks
    stats       Show queue statistics
    dlq         Dead-letter queue operations
    cleanup     Clean up old executions
    signal      Send signal to execution
    watch       Live monitoring (requires rich)

Environment:
    SENPUKI_DB  Default database path (default: senpuki.sqlite)
"""
import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from typing import Any, cast

from senpuki import Senpuki, ExecutionState
from senpuki.core import TaskRecord, DeadLetterRecord

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

async def list_executions(executor: Senpuki, args):
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


async def show_execution(executor: Senpuki, args):
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

async def list_tasks(executor: Senpuki, args):
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


async def show_task(executor: Senpuki, args):
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

async def show_stats(executor: Senpuki, args):
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
    print(f"\n{Colors.BOLD}Senpuki Statistics{Colors.RESET}")
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

async def dlq_list(executor: Senpuki, args):
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


async def dlq_show(executor: Senpuki, args):
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


async def dlq_replay(executor: Senpuki, args):
    """Replay a dead-letter entry."""
    try:
        new_id = await executor.replay_dead_letter(args.id, queue=args.queue)
        print(f"{Colors.GREEN}✓{Colors.RESET} Replayed dead-letter task {args.id}")
        print(f"  New task ID: {new_id}")
    except ValueError as e:
        print(f"{Colors.RED}Error:{Colors.RESET} {e}")
        return 1


async def dlq_discard(executor: Senpuki, args):
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


async def dlq_replay_all(executor: Senpuki, args):
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

async def cleanup(executor: Senpuki, args):
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

async def send_signal(executor: Senpuki, args):
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

async def _watch_simple(executor: Senpuki, args):
    """Simple fallback watch mode without rich."""
    print(f"{Colors.BOLD}Senpuki Watch Mode{Colors.RESET} (press Ctrl+C to exit)")
    print("-" * 50)
    print(f"{Colors.DIM}Tip: Install 'rich' for a better experience{Colors.RESET}\n")
    
    try:
        while True:
            # Clear screen (basic)
            if os.name == "nt":
                os.system("cls")
            else:
                print("\033[2J\033[H", end="")
            
            print(f"{Colors.BOLD}Senpuki Watch{Colors.RESET} - {datetime.now().strftime('%H:%M:%S')}")
            print("=" * 50)
            
            # Quick stats
            pending = await executor.backend.count_tasks(state="pending")
            running = await executor.backend.count_tasks(state="running")
            dlq = await executor.backend.count_dead_tasks()
            
            print(f"\nPending: {Colors.YELLOW}{pending}{Colors.RESET}  "
                  f"Running: {Colors.BLUE}{running}{Colors.RESET}  "
                  f"DLQ: {Colors.RED}{dlq}{Colors.RESET}")
            
            # Recent executions
            print(f"\n{Colors.BOLD}Recent Executions{Colors.RESET}")
            executions = await executor.list_executions(limit=10)
            for exc in executions:
                print(f"  {exc.id[:8]}... {state_color(exc.state)}")
            
            print(f"\n{Colors.DIM}Refreshing in {args.interval}s...{Colors.RESET}")
            await asyncio.sleep(args.interval)
            
    except KeyboardInterrupt:
        print("\nExiting watch mode.")


async def _watch_rich(executor: Senpuki, args):
    """Rich-based watch mode with fancy UI."""
    from rich.console import Console  # type: ignore
    from rich.live import Live  # type: ignore
    from rich.table import Table  # type: ignore
    from rich.panel import Panel  # type: ignore
    from rich.layout import Layout  # type: ignore
    
    console = Console()
    
    def make_layout():
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="main"),
            Layout(name="footer", size=3),
        )
        layout["main"].split_row(
            Layout(name="stats", ratio=1),
            Layout(name="executions", ratio=2),
        )
        return layout
    
    async def generate_display():
        # Stats
        pending = await executor.backend.count_tasks(state="pending")
        running = await executor.backend.count_tasks(state="running")
        completed = await executor.backend.count_tasks(state="completed")
        failed = await executor.backend.count_tasks(state="failed")
        dlq = await executor.backend.count_dead_tasks()
        
        stats_table = Table(show_header=False, box=None)
        stats_table.add_row("Pending", f"[yellow]{pending}[/]")
        stats_table.add_row("Running", f"[blue]{running}[/]")
        stats_table.add_row("Completed", f"[green]{completed}[/]")
        stats_table.add_row("Failed", f"[red]{failed}[/]")
        stats_table.add_row("DLQ", f"[red]{dlq}[/]")
        
        # Executions
        exec_table = Table(title="Recent Executions")
        exec_table.add_column("ID", style="dim")
        exec_table.add_column("State")
        exec_table.add_column("Started")
        
        executions = await executor.list_executions(limit=15)
        for exc in executions:
            state_style = {
                "pending": "yellow",
                "running": "blue",
                "completed": "green",
                "failed": "red",
            }.get(exc.state, "white")
            
            started = format_time_short(exc.started_at)
            exec_table.add_row(
                exc.id[:12] + "...",
                f"[{state_style}]{exc.state}[/]",
                started
            )
        
        layout = make_layout()
        layout["header"].update(Panel(f"[bold]Senpuki Watch[/] - {datetime.now().strftime('%H:%M:%S')}"))
        layout["stats"].update(Panel(stats_table, title="Stats"))
        layout["executions"].update(exec_table)
        layout["footer"].update(Panel("[dim]Press Ctrl+C to exit[/]"))
        
        return layout
    
    try:
        with Live(await generate_display(), console=console, refresh_per_second=1) as live:
            while True:
                await asyncio.sleep(args.interval)
                live.update(await generate_display())
    except KeyboardInterrupt:
        console.print("\n[dim]Exiting watch mode.[/]")


async def watch(executor: Senpuki, args):
    """Live monitoring of executions and queues."""
    try:
        import rich  # type: ignore  # noqa: F401
        has_rich = True
    except ImportError:
        has_rich = False
    
    if has_rich:
        await _watch_rich(executor, args)
    else:
        await _watch_simple(executor, args)


# ============================================================================
# MAIN
# ============================================================================

async def main_async():
    parser = argparse.ArgumentParser(
        description="Senpuki CLI - Manage distributed durable functions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  senpuki list                          List recent executions
  senpuki list --state running          List running executions
  senpuki show <execution-id>           Show execution details
  senpuki stats                         Show queue statistics
  senpuki dlq list                      List dead-letter queue
  senpuki dlq replay <task-id>          Replay failed task
  senpuki cleanup --days 7              Clean up old records
  senpuki signal <exec-id> my_signal    Send signal
  senpuki watch                         Live monitoring
        """
    )
    
    default_db = os.environ.get("SENPUKI_DB", "senpuki.sqlite")
    parser.add_argument(
        "--db", 
        default=default_db, 
        help=f"Database path or connection string (default: {default_db}, env: SENPUKI_DB)"
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
        backend = Senpuki.backends.PostgresBackend(args.db)
    else:
        backend = Senpuki.backends.SQLiteBackend(args.db)

    await backend.init_db()
    executor = Senpuki(backend=backend)
    
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
