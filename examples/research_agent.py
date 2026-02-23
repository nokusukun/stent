"""
Research LLM Agent Example

This example demonstrates a durable research agent that:
1. Takes a research question
2. Generates search queries using an LLM
3. Fetches and scrapes multiple web sources in parallel
4. Summarizes each source with an LLM
5. Synthesizes a final research report
6. Handles failures gracefully with retries and checkpointing

The workflow survives crashes at any point and resumes from the last checkpoint.
"""

import asyncio
import json
import hashlib
import logging
from datetime import datetime
from typing import Any

from stent import Stent, Result, RetryPolicy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


# =============================================================================
# Mock LLM Client (replace with real OpenAI/Anthropic/etc.)
# =============================================================================

class MockLLMClient:
    """
    Mock LLM client for demonstration.
    Replace with actual API calls to OpenAI, Anthropic, etc.
    """
    
    async def generate(self, prompt: str, system: str | None = None) -> str:
        # Simulate API latency
        await asyncio.sleep(0.5)
        
        # Mock responses based on prompt content
        if "search queries" in prompt.lower():
            return json.dumps({
                "queries": [
                    {"query": "topic overview research papers", "intent": "background"},
                    {"query": "topic recent news 2024", "intent": "recent_developments"},
                    {"query": "topic expert analysis opinions", "intent": "expert_opinions"},
                ]
            })
        elif "summarize" in prompt.lower():
            return json.dumps({
                "summary": "This source discusses key aspects of the topic...",
                "key_points": [
                    "First important finding",
                    "Second important finding", 
                    "Third important finding"
                ],
                "relevance_score": 0.85
            })
        elif "synthesize" in prompt.lower() or "research report" in prompt.lower():
            return json.dumps({
                "summary": "Based on the analyzed sources, the research indicates...",
                "sections": [
                    {"title": "Background", "content": "The topic has evolved significantly..."},
                    {"title": "Recent Developments", "content": "In 2024, several breakthroughs..."},
                    {"title": "Expert Consensus", "content": "Leading researchers agree that..."},
                    {"title": "Conclusions", "content": "The evidence suggests..."}
                ]
            })
        else:
            return "Generic LLM response"


# Global LLM client (in production, configure with API keys)
llm_client = MockLLMClient()


# =============================================================================
# Mock Web Services (replace with real implementations)
# =============================================================================

async def mock_web_search(query: str) -> list[dict]:
    """Mock web search - replace with real search API (Google, Bing, Serper, etc.)"""
    await asyncio.sleep(0.3)
    
    # Generate deterministic mock results based on query
    query_hash = hashlib.md5(query.encode()).hexdigest()[:8]
    
    return [
        {
            "url": f"https://example.com/article-{query_hash}-1",
            "title": f"Article about {query[:30]}...",
            "snippet": "This article explores the key aspects..."
        },
        {
            "url": f"https://example.com/article-{query_hash}-2", 
            "title": f"Research findings on {query[:30]}...",
            "snippet": "Recent research has shown..."
        },
        {
            "url": f"https://example.com/article-{query_hash}-3",
            "title": f"Expert analysis: {query[:30]}...",
            "snippet": "Experts in the field suggest..."
        }
    ]


async def mock_fetch_page(url: str) -> str:
    """Mock page fetcher - replace with real HTTP client + scraping"""
    await asyncio.sleep(0.2)
    
    # Simulate occasional failures
    if hash(url) % 10 == 0:
        raise ConnectionError(f"Failed to fetch {url}")
    
    return f"""
    <article>
        <h1>Article Title for {url}</h1>
        <p>This is the main content of the article. It contains important information
        about the research topic. The article discusses various aspects including
        methodology, findings, and implications.</p>
        <p>Additional paragraphs with more detailed information follow here.
        The research methodology involved extensive data collection and analysis.
        Results indicate significant findings that contribute to the field.</p>
    </article>
    """


# =============================================================================
# Durable Functions - Activities (Leaf Operations)
# =============================================================================

@Stent.durable(
    retry_policy=RetryPolicy(
        max_attempts=3,
        initial_delay=1.0,
        backoff_factor=2.0,
        retry_for=(ConnectionError, TimeoutError, json.JSONDecodeError)
    ),
    cached=True,  # Cache LLM responses for identical prompts
    max_concurrent=5,  # Rate limit LLM API calls
)
async def generate_search_queries(question: str) -> list[dict]:
    """Use LLM to generate diverse search queries for research question."""
    logger.info(f"Generating search queries for: {question[:50]}...")
    
    prompt = f"""Given the research question: "{question}"
    
Generate 3-5 diverse search queries to find relevant information.
Each query should have a different intent (background, recent_developments, expert_opinions, data_statistics, etc.)

Return JSON format:
{{"queries": [{{"query": "...", "intent": "..."}}]}}
"""
    
    response = await llm_client.generate(prompt)
    data = json.loads(response)
    
    queries = [
        {"query": q["query"], "intent": q["intent"]}
        for q in data["queries"]
    ]
    
    logger.info(f"Generated {len(queries)} search queries")
    return queries


@Stent.durable(
    retry_policy=RetryPolicy(
        max_attempts=3,
        initial_delay=0.5,
        retry_for=(ConnectionError, TimeoutError)
    ),
    cached=True,
    max_concurrent=10,  # Allow parallel searches
)
async def web_search(query: str) -> list[dict]:
    """Search the web for relevant sources."""
    logger.info(f"Searching: {query[:50]}...")
    results = await mock_web_search(query)
    logger.info(f"Found {len(results)} results for query")
    return results


@Stent.durable(
    retry_policy=RetryPolicy(
        max_attempts=3,
        initial_delay=1.0,
        backoff_factor=2.0,
        retry_for=(ConnectionError, TimeoutError)
    ),
    cached=True,
    max_concurrent=20,  # Allow many parallel fetches
)
async def fetch_webpage(url: str) -> dict:
    """Fetch and extract content from a webpage."""
    logger.info(f"Fetching: {url}")
    
    html_content = await mock_fetch_page(url)
    
    # In production: use BeautifulSoup, trafilatura, or similar
    # to extract clean text from HTML
    content = html_content.strip()
    
    return {
        "url": url,
        "title": f"Title from {url}",
        "content": content[:5000],  # Truncate for LLM context
        "fetched_at": datetime.now().isoformat()
    }


@Stent.durable(
    retry_policy=RetryPolicy(
        max_attempts=3,
        initial_delay=1.0,
        retry_for=(ConnectionError, TimeoutError, json.JSONDecodeError)
    ),
    cached=True,
    max_concurrent=5,  # Rate limit LLM calls
)
async def summarize_source(source: dict, question: str) -> dict:
    """Use LLM to summarize a source in context of the research question."""
    logger.info(f"Summarizing: {source['url']}")
    
    prompt = f"""Research Question: {question}

Source URL: {source['url']}
Source Title: {source['title']}
Source Content:
{source['content'][:3000]}

Please summarize this source in the context of the research question.
Return JSON format:
{{
    "summary": "2-3 sentence summary",
    "key_points": ["point 1", "point 2", "point 3"],
    "relevance_score": 0.0-1.0
}}
"""
    
    response = await llm_client.generate(prompt)
    data = json.loads(response)
    
    return {
        "url": source['url'],
        "title": source['title'],
        "summary": data["summary"],
        "key_points": data["key_points"],
        "relevance_score": data["relevance_score"]
    }


@Stent.durable(
    retry_policy=RetryPolicy(
        max_attempts=3,
        initial_delay=2.0,
        retry_for=(ConnectionError, TimeoutError, json.JSONDecodeError)
    ),
    max_concurrent=2,  # Synthesis is expensive, limit concurrency
)
async def synthesize_report(
    question: str,
    summaries: list[dict]
) -> dict:
    """Use LLM to synthesize final research report from all summaries."""
    logger.info(f"Synthesizing report from {len(summaries)} sources")
    
    # Build context from summaries
    summaries_text = "\n\n".join([
        f"Source: {s['url']}\nSummary: {s['summary']}\nKey Points: {', '.join(s['key_points'])}"
        for s in summaries
    ])
    
    prompt = f"""Research Question: {question}

Source Summaries:
{summaries_text}

Please synthesize a comprehensive research report that:
1. Answers the research question
2. Organizes findings into logical sections
3. Cites sources appropriately
4. Identifies areas of consensus and disagreement

Return JSON format:
{{
    "summary": "Executive summary paragraph",
    "sections": [
        {{"title": "Section Title", "content": "Section content..."}}
    ]
}}
"""
    
    response = await llm_client.generate(prompt)
    data = json.loads(response)
    
    return {
        "question": question,
        "summary": data["summary"],
        "sections": data["sections"],
        "sources": [s['url'] for s in summaries],
        "generated_at": datetime.now().isoformat()
    }


# =============================================================================
# Durable Functions - Sub-Orchestrators
# =============================================================================

@Stent.durable()
async def search_and_fetch_sources(query: dict) -> list[dict]:
    """Search for a query and fetch all resulting pages."""
    # Search
    search_results = await web_search(query["query"])
    
    # Fetch all pages in parallel
    fetch_tasks = [
        fetch_webpage(result["url"]) 
        for result in search_results
    ]
    
    # Use return_exceptions to handle partial failures
    results = await asyncio.gather(*fetch_tasks, return_exceptions=True)
    
    # Filter out failures
    sources = []
    for result in results:
        if isinstance(result, Exception):
            logger.warning(f"Failed to fetch source: {result}")
        else:
            sources.append(result)
    
    logger.info(f"Fetched {len(sources)}/{len(search_results)} sources for '{query['query'][:30]}...'")
    return sources


@Stent.durable()
async def process_sources_batch(
    sources: list[dict],
    question: str
) -> list[dict]:
    """Summarize a batch of sources in parallel."""
    # Summarize all sources in parallel
    summary_tasks = [
        summarize_source(source, question)
        for source in sources
    ]
    
    results = await asyncio.gather(*summary_tasks, return_exceptions=True)
    
    # Filter out failures
    summaries: list[dict] = []
    for result in results:
        if isinstance(result, BaseException):
            logger.warning(f"Failed to summarize source: {result}")
        else:
            summaries.append(result)
    
    return summaries


# =============================================================================
# Main Orchestrator - Research Agent Workflow
# =============================================================================

@Stent.durable()
async def research_agent(question: str) -> Result[dict, str]:
    """
    Main research agent workflow.
    
    This orchestrator:
    1. Generates search queries from the research question
    2. Searches and fetches sources for each query in parallel
    3. Summarizes all sources with an LLM
    4. Synthesizes a final research report
    
    The entire workflow is durable - if it crashes at any point,
    it will resume from the last completed step.
    """
    logger.info(f"Starting research for: {question}")
    start_time = datetime.now()
    
    try:
        # Step 1: Generate search queries
        logger.info("Step 1: Generating search queries...")
        search_queries = await generate_search_queries(question)
        
        if not search_queries:
            return Result.Error("Failed to generate search queries")
        
        # Step 2: Search and fetch sources for each query in parallel
        logger.info(f"Step 2: Searching {len(search_queries)} queries in parallel...")
        search_tasks = [
            search_and_fetch_sources(query)
            for query in search_queries
        ]
        
        all_source_lists = await asyncio.gather(*search_tasks)
        
        # Flatten and deduplicate sources
        seen_urls: set[str] = set()
        all_sources: list[dict] = []
        for source_list in all_source_lists:
            for source in source_list:
                if source["url"] not in seen_urls:
                    seen_urls.add(source["url"])
                    all_sources.append(source)
        
        logger.info(f"Collected {len(all_sources)} unique sources")
        
        if not all_sources:
            return Result.Error("No sources found for research question")
        
        # Step 3: Summarize all sources
        logger.info(f"Step 3: Summarizing {len(all_sources)} sources...")
        summaries = await process_sources_batch(all_sources, question)
        
        if not summaries:
            return Result.Error("Failed to summarize any sources")
        
        # Filter by relevance score
        relevant_summaries = [s for s in summaries if s["relevance_score"] >= 0.5]
        logger.info(f"Found {len(relevant_summaries)} relevant sources (score >= 0.5)")
        
        if not relevant_summaries:
            # Fall back to all summaries if none pass threshold
            relevant_summaries = summaries
        
        # Step 4: Synthesize final report
        logger.info("Step 4: Synthesizing final report...")
        report = await synthesize_report(question, relevant_summaries)
        
        elapsed = (datetime.now() - start_time).total_seconds()
        logger.info(f"Research completed in {elapsed:.1f}s")
        
        return Result.Ok(report)
        
    except Exception as e:
        logger.error(f"Research failed: {e}")
        return Result.Error(f"Research failed: {str(e)}")


# =============================================================================
# Advanced: Multi-Agent Research with Critic
# =============================================================================

@Stent.durable(
    retry_policy=RetryPolicy(max_attempts=2),
    max_concurrent=2,
)
async def critic_agent(report: dict, question: str) -> dict:
    """
    Critic agent that evaluates and suggests improvements to a research report.
    """
    logger.info("Critic agent evaluating report...")
    
    prompt = f"""Research Question: {question}

Research Report Summary: {report['summary']}

Sections:
{json.dumps(report['sections'], indent=2)}

Sources Used: {len(report['sources'])}

Please evaluate this research report and provide:
1. Overall quality score (0-10)
2. Strengths
3. Weaknesses
4. Suggested improvements
5. Missing perspectives or sources

Return JSON format:
{{
    "quality_score": 8,
    "strengths": ["...", "..."],
    "weaknesses": ["...", "..."],
    "improvements": ["...", "..."],
    "missing_perspectives": ["...", "..."]
}}
"""
    
    await llm_client.generate(prompt)
    
    # Mock response for demo
    return {
        "quality_score": 7.5,
        "strengths": [
            "Comprehensive coverage of main topics",
            "Good use of multiple sources"
        ],
        "weaknesses": [
            "Could use more recent data",
            "Missing some expert perspectives"
        ],
        "improvements": [
            "Add statistical data to support claims",
            "Include counterarguments"
        ],
        "missing_perspectives": [
            "Industry practitioner viewpoints",
            "International perspectives"
        ]
    }


@Stent.durable()
async def research_with_critique(question: str) -> Result[dict, str]:
    """
    Enhanced research workflow with critic agent feedback loop.
    
    1. Run initial research
    2. Have critic agent evaluate
    3. If quality score < 7, run additional targeted research
    4. Return final report with critique
    """
    logger.info(f"Starting research with critique for: {question}")
    
    # Initial research
    initial_result = await research_agent(question)
    
    if not initial_result.ok or initial_result.value is None:
        return Result.Error(f"Initial research failed: {initial_result.error}")
    
    report = initial_result.value
    
    # Critique the report
    critique = await critic_agent(report, question)
    
    # If quality is low, do additional research
    if critique["quality_score"] < 7:
        logger.info("Quality score below threshold, doing additional research...")
        
        # Generate additional queries based on missing perspectives
        additional_queries = [
            {"query": f"{question} {perspective}", "intent": "additional"}
            for perspective in critique["missing_perspectives"][:2]
        ]
        
        # Fetch additional sources
        additional_tasks = [
            search_and_fetch_sources(q) for q in additional_queries
        ]
        additional_sources_lists = await asyncio.gather(*additional_tasks)
        
        additional_sources: list[dict] = []
        for sources in additional_sources_lists:
            additional_sources.extend(sources)
        
        if additional_sources:
            # Summarize additional sources
            additional_summaries = await process_sources_batch(
                additional_sources, question
            )
            
            # Re-synthesize with additional sources
            if additional_summaries:
                report = await synthesize_report(question, additional_summaries)
    
    return Result.Ok({
        "report": report,
        "critique": critique
    })


# =============================================================================
# Run the Example
# =============================================================================

async def main():
    # Setup
    backend = Stent.backends.SQLiteBackend("research_agent.db")
    await backend.init_db()
    
    executor = Stent(backend=backend)
    
    # Start worker in background
    worker = asyncio.create_task(executor.serve(max_concurrency=20))
    
    # Research question
    question = """
    What are the latest developments in AI agents for software engineering, 
    and how are they being used to improve developer productivity?
    """
    
    print("=" * 60)
    print("RESEARCH AGENT DEMO")
    print("=" * 60)
    print(f"\nResearch Question:\n{question.strip()}\n")
    print("=" * 60)
    
    # Option 1: Simple research
    print("\n[Starting Simple Research Agent...]\n")
    
    exec_id = await executor.dispatch(research_agent, question.strip())
    print(f"Execution ID: {exec_id}\n")
    
    # Wait for result
    result = await executor.wait_for(exec_id, expiry=300)
    
    if result is not None and result.ok and result.value is not None:
        report = result.value
        print("\n" + "=" * 60)
        print("RESEARCH REPORT")
        print("=" * 60)
        print(f"\nQuestion: {report['question'][:100]}...")
        print(f"\nSummary:\n{report['summary']}")
        print(f"\nSections:")
        for section in report['sections']:
            print(f"\n  ## {section['title']}")
            print(f"  {section['content'][:200]}...")
        print(f"\nSources ({len(report['sources'])}):")
        for url in report['sources'][:5]:
            print(f"  - {url}")
        if len(report['sources']) > 5:
            print(f"  ... and {len(report['sources']) - 5} more")
    elif result is not None:
        print(f"\nResearch failed: {result.error}")
    else:
        print("\nResearch timed out or returned no result")
    
    # Cleanup
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass
    
    print("\n" + "=" * 60)
    print("Done!")


if __name__ == "__main__":
    asyncio.run(main())
