from typing import Any
import asyncio
import random
import logging
from stent import Stent

# Configure logging to see what's happening
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# 1. Define Activities
@Stent.durable()
async def fetch_url(url: str) -> str:
    """Simulates fetching a webpage with variable latency."""
    # Simulate network latency (0.5 to 2.0 seconds)
    # We use Stent.sleep to be "good citizens" and release the worker
    delay = random.uniform(0.5, 2.0)
    await Stent.sleep(f"{delay:.2f}s")
    
    if "error" in url:
        raise ValueError(f"404 Not Found: {url}")
        
    return f"<html><body><h1>Content of {url}</h1><p>Some interesting text here.</p></body></html>"

@Stent.durable()
async def analyze_sentiment(content: str) -> dict:
    """Simulates CPU-intensive analysis."""
    print(f"[Analysis] Analyzing sentiment: {content}...")
    await asyncio.sleep(0.1) # Simulate CPU work
    return {
        "score": random.randint(0, 100),
        "keywords": ["stent", "python", "durable"]
    }

# 2. Define Sub-Orchestrator
@Stent.durable()
async def process_page(url: str) -> dict:
    """
    Orchestrates the processing of a single page.
    This is a sub-workflow that will be run in parallel for multiple URLs.
    """
    print(f"[Page] Processing {url}...")
    
    # Call activity 1
    content = await fetch_url(url)
    
    # Call activity 2
    analysis = await analyze_sentiment(content)
    
    return {
        "url": url,
        "content_length": len(content),
        "analysis": analysis
    }

# 3. Define Main Orchestrator
@Stent.durable()
async def site_crawler(urls: list[str]) -> dict:
    """
    Main workflow that fans out to process multiple pages and fans in to aggregate results.
    """
    print(f"[Crawler] Starting crawl for {len(urls)} URLs")
    
    # Fan-out: Dispatch a sub-orchestrator for each URL.
    # We create the tasks but await them all at once using gather.
    tasks = [process_page(url) for url in urls]
    
    # Fan-in: Wait for all to complete.
    # return_exceptions=True allows the workflow to continue even if some pages fail.
    results: list[Any | BaseException] = await Stent.gather(*tasks, return_exceptions=True)
    
    # Aggregation Logic
    report: dict[str, Any] = {
        "total_urls": len(urls),
        "successful": 0,
        "failed": 0,
        "average_score": 0.0,
        "failures": [],
        "processed_data": []
    }
    
    total_score = 0
    
    for url, res in zip(urls, results):
        if isinstance(res, Exception) or isinstance(res, BaseException):
            report["failed"] += 1
            report["failures"].append({"url": url, "error": str(res)})
        else:
            report["successful"] += 1
            report["processed_data"].append(res)
            total_score += res["analysis"]["score"]
            
    if report["successful"] > 0:
        report["average_score"] = total_score / report["successful"]
        
    return report

# 4. Run the example
async def main():
    # Setup backend and executor
    backend = Stent.backends.SQLiteBackend("complex_gather.sqlite")
    await backend.init_db()
    executor = Stent(backend=backend)
    
    # Start worker in background (high concurrency for parallel tasks)
    worker_task = asyncio.create_task(executor.serve(max_concurrency=20))
    
    try:
        urls = [
            "https://example.com/page1",
            "https://example.com/page2",
            "https://example.com/error_page", # This will fail
            "https://example.com/page3",
            "https://example.com/page4",
        ]
        
        print("Dispatching workflow...")
        exec_id = await executor.dispatch(site_crawler, urls)
        print(f"Workflow dispatched: {exec_id}")
        
        # Poll for completion
        result = await executor.wait_for(exec_id, expiry=30.0)
        
        if result.ok:
            import json
            print("\nWorkflow Completed Successfully!")
            print(json.dumps(result.value, indent=2))
        else:
            print(f"\nWorkflow Failed: {result.error}")
            
    finally:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
        # Cleanup
        import os
        if os.path.exists("complex_gather.sqlite"):
            os.remove("complex_gather.sqlite")

if __name__ == "__main__":
    asyncio.run(main())
