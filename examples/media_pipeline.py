import asyncio
import os
import random
import time
import logging
import traceback
import multiprocessing
from datetime import datetime, timedelta
from stent import Stent, Result, RetryPolicy

# Disable extensive logging for the dashboard effect, only errors
logging.basicConfig(level=logging.ERROR)

# --- Configuration ---
NUM_ACTIVITY_WORKERS = 5
NUM_ORCHESTRATOR_WORKERS = 2
DB_PATH = "media_pipeline.sqlite"
LEASE_DURATION = timedelta(seconds=5)

# --- Activities ---

ACTIVITY_QUEUE = "processing_queue"

@Stent.durable(queue=ACTIVITY_QUEUE)
async def validate_upload(file_name: str) -> bool:
    await asyncio.sleep(random.uniform(0.5, 1.5))
    if "corrupt" in file_name:
        raise ValueError(f"File {file_name} header is corrupt")
    return True

@Stent.durable(retry_policy=RetryPolicy(max_attempts=3, initial_delay=1.0), queue=ACTIVITY_QUEUE)
async def scan_for_safety(file_name: str) -> str:
    await asyncio.sleep(random.uniform(1.0, 2.5))
    if random.random() < 0.05: raise ConnectionError("Safety API expiry")
    if random.random() < 0.02: return "flagged"
    return "clean"

@Stent.durable(queue=ACTIVITY_QUEUE, cached=True)
async def extract_audio(file_name: str) -> str:
    await asyncio.sleep(random.uniform(1.5, 3.0))
    return f"{file_name}.wav"

@Stent.durable(queue="gpu_tasks", cached=True)
async def transcribe_audio(audio_file: str) -> str:
    duration = random.uniform(3.0, 6.0)
    await asyncio.sleep(duration)
    return f"Transcription({int(duration)}s)"

@Stent.durable(queue="gpu_tasks", cached=True)
async def generate_thumbnails(file_name: str) -> list[str]:
    await asyncio.sleep(random.uniform(2.0, 4.0))
    return [f"thumb_{i}.jpg" for i in range(3)]

@Stent.durable(queue=ACTIVITY_QUEUE, cached=True)
async def generate_metadata(transcription: str) -> dict:
    await asyncio.sleep(random.uniform(1.0, 2.0))
    return {"tags": ["viral"], "sentiment": "positive"}

@Stent.durable(queue=ACTIVITY_QUEUE)
async def package_assets(file_name: str, transcription: str, thumbs: list[str], metadata: dict) -> str:
    await asyncio.sleep(random.uniform(0.5, 1.0))
    return f"s3://{file_name}.zip"

# --- Orchestrator ---

@Stent.durable()
async def media_processing_pipeline(file_name: str) -> Result[dict, Exception]:
    try:
        await validate_upload(file_name)
        
        status = await scan_for_safety(file_name)
        if status == "flagged": return Result.Error(Exception("Content flagged"))
            
        # Parallel branches
        results = await asyncio.gather(
            process_audio_chain(file_name),
            generate_thumbnails(file_name)
        )
        (transcription, metadata), thumbs = results
        
        final_url = await package_assets(file_name, transcription, thumbs, metadata)
        return Result.Ok({"status": "success", "url": final_url})
    except Exception as e:
        return Result.Error(e)

@Stent.durable()
async def process_audio_chain(file_name: str):
    audio_path = await extract_audio(file_name)
    text = await transcribe_audio(audio_path)
    meta = await generate_metadata(text)
    return text, meta

# --- Worker Process Entry Point ---

def run_worker_process(db_path, queues, worker_id, lease_duration_seconds):
    # Re-init logging for process
    logging.basicConfig(level=logging.ERROR)
    
    async def _run():
        backend = Stent.backends.SQLiteBackend(db_path)
        # No init_db here, assumed done by main
        executor = Stent(backend=backend)
        await executor.serve(
            max_concurrency=1,
            poll_interval=0.1,
            queues=queues,
            worker_id=worker_id,
            lease_duration=timedelta(seconds=lease_duration_seconds)
        )

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass

# --- Dashboard & Runner ---

def clear_screen():
    print("\033[H\033[J", end="")

def print_dashboard(statuses: list[dict], worker_activities: list, start_time: float, crash_count: int, total_jobs: int):
    clear_screen()
    elapsed = time.time() - start_time
    completed_count = sum(1 for s in statuses if s['state'] in ('completed', 'failed', 'timed_out'))
    
    print(f"--- Resilience Demo: Media Pipeline ({elapsed:.1f}s) ---")
    print(f"Active Worker Processes: {len(worker_activities)} | Total Process Crashes: {crash_count}")
    print(f"Total Jobs Dispatched: {total_jobs} | Completed: {completed_count} | Active: {len(statuses) - completed_count}")
    
    # --- Jobs Table (Last 15) ---
    print("\n[ LATEST JOBS ]")
    print("-" * 100)
    print(f"{'Job ID':<38} | {'State':<10} | {'Current Step':<35} | {'Steps'}")
    print("-" * 100)
    
    # Show recent jobs first
    for s in statuses[-20:]:
        progress_parts = s['progress'].split(" > ")
        last_step = progress_parts[-1] if progress_parts else "Initializing"
        
        cache_hits = sum(1 for p in progress_parts if "cache_hit" in p)
        if cache_hits > 0:
            last_step += f" ({cache_hits} recovered)"

        if len(last_step) > 35: last_step = "..." + last_step[-32:]
            
        color = ""
        if s['state'] == "running": color = "\033[94m" # Blue
        elif s['state'] == "completed": color = "\033[92m" # Green
        elif s['state'] == "failed": color = "\033[91m" # Red
        reset = "\033[0m"
        
        print(f"{s['id']:<38} | {color}{s['state']:<10}{reset} | {last_step:<35} | {len(progress_parts)}")
    
    # --- Workers Table ---
    print("\n[ WORKERS ]")
    print("-" * 100)
    print(f"{'Worker ID':<20} | {'Type':<12} | {'Task':<45} | {'Age'}")
    print("-" * 100)
    
    worker_activities.sort(key=lambda x: x.get('type', ''))
    
    for w in worker_activities:
        task = w.get('task')
        wid = w['id']
        wtype = w['type']
        
        if task:
            step = task.step_name
            if task.retries > 0:
                step += f" (Retry {task.retries})"
            
            duration_s = ""
            if task.started_at:
                d = (datetime.now() - task.started_at).total_seconds()
                duration_s = f"{d:.1f}s"
            
            print(f"{wid:<20} | {wtype:<12} | {step:<45} | {duration_s}")
        else:
            print(f"{wid:<20} | {wtype:<12} | {'Idle / Polling...':<45} | -")
    
    print("-" * 100)

async def main():
    # Use spawn for consistency across platforms
    multiprocessing.set_start_method("spawn", force=True)
    
    if os.path.exists(DB_PATH): os.remove(DB_PATH)
    backend = Stent.backends.SQLiteBackend(DB_PATH)
    await backend.init_db()
    executor = Stent(backend=backend)
    
    # We store metadata about processes here
    # List of dicts: {'id': str, 'type': str, 'process': Process, 'queues': list}
    active_processes = []
    
    def spawn_worker(wtype, queues, index):
        wid = f"{wtype}-{index}"
        p = multiprocessing.Process(
            target=run_worker_process,
            args=(DB_PATH, queues, wid, LEASE_DURATION.total_seconds()),
            name=wid
        )
        p.start()
        active_processes.append({
            'id': wid,
            'type': wtype,
            'process': p,
            'queues': queues,
            'index': index
        })

    def ensure_workers():
        # Check for dead processes and remove them from list (Chaos Monkey kills them)
        # Actually Chaos Monkey will kill them, we just need to respawn missing ones.
        
        # Count active by type/index
        # We want strict slots: Activity-0 to Activity-4, Orchestrator-0 to Orchestrator-1
        
        existing_map = {(w['type'], w['index']): w for w in active_processes if w['process'].is_alive()}
        
        # Update active list to only alive ones
        active_processes[:] = existing_map.values()
        
        for i in range(NUM_ACTIVITY_WORKERS):
            if ('Activity', i) not in existing_map:
                spawn_worker("Activity", [ACTIVITY_QUEUE, "gpu_tasks"], i)
                
        for i in range(NUM_ORCHESTRATOR_WORKERS):
            if ('Orchestrator', i) not in existing_map:
                spawn_worker("Orchestrator", [None], i)

    ensure_workers()
    
    job_ids = []
    crash_count = 0
    start_time = time.time()
    
    # --- Loops ---
    
    async def spawner():
        i = 0
        while True:
            fname = f"video_{{i:03d}}.mp4"
            if i % 10 == 0: fname = "corrupt.mp4"
            eid = await executor.dispatch(media_processing_pipeline, fname)
            job_ids.append(eid)
            i += 1
            await asyncio.sleep(5.0)

    async def chaos_monkey():
        nonlocal crash_count
        while True:
            await asyncio.sleep(random.uniform(5.0, 8.0))
            if active_processes:
                victim = random.choice(active_processes)
                if victim['process'].is_alive():
                    victim['process'].terminate()
                    victim['process'].join()
                    crash_count += 1
                    # Remove from list immediately so main loop sees it missing
                    if victim in active_processes:
                        active_processes.remove(victim)
                    ensure_workers() # Respawn immediately

    spawner_task = asyncio.create_task(spawner())
    chaos_task = asyncio.create_task(chaos_monkey())
    
    try:
        while True:
            ensure_workers()
            
            # Prune job history
            if len(job_ids) > 50:
                job_ids[:] = job_ids[-50:]
            
            statuses = []
            for eid in job_ids:
                state = await executor.state_of(eid)
                statuses.append({
                    "id": eid,
                    "state": state.state,
                    "progress": state.progress_str
                })
            
            # Get DB view of who is working
            running_tasks = await executor.get_running_activities()
            worker_map = {t.worker_id: t for t in running_tasks}
            
            # Prepare display data merging Process list with DB tasks
            display_workers = []
            for w in active_processes:
                w_copy = {'id': w['id'], 'type': w['type']}
                w_copy['task'] = worker_map.get(w['id'])
                display_workers.append(w_copy)
            
            print_dashboard(statuses, display_workers, start_time, crash_count, len(job_ids))
            
            await asyncio.sleep(0.2)
            
    except asyncio.CancelledError:
        pass
    finally:
        spawner_task.cancel()
        chaos_task.cancel()
        for w in active_processes:
            if w['process'].is_alive():
                w['process'].terminate()
        print("\nSimulation Stopped.")

if __name__ == "__main__":
    asyncio.run(main())