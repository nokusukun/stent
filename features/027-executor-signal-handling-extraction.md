# Executor Signal Handling Extraction

## Description
Continued P2.3 by extracting signal send/wait behavior into a dedicated helper module while preserving existing workflow and API behavior.

## Key Changes
* `stent/executor_signals.py`
  * Added deterministic signal task-id and step-name helpers.
  * Added `persist_signal_and_wake_waiter` for buffered signal persistence and waiter wake-up.
  * Added `resolve_signal_wait` for replay-safe signal wait resolution (existing task, buffered signal, or pending wait).
* `stent/executor.py`
  * `send_signal` now delegates to the new helper module.
  * `wait_for_signal_instance` now delegates to the new helper module.
  * Removed now-unused direct `SignalRecord` import.

## Usage/Configuration
```python
# Public API is unchanged.
await executor.send_signal(execution_id, "my_signal", payload)
value = await Stent.wait_for_signal("my_signal")
```
