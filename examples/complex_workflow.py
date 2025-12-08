import asyncio
import os
import logging
from datetime import timedelta
from dfns import DFns, Result, RetryPolicy

logging.basicConfig(level=logging.INFO)

# --- Durable Functions Definition ---

@DFns.durable(retry_policy=RetryPolicy(max_attempts=3, initial_delay=0.1))
async def fetch_external_data(url: str) -> Result[str, Exception]:
    # Simulate external API call that might fail
    print(f"[Task] Fetching data from {url}")
    await asyncio.sleep(0.3)
    if "fail" in url:
        raise ConnectionError(f"Failed to connect to {url}")
    return Result.Ok(f"<data from {url}>")

@DFns.durable(cached=True, version="v1.0")
async def process_data_heavy(raw_data: str) -> Result[str, Exception]:
    # Simulate a CPU-bound or idempotent data processing task
    print(f"[Task] Processing data: {raw_data[:20]}...")
    await asyncio.sleep(0.7)
    processed = raw_data.upper().replace("DATA", "PROCESSED_DATA")
    return Result.Ok(processed)

@DFns.durable(idempotent=True, queue="notifications")
async def send_notification(recipient: str, subject: str, body: str) -> Result[bool, Exception]:
    # Simulate sending an email or other notification
    print(f"[Task] Sending notification to {recipient} with subject: {subject}")
    await asyncio.sleep(0.2)
    if "error" in recipient:
        raise ValueError("Invalid recipient for notification")
    return Result.Ok(True)

@DFns.durable(tags=["orchestrator", "pipeline"])
async def complex_data_pipeline(config: dict) -> Result[dict, Exception]:
    print("[Orchestrator] Starting complex data pipeline")

    # Step 1: Fetch data (can retry)
    url1 = config.get("url1", "http://example.com/data1")
    fetch_result1 = await fetch_external_data(url1)
    if not fetch_result1.ok:
        print(f"[Orchestrator] Failed to fetch data1: {fetch_result1.error}")
        return Result.Error(Exception(f"Pipeline failed at fetch_external_data: {fetch_result1.error}"))
    raw_data1 = fetch_result1.or_raise()

    url2 = config.get("url2", "http://example.com/data2")
    fetch_result2 = await fetch_external_data(url2)
    if not fetch_result2.ok:
        print(f"[Orchestrator] Failed to fetch data2: {fetch_result2.error}")
        return Result.Error(Exception(f"Pipeline failed at fetch_external_data: {fetch_result2.error}"))
    raw_data2 = fetch_result2.or_raise()

    combined_data = raw_data1 + raw_data2

    # Step 2: Process combined data (cached)
    process_result = await process_data_heavy(combined_data)
    if not process_result.ok:
        print(f"[Orchestrator] Failed to process data: {process_result.error}")
        return Result.Error(Exception(f"Pipeline failed at process_data_heavy: {process_result.error}"))
    processed_output = process_result.or_raise()

    # Step 3: Send notification (idempotent, specific queue)
    recipient = config.get("notification_email", "admin@example.com")
    notification_subject = "Data Pipeline Complete"
    notification_body = f"Your data pipeline finished successfully. Output: {processed_output[:50]}..."
    
    notification_result = await send_notification(recipient, notification_subject, notification_body)
    if not notification_result.ok:
        print(f"[Orchestrator] Failed to send notification: {notification_result.error}")
        # Optionally, fail the whole pipeline or just log the notification error
        return Result.Error(Exception(f"Pipeline completed with notification error: {notification_result.error}"))

    print("[Orchestrator] Complex data pipeline finished successfully")
    return Result.Ok({"final_output": processed_output, "notification_sent": notification_result.or_raise()})

# --- Main Execution Logic ---

async def run_pipeline(executor: DFns, config: dict, expiry_str: str | None = None):
    print(f"\n--- Running Pipeline with config: {config} ---")
    execution_id = await executor.dispatch(complex_data_pipeline, config, expiry=expiry_str)
    print(f"Dispatched pipeline execution ID: {execution_id}")

    while True:
        state = await executor.state_of(execution_id)
        print(f"  State: {state.state}, Progress: {state.progress_str}")
        if state.state in ("completed", "failed", "timed_out"):
            break
        await asyncio.sleep(0.5)
    
    try:
        final_result = await executor.result_of(execution_id)
        print(f"  Final Result: {final_result}")
    except Exception as e:
        print(f"  Error retrieving result: {e}")
    print("---------------------------------------------------")
    return state.state

async def main():
    db_path = "complex_pipeline.sqlite"
    if os.path.exists(db_path):
        os.remove(db_path)
        
    backend = DFns.backends.SQLiteBackend(db_path)
    await backend.init_db()
    
    DFns.mute_async_sleep_notifications = True
    executor = DFns(
        backend=backend, 
        notification_backend=DFns.notifications.RedisBackend("redis://localhost:6379")
        )
    
    # Start a worker that listens to all queues for now
    print("Starting a general worker...")
    worker_task = asyncio.create_task(executor.serve(poll_interval=0.2))

    # Scenario 1: Successful run
    await run_pipeline(executor, {"url1": "http://good.com/dataA", "notification_email": "success@example.com"})
    
    # Scenario 2: Simulate failure in fetch_external_data (retryable)
    # The worker will retry 'fetch_external_data' for 'fail.com' twice (total 3 attempts).
    # After 3 failures, it will be marked failed, and orchestrator will catch it.
    await run_pipeline(executor, {"url1": "http://fail.com/data", "notification_email": "success@example.com"})

    # Scenario 3: Cached processing - should be faster
    # First run: process data normally
    await run_pipeline(executor, {"url1": "http://cache.com/data1", "url2": "http://cache.com/data2", "notification_email": "cache@example.com"})
    # Second run with same data for processing (process_data_heavy is cached=True) should hit cache
    await run_pipeline(executor, {"url1": "http://cache.com/data1", "url2": "http://cache.com/data2", "notification_email": "cache_again@example.com"})
    
    # Scenario 4: expiry an execution
    await run_pipeline(executor, {"url1": "http://slow.com/data", "url2": "http://slow.com/data"}, expiry_str="0.1s") # Will expiry due to long process_data_heavy

    # Scenario 5: Notification failure (non-retryable within send_notification if it expects clean recipient)
    # Note: send_notification only has basic exception handling, not specific retry_for for ValueError
    # But if the orchestrator is set to retry this, it will.
    # For this example, send_notification is idempotent, but if it fails, it will be marked failed.
    await run_pipeline(executor, {"notification_email": "error_recipient@example.com"})

    print("Shutting down worker.")
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
    print("Examples finished.")

if __name__ == "__main__":
    asyncio.run(main())
