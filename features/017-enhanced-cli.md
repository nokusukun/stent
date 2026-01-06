# Enhanced CLI

## Description

Comprehensive overhaul of the Senpuki command-line interface, adding new commands for managing executions, tasks, dead-letter queues, and real-time monitoring. The CLI now provides colored output, better formatting, and a live watch mode.

## Key Changes

* `senpuki/cli.py` - Complete rewrite with new subcommands and formatting
  - Added ANSI color support with automatic Windows compatibility
  - Added `tasks list/show` for task inspection
  - Added `stats` for queue statistics and system overview
  - Added `cleanup` for removing old executions and DLQ entries
  - Added `signal` for sending signals to running executions
  - Added `watch` for live monitoring (with optional `rich` library support)
  - Added `dlq discard` and `dlq replay-all` commands
  - Improved output formatting with duration calculations and state colors

## Usage/Configuration

### Basic Commands

```bash
# List executions
senpuki list
senpuki list --limit 50 --state running

# Show execution details
senpuki show <execution-id>

# Show queue statistics
senpuki stats

# List/show tasks
senpuki tasks list
senpuki tasks list --state pending
senpuki tasks show <task-id>
```

### Dead-Letter Queue

```bash
# List DLQ entries
senpuki dlq list

# Show details
senpuki dlq show <task-id>

# Replay a failed task
senpuki dlq replay <task-id>
senpuki dlq replay <task-id> --queue high-priority

# Discard a task
senpuki dlq discard <task-id> --force

# Replay all failed tasks
senpuki dlq replay-all --force
```

### Cleanup

```bash
# Clean up records older than 30 days (default)
senpuki cleanup

# Clean up records older than 7 days
senpuki cleanup --days 7

# Clean up only executions or DLQ
senpuki cleanup --executions
senpuki cleanup --dlq

# Dry run to see what would be deleted
senpuki cleanup --dry-run

# Skip confirmation
senpuki cleanup --days 7 --force
```

### Signals

```bash
# Send a simple signal
senpuki signal <execution-id> approval_received

# Send signal with payload
senpuki signal <execution-id> user_input "some data"

# Send JSON payload
senpuki signal <execution-id> config '{"enabled": true}' --json
```

### Live Monitoring

```bash
# Start watch mode (refreshes every 2 seconds)
senpuki watch

# Custom refresh interval
senpuki watch --interval 5

# Note: Install 'rich' for enhanced watch UI
pip install rich
```

### Configuration

```bash
# Use environment variable
export SENPUKI_DB=postgres://user:pass@localhost/senpuki
senpuki list

# Or command-line argument
senpuki --db myapp.sqlite list

# Disable colors
senpuki --no-color list
```

## Output Examples

### Stats Command
```
Senpuki Statistics
==================================================

Executions
  Total:     156
  Pending    12
  Running    5
  Completed  130
  Failed     9

Tasks
  Total:     892
  Pending    45
  Running    5
  Completed  842

Queue Depths
  default         23 ███████████████████████
  high-priority    8 ████████
  background      14 ██████████████

Dead Letter Queue
  Count:     3

Running Tasks (5)
  process_order                  worker=worker-1
  send_notification              worker=worker-2
  ...
```

### Execution Show
```
Execution: a1b2c3d4-e5f6-7890-abcd-ef1234567890
============================================================
  State:      running
  Queue:      default
  Priority:   5
  Tags:       user:123, order:456

Timing
  Started:    2024-01-15 14:30:22
  Completed:  -
  Duration:   5m 23s

Progress (8 steps)
  [14:30:22] ✓ validate_order
  [14:30:23] ✓ check_inventory
  [14:30:25] ✓ process_payment
  [14:35:45] ▶ ship_order
             Detail: Waiting for carrier response
```
