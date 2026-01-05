# Execution Heartbeats & Adaptive Polling

## Description
Adds the second phase of the hardening plan by keeping long-running work safely leased, reducing database pressure when Redis notifications are unavailable, and eliminating the global `asyncio.sleep` monkey patch.

## Key Changes
* Executors and backends gained a lease renewal API so workers heartbeat long activities instead of timing out mid-flight.
* Waiting loops now support adaptive backoff (min/max interval plus multiplier) for both orchestration waiters and worker claim loops.
* Removed the global `asyncio.sleep` patch in favor of documentation and targeted tests, ensuring third-party event loop code is unaffected.

## Usage/Configuration
```python
executor = Senpuki(
    backend=backend,
    poll_min_interval=0.25,
    poll_max_interval=3.0,
    poll_backoff_factor=1.5,
)

await executor.serve(
    lease_duration=timedelta(minutes=5),
    heartbeat_interval=timedelta(minutes=2),
    poll_interval=0.5,
    poll_interval_max=5.0,
    poll_backoff_factor=2.0,
)
```
