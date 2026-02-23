# Parallel Execution

This guide covers how to execute tasks concurrently using Stent's parallel execution features.

## Overview

Stent supports two main approaches for parallel execution:

1. **`asyncio.gather`** - Standard Python approach, schedules immediately
2. **`Stent.map`** - Optimized batch scheduling for large workloads

Both approaches release the orchestrator's worker slot while waiting, allowing other tasks to be processed.

## Using asyncio.gather

The simplest way to run tasks in parallel:

```python
import asyncio
from stent import Stent

@Stent.durable
async def process_item(item_id: str) -> dict:
    # Process a single item
    await asyncio.sleep(1)  # Simulate work
    return {"id": item_id, "processed": True}

@Stent.durable
async def parallel_workflow(item_ids: list[str]) -> list[dict]:
    # Schedule all tasks and wait for all to complete
    tasks = [process_item(item_id) for item_id in item_ids]
    results = await asyncio.gather(*tasks)
    return results
```

### How It Works

1. Each `process_item(item_id)` call creates a task immediately
2. Tasks are persisted to the database
3. `asyncio.gather` waits for all tasks to complete
4. Workers pick up and process tasks in parallel
5. Results are collected when all complete

### Handling Failures

By default, `asyncio.gather` raises the first exception:

```python
@Stent.durable
async def workflow_fail_fast(items: list[str]) -> list[dict]:
    tasks = [process_item(item) for item in items]
    
    try:
        results = await asyncio.gather(*tasks)
        return results
    except Exception as e:
        # One task failed - other tasks may still be running
        return []
```

Use `return_exceptions=True` to capture all results:

```python
@Stent.durable
async def workflow_partial_success(items: list[str]) -> dict:
    tasks = [process_item(item) for item in items]
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    successes = []
    failures = []
    
    for item, result in zip(items, results):
        if isinstance(result, Exception):
            failures.append({"item": item, "error": str(result)})
        else:
            successes.append(result)
    
    return {
        "succeeded": len(successes),
        "failed": len(failures),
        "results": successes,
        "errors": failures
    }
```

## Using Stent.map

For large batches, `Stent.map` is more efficient:

```python
@Stent.durable
async def batch_workflow(item_ids: list[str]) -> list[dict]:
    # Batch creates all tasks in fewer database operations
    results = await Stent.map(process_item, item_ids)
    return results
```

### Benefits Over asyncio.gather

| Aspect | asyncio.gather | Stent.map |
|--------|---------------|-------------|
| Task creation | One DB write per task | Batched writes |
| Progress tracking | Individual | Batched |
| Cache/idempotency | Checked per item | Batch checked |
| Best for | Small batches | Large batches |

### Example: Processing 1000 Items

```python
@Stent.durable
async def large_batch_workflow(item_ids: list[str]) -> dict:
    # With 1000 items, Stent.map is much faster
    # - Creates all tasks in batched DB operations
    # - Efficiently checks cache/idempotency
    
    results = await Stent.map(process_item, item_ids)
    
    return {
        "processed": len(results),
        "sample": results[:5]
    }
```

## Fan-Out/Fan-In Pattern

A common pattern for parallel processing:

```python
@Stent.durable
async def download_image(url: str) -> str:
    # Download and return local path
    path = f"/tmp/{hash(url)}.jpg"
    await http_client.download(url, path)
    return path

@Stent.durable
async def process_image(path: str) -> dict:
    # Process image and return metadata
    return await image_processor.analyze(path)

@Stent.durable
async def aggregate_results(results: list[dict]) -> dict:
    # Combine all results
    return {
        "total": len(results),
        "categories": list(set(r["category"] for r in results))
    }

@Stent.durable
async def image_pipeline(urls: list[str]) -> dict:
    # Fan-out: Download all images in parallel
    download_tasks = [download_image(url) for url in urls]
    paths = await asyncio.gather(*download_tasks)
    
    # Fan-out: Process all images in parallel
    process_tasks = [process_image(path) for path in paths]
    results = await asyncio.gather(*process_tasks)
    
    # Fan-in: Aggregate results
    summary = await aggregate_results(results)
    
    return summary
```

## Controlling Parallelism

### At the Function Level

Use `max_concurrent` to limit parallel executions:

```python
@Stent.durable(max_concurrent=5)
async def rate_limited_api_call(data: dict) -> dict:
    # Max 5 concurrent calls across ALL workers
    return await external_api.call(data)

@Stent.durable
async def workflow(items: list[dict]) -> list[dict]:
    # Even though we schedule all at once, only 5 run at a time
    tasks = [rate_limited_api_call(item) for item in items]
    return await asyncio.gather(*tasks)
```

### At the Worker Level

Limit worker concurrency:

```python
# Worker processes max 10 tasks at a time
await executor.serve(max_concurrency=10)
```

### Manual Batching

For fine-grained control:

```python
@Stent.durable
async def controlled_parallel(items: list[dict], batch_size: int = 10) -> list[dict]:
    all_results = []
    
    # Process in batches
    for i in range(0, len(items), batch_size):
        batch = items[i:i + batch_size]
        tasks = [process_item(item) for item in batch]
        results = await asyncio.gather(*tasks)
        all_results.extend(results)
        
        # Optional: Add delay between batches
        if i + batch_size < len(items):
            await Stent.sleep("1s")
    
    return all_results
```

## Parallel Sub-Workflows

Orchestrators can call other orchestrators in parallel:

```python
@Stent.durable
async def process_region(region: str, data: dict) -> dict:
    # This is itself an orchestrator with multiple steps
    validated = await validate_for_region(region, data)
    processed = await process_in_region(region, validated)
    return await finalize_region(region, processed)

@Stent.durable
async def multi_region_workflow(data: dict) -> dict:
    regions = ["us-east", "us-west", "eu-west", "ap-south"]
    
    # Run sub-workflows in parallel
    tasks = [process_region(region, data) for region in regions]
    results = await asyncio.gather(*tasks)
    
    # Combine results from all regions
    return {
        "regions": dict(zip(regions, results)),
        "total_processed": sum(r["count"] for r in results)
    }
```

## Mixed Sequential and Parallel

Combine patterns as needed:

```python
@Stent.durable
async def complex_workflow(orders: list[dict]) -> dict:
    # Step 1: Validate all orders in parallel
    validation_tasks = [validate_order(order) for order in orders]
    validated = await asyncio.gather(*validation_tasks, return_exceptions=True)
    
    # Filter out failed validations
    valid_orders = [
        order for order, result in zip(orders, validated)
        if not isinstance(result, Exception)
    ]
    
    # Step 2: Process valid orders sequentially (for consistency)
    results = []
    for order in valid_orders:
        # Must be sequential due to inventory constraints
        result = await process_order(order)
        results.append(result)
    
    # Step 3: Send notifications in parallel
    notification_tasks = [
        send_notification(result["customer_id"], result["order_id"])
        for result in results
    ]
    await asyncio.gather(*notification_tasks)
    
    return {
        "processed": len(results),
        "failed_validation": len(orders) - len(valid_orders)
    }
```

## Error Handling in Parallel

### Fail Fast (Default)

```python
@Stent.durable
async def fail_fast_workflow(items: list) -> list:
    tasks = [process_item(item) for item in items]
    
    # First exception cancels gather and raises
    results = await asyncio.gather(*tasks)
    return results
```

### Collect All Results

```python
@Stent.durable
async def collect_all_workflow(items: list) -> dict:
    tasks = [process_item(item) for item in items]
    
    # Collects all results, including exceptions
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    succeeded = [r for r in results if not isinstance(r, Exception)]
    failed = [r for r in results if isinstance(r, Exception)]
    
    return {
        "succeeded": succeeded,
        "failed": [str(e) for e in failed]
    }
```

### Retry Failed Items

```python
@Stent.durable
async def retry_failed_workflow(items: list) -> dict:
    results = await asyncio.gather(
        *[process_item(item) for item in items],
        return_exceptions=True
    )
    
    # Collect failures
    failed_items = [
        item for item, result in zip(items, results)
        if isinstance(result, Exception)
    ]
    
    if failed_items:
        # Wait and retry failed items
        await Stent.sleep("5s")
        retry_results = await asyncio.gather(
            *[process_item(item) for item in failed_items],
            return_exceptions=True
        )
        
        # Update results
        retry_idx = 0
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                results[i] = retry_results[retry_idx]
                retry_idx += 1
    
    return {
        "results": [r for r in results if not isinstance(r, Exception)],
        "still_failed": [str(r) for r in results if isinstance(r, Exception)]
    }
```

## Performance Considerations

### 1. Database Load

Each parallel task creates database operations:
- Task creation
- Task claiming
- Result storage
- Progress updates

For very large batches (1000+ items), consider:
- Using `Stent.map` for batch operations
- Processing in smaller batches
- Adding delays between batches

### 2. Worker Saturation

If you schedule 1000 tasks but only have 10 worker slots:
- Tasks queue up in "pending" state
- Workers process 10 at a time
- Total time = (1000 / 10) * average_task_time

### 3. Memory Usage

With `asyncio.gather`, all results are held in memory:

```python
# Potentially high memory for large batches
results = await asyncio.gather(*[big_task(i) for i in range(10000)])
```

Consider streaming results for very large batches:

```python
@Stent.durable
async def streaming_workflow(items: list) -> int:
    processed = 0
    
    for batch in chunks(items, 100):
        results = await asyncio.gather(*[process_item(i) for i in batch])
        
        # Process results immediately, don't accumulate
        await store_results(results)
        processed += len(results)
    
    return processed
```

## Best Practices

1. **Choose the right tool**:
   - Small batches (< 50): `asyncio.gather`
   - Large batches (50+): `Stent.map`

2. **Use `return_exceptions=True`** for robustness

3. **Set `max_concurrent`** on rate-limited functions

4. **Monitor worker concurrency** - ensure you have enough workers

5. **Consider memory** for very large batches

6. **Test error scenarios** - ensure partial failures are handled
