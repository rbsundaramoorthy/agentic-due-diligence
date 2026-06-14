"""
Web search tool for agent use.

Two options are provided:
  1. Brave Search API (recommended) — free tier gives 2,000 queries/month
     Sign up at https://brave.com/search/api/ and set BRAVE_API_KEY env var
  2. Fallback mock — returns a placeholder so you can test the agent loop
     without any API key

To switch between them, set USE_MOCK_SEARCH=true in your environment
for local testing without an API key.
"""

import os
import json
from typing import List, Dict

import httpx


USE_MOCK = os.getenv("USE_MOCK_SEARCH", "false").lower() == "true"
BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "")


async def web_search(query: str, max_results: int = 5) -> str:
    """Search the web and return results as a JSON string.

    Returns a list of {title, url, snippet} objects.
    """
    if USE_MOCK or not BRAVE_API_KEY:
        return _mock_search(query, max_results)

    return await _brave_search(query, max_results)


async def _brave_search(query: str, max_results: int = 5) -> str:
    """Real search using Brave Search API (free tier: 2,000 queries/month)."""
    url = "https://api.search.brave.com/res/v1/web/search"
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": BRAVE_API_KEY,
    }
    params = {"q": query, "count": max_results}

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(url, headers=headers, params=params)
        response.raise_for_status()
        data = response.json()

    results = []
    for item in data.get("web", {}).get("results", [])[:max_results]:
        results.append(
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("description", ""),
            }
        )

    return json.dumps(results, indent=2)


async def web_fetch(url: str) -> str:
    """Fetch the text content of a web page.

    Returns a truncated version (first 8,000 chars) to stay within
    context limits when feeding back to the LLM.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; DueDiligenceBot/1.0; research project)"
        )
    }

    async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()

    text = response.text
    # Strip HTML tags (rough but effective for feeding to LLM)
    import re

    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    # Truncate to ~8k chars to stay within context budget
    if len(text) > 8000:
        text = text[:8000] + "\n\n[... truncated — page content continues ...]"

    return text


def _mock_search(query: str, max_results: int = 5) -> str:
    """Mock search for local testing without API keys.

    Returns plausible-looking results so you can test the full
    agent loop — tool calling, parsing, state machine — without
    spending any API credits on search.

    Replace this with real search before pushing to GitHub.
    """
    mock_results = [
        {
            "title": f"About {query} - Company Overview",
            "url": f"https://example.com/{query.lower().replace(' ', '-')}",
            "snippet": (
                f"{query} is a technology company founded in 2015, "
                "headquartered in San Francisco, CA. The company employs "
                "approximately 3,500 people and specializes in AI-powered "
                "enterprise solutions. Key products include a developer "
                "platform and an analytics dashboard."
            ),
        },
        {
            "title": f"{query} - Crunchbase Company Profile",
            "url": f"https://crunchbase.com/organization/{query.lower()}",
            "snippet": (
                f"{query} has raised $450M in total funding across 5 rounds. "
                "Latest funding was a Series D of $200M in January 2025, "
                "led by Sequoia Capital. The company was valued at $4.5B."
            ),
        },
        {
            "title": f"{query} Leadership Team",
            "url": f"https://example.com/{query.lower()}/leadership",
            "snippet": (
                f"CEO Jane Smith co-founded {query} after a decade at Google. "
                "CTO Alex Chen joined from Meta's AI research lab. "
                "The leadership team has deep experience in machine learning "
                "and distributed systems."
            ),
        },
        {
            "title": f"{query} Tech Stack and Engineering Blog",
            "url": f"https://engineering.{query.lower()}.com",
            "snippet": (
                f"{query}'s platform is built on Python, Go, and Rust. "
                "They use Kubernetes for orchestration, PostgreSQL and "
                "DynamoDB for data storage, and have built custom ML "
                "infrastructure on top of PyTorch."
            ),
        },
        {
            "title": f"{query} News - Recent Developments",
            "url": f"https://techcrunch.com/{query.lower()}-latest",
            "snippet": (
                f"{query} announced a new AI agent platform in February 2026 "
                "and expanded into the European market. The company reported "
                "60% year-over-year revenue growth in their latest update."
            ),
        },
    ]

    return json.dumps(mock_results[:max_results], indent=2)
