import asyncio
import os
import random
import time
import logging
from datetime import datetime
from dfns import DFns, Result, RetryPolicy

# Disable extensive logging for the dashboard effect, only errors
logging.basicConfig(level=logging.ERROR)

# --- Configuration ---
NUM_WORKERS = 5
NUM_JOBS = 20
DB_PATH = "media_pipeline.sqlite"

# --- Activities ---

# We assign activities to specific queues to avoid blocking the orchestrator slots
# The orchestrators will run on the default (None) queue.
ACTIVITY_QUEUE = "processing_queue"

@DFns.durable(queue=ACTIVITY_QUEUE)
async def validate_upload(file_name: str) -> bool:
    # Simulate checking file format and headers
    await asyncio.sleep(random.uniform(1.0, 3.0))
    if "corrupt" in file_name:
        raise ValueError(f"File {file_name} header is corrupt")
    return True

@DFns.durable(retry_policy=RetryPolicy(max_attempts=3, initial_delay=1.0), queue=ACTIVITY_QUEUE)
async def scan_for_safety(file_name: str) -> str:
    # Simulate calling an external safety API (virus/moderation)
    await asyncio.sleep(random.uniform(2.0, 5.0))
    # 5% chance of transient failure (retryable)
    if random.random() < 0.05:
        raise ConnectionError("Safety API timeout")
    # 5% chance of content violation
    if random.random() < 0.05:
        return "flagged"
    return "clean"

@DFns.durable(queue=ACTIVITY_QUEUE)
async def extract_audio(file_name: str) -> str:
    # Simulate ffmpeg extraction
    await asyncio.sleep(random.uniform(3.0, 8.0))
    return f"{file_name}.wav"

@DFns.durable(queue="gpu_tasks") # specialized queue
async def transcribe_audio(audio_file: str) -> str:
    # Heavy AI task
    duration = random.uniform(15.0, 30.0)
    await asyncio.sleep(duration)
    return f"Transcription of {audio_file} ({int(duration)}s)"

@DFns.durable(queue="gpu_tasks")
async def generate_thumbnails(file_name: str) -> list[str]:
    # Image processing
    await asyncio.sleep(random.uniform(5.0, 12.0))
    return [f"{file_name}_thumb_{i}.jpg" for i in range(3)]

@DFns.durable(queue=ACTIVITY_QUEUE)
async def generate_metadata(transcription: str) -> dict:
    # NLP summary and tagging
    await asyncio.sleep(random.uniform(3.0, 6.0))
    return {
        "summary": "AI Generated Summary...",
        "tags": ["viral", "video", "processing"],
        "sentiment": "positive"
    }

@DFns.durable(queue=ACTIVITY_QUEUE)
async def package_assets(file_name: str, transcription: str, thumbs: list[str], metadata: dict) -> str:
    # Final DB update / Zip
    await asyncio.sleep(random.uniform(1.0, 2.0))
    return f"s3://bucket/processed/{file_name}.zip"

# --- Orchestrator ---

@DFns.durable()
async def media_processing_pipeline(file_name: str) -> Result[dict, Exception]:
    try:
        # Step 1: Validation
        await validate_upload(file_name)
        
        # Step 2: Safety Scan
        safety_status = await scan_for_safety(file_name)
        if safety_status == "flagged":
            return Result.Error(Exception("Content flagged by safety scan"))
            
        # Step 3: Fan-out (Audio Extraction & Thumbnails)
        # We need audio for transcription, but thumbnails can happen independently of audio.
        # However, extracting audio depends on the file.
        # Let's run Audio Extraction and Thumbnails in parallel.
        
        # Branch A: Audio -> Transcribe -> Metadata
        async def audio_branch():
            audio_path = await extract_audio(file_name)
            text = await transcribe_audio(audio_path)
            meta = await generate_metadata(text)
            return text, meta

        # Branch B: Thumbnails
        async def thumb_branch():
            return await generate_thumbnails(file_name)

        # Run branches
        # Note: DFns supports awaiting coroutines directly if they are durable calls or orchestrations.
        # But here `audio_branch` is a local helper function invoking durable activities.
        # If we invoke it directly, it runs locally until it hits the first `await`.
        # To make the *branches* execute in parallel on workers, we should wrap them in their own sub-orchestrators
        # OR just schedule the tasks they depend on.
        # Ideally: 
        # t1 = extract_audio(...)
        # t2 = generate_thumbnails(...)
        # This only parallelizes the *first* step of each branch. 
        # To fully parallelize chain A vs chain B, we should put them in separate @durable functions.
        
        results = await asyncio.gather(
            process_audio_chain(file_name),
            generate_thumbnails(file_name)
        )
        
        (transcription, metadata), thumbs = results
        
        # Step 4: Finalize
        final_url = await package_assets(file_name, transcription, thumbs, metadata)
        
        return Result.Ok({
            "status": "success",
            "url": final_url,
            "meta": metadata
        })

    except Exception as e:
        return Result.Error(e)

@DFns.durable()
async def process_audio_chain(file_name: str):
    # Sub-orchestrator to bundle the sequential audio steps
    audio_path = await extract_audio(file_name)
    text = await transcribe_audio(audio_path)
    meta = await generate_metadata(text)
    return text, meta

# --- Dashboard & Runner ---

def clear_screen():
    print("\033[H\033[J", end="")

def print_dashboard(statuses: list[dict], worker_activities: list, start_time: float):
    clear_screen()
    elapsed = time.time() - start_time
    completed_count = sum(1 for s in statuses if s['state'] in ('completed', 'failed', 'timed_out'))
    
    print(f"--- Media Processing Pipeline Dashboard ({elapsed:.1f}s) ---")
    print(f"Workers: {NUM_WORKERS} | Jobs: {len(statuses)} | Completed: {completed_count}")
    
    # --- Jobs Table ---
    print("\n[ JOBS ]")
    print("-" * 80)
    print(f"{'Job ID':<38} | {'State':<10} | {'Current Step':<25}")
    print("-" * 80)
    
    for s in statuses:
        # Parse progress string to find latest step
        progress_parts = s['progress'].split(" > ")
        last_step = progress_parts[-1] if progress_parts else "Initializing"
        if len(last_step) > 25:
            last_step = "..." + last_step[-22:]
            
        color = ""
        if s['state'] == "running": color = "\033[94m" # Blue
        elif s['state'] == "completed": color = "\033[92m" # Green
        elif s['state'] == "failed": color = "\033[91m" # Red
        reset = "\033[0m"
        
        print(f"{s['id']:<38} | {color}{s['state']:<10}{reset} | {last_step:<25}")
    
    # --- Workers Table ---
    print("\n[ ACTIVE WORKERS ]")
    print("-" * 80)
    print(f"{'Worker ID':<38} | {'Step Name':<35}")
    print("-" * 80)
    
    printed_workers = set()
    if not worker_activities:
        print(f"{'Idle':<38} | {'Waiting for tasks...':<35}")
    else:
        for task in worker_activities:
            if task.worker_id in printed_workers:
                continue
            printed_workers.add(task.worker_id)
            wid = task.worker_id or "Unknown"
            step = task.step_name
            duration_s = ""
            if task.started_at:
                d = (datetime.now() - task.started_at).total_seconds()
                duration_s = f"({d:.1f}s)"
            
            print(f"{wid:<38} | {step} {duration_s}")
    
    print("-" * 80)

async def main():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        
    backend = DFns.backends.SQLiteBackend(DB_PATH)
    await backend.init_db()
    
    # We need a notification backend for efficient wait, though we'll polling for the dashboard
    executor = DFns(backend=backend)
    
    # Start Workers
    # To prevent deadlocks where orchestrators fill all slots and wait for activities that can't start,
    # we separate the worker pools.
    
    print("Starting workers...")
    worker_tasks = []
    
    # 5 Activity Workers (Process the heavy lifting)
    for i in range(NUM_WORKERS):
        # Listen to activity queue and GPU queue
        t = asyncio.create_task(executor.serve(
            max_concurrency=1, 
            poll_interval=0.1,
            queues=[ACTIVITY_QUEUE, "gpu_tasks", None],
            worker_id=f"Worker-{i+1}"
        ))
        worker_tasks.append(t)

    # # 3 Orchestrator Workers (Manage the workflow state)
    # # They listen to the default queue (None) where orchestrators are dispatched
    # for i in range(3):
    #     t = asyncio.create_task(executor.serve(
    #         max_concurrency=1, 
    #         poll_interval=0.1,
    #         queues=[None], # Listen to default queue
    #         worker_id=f"Orchestrator-{i+1}"
    #     ))
    #     worker_tasks.append(t)
    
    # Dispatch Jobs
    print(f"Dispatching {NUM_JOBS} jobs...")
    job_ids = []
    for i in range(NUM_JOBS):
        fname = f"video_{i:02d}.mp4"
        # Add some "corrupt" files to show failures
        if i == 5: fname = "corrupt_video.mp4"
        eid = await executor.dispatch(media_processing_pipeline, fname)
        job_ids.append(eid)
    
    start_time = time.time()
    
    # Monitor Loop
    while True:
        # Gather stats
        statuses = []
        all_done = True
        
        for eid in job_ids:
            state = await executor.state_of(eid)
            statuses.append({
                "id": eid,
                "state": state.state,
                "progress": state.progress_str
            })
            if state.state not in ("completed", "failed", "timed_out"):
                all_done = False
        
        # Fetch active workers (running tasks)
        running_tasks = await executor.get_running_activities()
        active_workers = [t for t in running_tasks if t.worker_id]
        
        print_dashboard(statuses, active_workers, start_time)
        
        if all_done:
            break
        
        await asyncio.sleep(0.5)
        
    print("\nAll jobs finished.")
    for t in worker_tasks:
        t.cancel()
    await asyncio.gather(*worker_tasks, return_exceptions=True)

if __name__ == "__main__":
    asyncio.run(main())
