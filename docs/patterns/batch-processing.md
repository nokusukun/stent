# Batch Processing

This guide covers patterns for processing large datasets efficiently with Stent.

## Overview

Batch processing involves:

1. **Chunking** - Breaking large datasets into manageable pieces
2. **Parallel execution** - Processing multiple items concurrently
3. **Partial failure handling** - Continuing despite individual failures
4. **Progress tracking** - Monitoring completion status
5. **Result aggregation** - Combining results from all items

## Basic Batch Pattern

```python
from stent import Stent, Result
import asyncio

@Stent.durable()
async def process_item(item_id: int) -> dict:
    """Process a single item."""
    # Simulate processing
    await asyncio.sleep(0.1)
    return {"id": item_id, "status": "processed"}

@Stent.durable()
async def batch_workflow(item_ids: list[int]) -> Result[dict, str]:
    """Process all items in a batch."""
    # Fan-out: Process all items in parallel
    tasks = [process_item(item_id) for item_id in item_ids]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Aggregate results
    succeeded = []
    failed = []
    
    for item_id, result in zip(item_ids, results):
        if isinstance(result, Exception):
            failed.append({"id": item_id, "error": str(result)})
        else:
            succeeded.append(result)
    
    return Result.Ok({
        "total": len(item_ids),
        "succeeded": len(succeeded),
        "failed": len(failed),
        "results": succeeded,
        "errors": failed
    })
```

## Complete Example: Image Processing Pipeline

This example downloads images, processes them, and creates a gallery.

### Define Activities

```python
from stent import Stent, Result
import random
import logging

logger = logging.getLogger(__name__)

@Stent.durable()
async def download_image(image_id: int) -> str:
    """Download an image and return local path."""
    # Simulate network latency
    delay = random.uniform(0.1, 0.5)
    await asyncio.sleep(delay)
    
    # Simulate occasional failures
    if random.random() < 0.1:
        raise ConnectionError(f"Network error downloading image {image_id}")
    
    path = f"/tmp/img_{image_id}.jpg"
    logger.info(f"Downloaded image {image_id} to {path}")
    return path

@Stent.durable()
async def process_image(path: str) -> str:
    """Process an image (resize, convert, etc.)."""
    await asyncio.sleep(0.2)  # Simulate CPU work
    processed_path = path.replace(".jpg", "_processed.jpg")
    logger.info(f"Processed {path} -> {processed_path}")
    return processed_path

@Stent.durable()
async def create_gallery(image_paths: list[str]) -> str:
    """Create a gallery from processed images."""
    await asyncio.sleep(0.5)
    gallery_url = f"https://gallery.example.com/{len(image_paths)}_images"
    logger.info(f"Created gallery with {len(image_paths)} images")
    return gallery_url
```

### Define the Batch Orchestrator

```python
@Stent.durable()
async def image_batch_workflow(image_ids: list[int]) -> Result[str, str]:
    """
    Process a batch of images:
    1. Download all images in parallel
    2. Process downloaded images in parallel  
    3. Create gallery from results
    """
    logger.info(f"Starting batch workflow for {len(image_ids)} images")
    
    # Stage 1: Download all images
    download_tasks = [download_image(img_id) for img_id in image_ids]
    download_results = await asyncio.gather(*download_tasks, return_exceptions=True)
    
    # Filter successful downloads
    downloaded_paths = []
    download_failures = []
    
    for img_id, result in zip(image_ids, download_results):
        if isinstance(result, Exception):
            logger.warning(f"Failed to download image {img_id}: {result}")
            download_failures.append(img_id)
        else:
            downloaded_paths.append(result)
    
    if not downloaded_paths:
        return Result.Error("No images downloaded successfully")
    
    logger.info(f"Downloaded {len(downloaded_paths)}/{len(image_ids)} images")
    
    # Stage 2: Process downloaded images
    process_tasks = [process_image(path) for path in downloaded_paths]
    processed_paths = await asyncio.gather(*process_tasks)
    
    # Stage 3: Create gallery
    gallery_url = await create_gallery(processed_paths)
    
    return Result.Ok(gallery_url)
```

## Chunked Processing

For very large datasets, process in chunks to avoid memory issues and enable progress tracking:

```python
@Stent.durable()
async def chunked_batch_workflow(
    item_ids: list[int],
    chunk_size: int = 100
) -> Result[dict, str]:
    """Process items in chunks with progress tracking."""
    total_items = len(item_ids)
    processed = 0
    all_results = []
    all_errors = []
    
    # Process in chunks
    for i in range(0, total_items, chunk_size):
        chunk = item_ids[i:i + chunk_size]
        chunk_num = (i // chunk_size) + 1
        total_chunks = (total_items + chunk_size - 1) // chunk_size
        
        logger.info(f"Processing chunk {chunk_num}/{total_chunks}")
        
        # Process chunk
        tasks = [process_item(item_id) for item_id in chunk]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Collect results
        for item_id, result in zip(chunk, results):
            if isinstance(result, Exception):
                all_errors.append({"id": item_id, "error": str(result)})
            else:
                all_results.append(result)
        
        processed += len(chunk)
        logger.info(f"Progress: {processed}/{total_items} ({100*processed/total_items:.1f}%)")
        
        # Optional: Add delay between chunks to avoid overwhelming resources
        if i + chunk_size < total_items:
            await Stent.sleep("100ms")
    
    return Result.Ok({
        "total": total_items,
        "succeeded": len(all_results),
        "failed": len(all_errors),
        "results": all_results,
        "errors": all_errors
    })
```

## Using Stent.map for Efficiency

For large batches, `Stent.map` provides optimized batch scheduling:

```python
@Stent.durable()
async def efficient_batch_workflow(item_ids: list[int]) -> Result[dict, str]:
    """Use Stent.map for efficient batch processing."""
    
    # Stent.map batches database operations
    # More efficient than asyncio.gather for large batches
    results = await Stent.map(process_item, item_ids)
    
    return Result.Ok({
        "total": len(item_ids),
        "results": results
    })
```

### When to Use Each Approach

| Approach | Best For | Benefits |
|----------|----------|----------|
| `asyncio.gather` | Small batches (< 50 items) | Simple, standard Python |
| `Stent.map` | Large batches (50+ items) | Batched DB operations |
| Chunked processing | Very large batches (1000+ items) | Memory control, progress |

## Rate-Limited Batch Processing

When calling external APIs with rate limits:

```python
@Stent.durable(max_concurrent=5)
async def rate_limited_api_call(item_id: int) -> dict:
    """Max 5 concurrent calls to this API."""
    return await external_api.process(item_id)

@Stent.durable()
async def rate_limited_batch(item_ids: list[int]) -> Result[list, str]:
    """Process batch with rate limiting."""
    
    # Even though all tasks are scheduled at once,
    # only 5 will run concurrently due to max_concurrent
    tasks = [rate_limited_api_call(item_id) for item_id in item_ids]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    succeeded = [r for r in results if not isinstance(r, Exception)]
    return Result.Ok(succeeded)
```

### Manual Rate Limiting

For more control over rate limiting:

```python
@Stent.durable()
async def manually_rate_limited_batch(
    item_ids: list[int],
    batch_size: int = 10,
    delay_between_batches: str = "1s"
) -> Result[list, str]:
    """Process with explicit rate control."""
    all_results = []
    
    for i in range(0, len(item_ids), batch_size):
        batch = item_ids[i:i + batch_size]
        
        # Process batch
        tasks = [process_item(item_id) for item_id in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Collect non-error results
        all_results.extend(r for r in results if not isinstance(r, Exception))
        
        # Wait before next batch
        if i + batch_size < len(item_ids):
            await Stent.sleep(delay_between_batches)
    
    return Result.Ok(all_results)
```

## Handling Partial Failures

### Continue On Error

Process all items even if some fail:

```python
@Stent.durable()
async def resilient_batch(item_ids: list[int]) -> Result[dict, str]:
    """Process all items, collecting failures separately."""
    tasks = [process_item(item_id) for item_id in item_ids]
    
    # return_exceptions=True ensures all results are returned
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    succeeded = []
    failed = []
    
    for item_id, result in zip(item_ids, results):
        if isinstance(result, Exception):
            failed.append({
                "id": item_id,
                "error": str(result),
                "error_type": type(result).__name__
            })
        else:
            succeeded.append(result)
    
    # Decide success/failure based on threshold
    success_rate = len(succeeded) / len(item_ids)
    
    if success_rate < 0.5:
        return Result.Error(f"Too many failures: {len(failed)}/{len(item_ids)}")
    
    return Result.Ok({
        "succeeded": succeeded,
        "failed": failed,
        "success_rate": success_rate
    })
```

### Retry Failed Items

```python
@Stent.durable()
async def batch_with_retry(
    item_ids: list[int],
    max_retries: int = 2
) -> Result[dict, str]:
    """Retry failed items."""
    
    pending = list(item_ids)
    all_succeeded = []
    final_failures = []
    
    for attempt in range(max_retries + 1):
        if not pending:
            break
            
        if attempt > 0:
            logger.info(f"Retry attempt {attempt} for {len(pending)} items")
            await Stent.sleep("5s")  # Wait before retry
        
        tasks = [process_item(item_id) for item_id in pending]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        new_pending = []
        for item_id, result in zip(pending, results):
            if isinstance(result, Exception):
                new_pending.append(item_id)
            else:
                all_succeeded.append(result)
        
        pending = new_pending
    
    # Remaining items are final failures
    final_failures = [{"id": item_id, "error": "Max retries exceeded"} for item_id in pending]
    
    return Result.Ok({
        "succeeded": all_succeeded,
        "failed": final_failures
    })
```

## Progress Tracking and Monitoring

### With Structured Logging

```python
from stent import install_structured_logging

install_structured_logging(logging.getLogger())

@Stent.durable()
async def monitored_batch(item_ids: list[int]) -> Result[dict, str]:
    """Batch with detailed progress logging."""
    total = len(item_ids)
    
    logger.info(f"Starting batch processing", extra={
        "batch_size": total,
        "operation": "batch_start"
    })
    
    succeeded = 0
    failed = 0
    
    # Process in chunks for progress updates
    chunk_size = 50
    for i in range(0, total, chunk_size):
        chunk = item_ids[i:i + chunk_size]
        
        tasks = [process_item(item_id) for item_id in chunk]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        chunk_succeeded = sum(1 for r in results if not isinstance(r, Exception))
        chunk_failed = len(results) - chunk_succeeded
        
        succeeded += chunk_succeeded
        failed += chunk_failed
        
        progress = (i + len(chunk)) / total * 100
        
        logger.info(f"Batch progress", extra={
            "progress_pct": progress,
            "processed": i + len(chunk),
            "total": total,
            "succeeded": succeeded,
            "failed": failed
        })
    
    logger.info(f"Batch completed", extra={
        "operation": "batch_complete",
        "total": total,
        "succeeded": succeeded,
        "failed": failed,
        "success_rate": succeeded / total
    })
    
    return Result.Ok({
        "total": total,
        "succeeded": succeeded,
        "failed": failed
    })
```

### Checkpoint/Resume Pattern

For very long batches, save progress to enable resume:

```python
@Stent.durable()
async def resumable_batch(
    batch_id: str,
    item_ids: list[int]
) -> Result[dict, str]:
    """Batch that can resume from checkpoint."""
    
    # Load checkpoint if exists
    checkpoint = await load_checkpoint(batch_id)
    
    if checkpoint:
        processed_ids = set(checkpoint["processed_ids"])
        results = checkpoint["results"]
        errors = checkpoint["errors"]
        logger.info(f"Resuming from checkpoint: {len(processed_ids)} already processed")
    else:
        processed_ids = set()
        results = []
        errors = []
    
    # Filter to unprocessed items
    remaining = [id for id in item_ids if id not in processed_ids]
    
    # Process in chunks with checkpoints
    chunk_size = 100
    for i in range(0, len(remaining), chunk_size):
        chunk = remaining[i:i + chunk_size]
        
        tasks = [process_item(item_id) for item_id in chunk]
        chunk_results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for item_id, result in zip(chunk, chunk_results):
            processed_ids.add(item_id)
            if isinstance(result, Exception):
                errors.append({"id": item_id, "error": str(result)})
            else:
                results.append(result)
        
        # Save checkpoint after each chunk
        await save_checkpoint(batch_id, {
            "processed_ids": list(processed_ids),
            "results": results,
            "errors": errors
        })
        
        logger.info(f"Checkpoint saved: {len(processed_ids)}/{len(item_ids)} processed")
    
    # Cleanup checkpoint on completion
    await delete_checkpoint(batch_id)
    
    return Result.Ok({
        "total": len(item_ids),
        "succeeded": len(results),
        "failed": len(errors),
        "results": results,
        "errors": errors
    })
```

## Multi-Stage Pipelines

Process data through multiple transformation stages:

```python
@Stent.durable()
async def extract_data(source_id: str) -> dict:
    """Extract raw data from source."""
    return await data_extractor.extract(source_id)

@Stent.durable()
async def transform_data(raw_data: dict) -> dict:
    """Transform raw data."""
    return await data_transformer.transform(raw_data)

@Stent.durable()
async def load_data(transformed_data: dict) -> str:
    """Load data to destination."""
    return await data_loader.load(transformed_data)

@Stent.durable()
async def etl_pipeline(source_ids: list[str]) -> Result[dict, str]:
    """
    ETL pipeline:
    Extract -> Transform -> Load
    """
    # Stage 1: Extract (parallel)
    logger.info(f"Stage 1: Extracting {len(source_ids)} sources")
    extract_tasks = [extract_data(sid) for sid in source_ids]
    extracted = await asyncio.gather(*extract_tasks, return_exceptions=True)
    
    # Filter successful extractions
    valid_extracts = [
        (sid, data) for sid, data in zip(source_ids, extracted)
        if not isinstance(data, Exception)
    ]
    
    if not valid_extracts:
        return Result.Error("All extractions failed")
    
    logger.info(f"Extracted {len(valid_extracts)}/{len(source_ids)} sources")
    
    # Stage 2: Transform (parallel)
    logger.info("Stage 2: Transforming data")
    transform_tasks = [transform_data(data) for _, data in valid_extracts]
    transformed = await asyncio.gather(*transform_tasks, return_exceptions=True)
    
    # Filter successful transforms
    valid_transforms = [
        (sid, data) for (sid, _), data in zip(valid_extracts, transformed)
        if not isinstance(data, Exception)
    ]
    
    logger.info(f"Transformed {len(valid_transforms)}/{len(valid_extracts)} records")
    
    # Stage 3: Load (parallel)
    logger.info("Stage 3: Loading data")
    load_tasks = [load_data(data) for _, data in valid_transforms]
    loaded = await asyncio.gather(*load_tasks, return_exceptions=True)
    
    # Count successes
    load_successes = sum(1 for r in loaded if not isinstance(r, Exception))
    
    logger.info(f"Loaded {load_successes}/{len(valid_transforms)} records")
    
    return Result.Ok({
        "extracted": len(valid_extracts),
        "transformed": len(valid_transforms),
        "loaded": load_successes,
        "original_count": len(source_ids)
    })
```

## Best Practices

### 1. Choose Appropriate Chunk Sizes

```python
# Too small: Excessive overhead
chunk_size = 1  # Bad

# Too large: Memory issues, no progress visibility  
chunk_size = 100000  # Bad for very large items

# Good: Balance between efficiency and visibility
chunk_size = 100  # Good for most cases
```

### 2. Always Use return_exceptions=True

```python
# Without: First failure stops everything
results = await asyncio.gather(*tasks)  # Risky

# With: All results captured
results = await asyncio.gather(*tasks, return_exceptions=True)  # Safe
```

### 3. Set Timeouts for Long Batches

```python
exec_id = await executor.dispatch(
    large_batch_workflow,
    item_ids,
    max_duration="2h"  # 2 hour timeout
)
```

### 4. Monitor Worker Concurrency

Ensure workers can handle your batch parallelism:

```python
# If processing 1000 items with 10 workers at concurrency 10,
# max parallel = 10 * 10 = 100 tasks
await executor.serve(max_concurrency=10)
```

### 5. Implement Idempotency

For resumable batches:

```python
@Stent.durable(idempotent=True)
async def idempotent_process(item_id: int) -> dict:
    """Safe to retry - won't duplicate work."""
    # Check if already processed
    existing = await get_processed_result(item_id)
    if existing:
        return existing
    
    # Process and store
    result = await do_processing(item_id)
    await store_result(item_id, result)
    return result
```

### 6. Clean Up Resources

```python
@Stent.durable()
async def batch_with_cleanup(item_ids: list[int]) -> Result[dict, str]:
    """Clean up temporary resources after batch."""
    temp_files = []
    
    try:
        # Process and track temp files
        for item_id in item_ids:
            temp_path = f"/tmp/processing_{item_id}"
            temp_files.append(temp_path)
            await process_to_file(item_id, temp_path)
        
        # Aggregate results
        result = await aggregate_files(temp_files)
        return Result.Ok(result)
        
    finally:
        # Always cleanup
        for path in temp_files:
            try:
                await delete_file(path)
            except Exception:
                pass
```

## See Also

- [Parallel Execution](../guides/parallel-execution.md) - Concurrency patterns
- [Saga Pattern](saga.md) - Distributed transactions
- [Error Handling](../guides/error-handling.md) - Retry and failure handling
- [Workers](../guides/workers.md) - Scaling worker concurrency
