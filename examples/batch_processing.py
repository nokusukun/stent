import asyncio
import os
import logging
import random
from senpuki import Senpuki, Result

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger("BatchExample")

# --- Activities ---

@Senpuki.durable()
async def download_image(image_id: int) -> str:
    # Simulate network IO
    delay = random.uniform(0.1, 0.5)
    await asyncio.sleep(delay)
    # Simulate occasional network failure
    if random.random() < 0.1:
        raise ConnectionError(f"Network error downloading image {image_id}")
    
    path = f"/tmp/img_{image_id}.jpg"
    logger.info(f"Downloaded image {image_id} to {path} ({delay:.2f}s)")
    return path

@Senpuki.durable()
async def process_image(path: str) -> str:
    # Simulate CPU intensive work
    await asyncio.sleep(0.2)
    processed_path = path.replace(".jpg", "_bw.jpg")
    logger.info(f"Processed {path} -> {processed_path}")
    return processed_path

@Senpuki.durable()
async def create_gallery(image_paths: list[str]) -> str:
    await asyncio.sleep(0.5)
    logger.info(f"Creating gallery from {len(image_paths)} images")
    return f"http://example.com/gallery/{len(image_paths)}_images"

# --- Orchestrator ---

@Senpuki.durable()
async def batch_image_workflow(image_ids: list[int]) -> Result[str, Exception]:
    logger.info(f"Starting batch workflow for {len(image_ids)} images")
    
    # Fan-out: Download all images in parallel
    download_tasks = []
    for img_id in image_ids:
        # We can fire off tasks. If one fails, we might want to handle it.
        # asyncio.gather will raise the first exception by default.
        # In a real batch, maybe we want return_exceptions=True to process partials.
        download_tasks.append(download_image(img_id))
    
    # Wait for downloads
    # To handle partial failures, we would wrap download_image in a safe version or use return_exceptions.
    # Here we assume we want all or nothing for simplicity, but let's use return_exceptions=True to show robustness.
    download_results = await asyncio.gather(*download_tasks, return_exceptions=True)
    
    successful_downloads = []
    for i, res in enumerate(download_results):
        if isinstance(res, Exception):
            logger.warning(f"Failed to download image {image_ids[i]}: {res}")
        else:
            successful_downloads.append(res)
            
    if not successful_downloads:
        return Result.Error(Exception("No images downloaded successfully"))

    # Fan-out: Process downloaded images
    process_tasks = [process_image(path) for path in successful_downloads]
    processed_paths = await asyncio.gather(*process_tasks)
    
    # Fan-in: Create gallery
    gallery_url = await create_gallery(processed_paths)
    
    return Result.Ok(gallery_url)

# --- Runner ---

async def main():
    db_path = "batch_example.sqlite"
    if os.path.exists(db_path):
        os.remove(db_path)
    
    backend = Senpuki.backends.SQLiteBackend(db_path)
    await backend.init_db()
    
    executor = Senpuki(backend=backend)
    
    # Start worker with concurrency
    worker_task = asyncio.create_task(executor.serve(poll_interval=0.1, max_concurrency=20))
    
    logger.info("Dispatching workflow...")
    # Process 10 images
    ids = list(range(1, 11))
    exec_id = await executor.dispatch(batch_image_workflow, ids)
    
    # Monitor
    while True:
        state = await executor.state_of(exec_id)
        if state.state in ("completed", "failed", "timed_out"):
            break
        await asyncio.sleep(0.5)
        
    result = await executor.result_of(exec_id)
    logger.info(f"Final Result: {result}")
    
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass

if __name__ == "__main__":
    asyncio.run(main())
