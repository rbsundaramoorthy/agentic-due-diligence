"""
USPTO PatentsView tool function for the ResearchAgent.

Queries the PatentsView API v2 (https://search.patentsview.org) for US patents
assigned to a company. No API key required.

Returns patent count and top 10 patents by grant date (title, grant date, patent ID).

Rate limit: 5 req/sec (registry entry). Granted patents are immutable — cache forever.
"""

import json
from typing import Optional

import httpx

from src.sources.cache import SourceCache

_BASE_URL = "https://search.patentsview.org/api/g2/patent/query"
_TTL = None  # Granted patents are immutable


async def uspto_search_patents(
    company_name: str,
    cache: Optional[SourceCache] = None,
) -> str:
    """Search USPTO PatentsView for patents assigned to a company.

    Returns JSON: {found, patent_count, patents: [{patent_id, title, date}], source_url}
    Returns JSON: {found: false, note} when no patents are found or on error.
    """
    cache_params = {"op": "patent_search", "assignee": company_name.strip()}
    if cache:
        hit = cache.get("uspto", cache_params)
        if hit:
            return hit

    query = json.dumps({
        "assignees": {"assignee_organization": {"_text_phrase": company_name.strip()}}
    })
    fields = json.dumps([
        "patent_id", "patent_title", "patent_date", "assignees"
    ])
    options = json.dumps({"per_page": 10, "sort": [{"patent_date": "desc"}]})

    params = {"q": query, "f": fields, "o": options}
    source_url = _BASE_URL

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(_BASE_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return json.dumps({
            "found": False,
            "error": f"HTTP {e.response.status_code}",
            "note": "USPTO PatentsView API error.",
        })
    except Exception as e:
        return json.dumps({"found": False, "error": str(e)})

    total = data.get("total_patent_count", 0) or 0
    patents_raw = data.get("patents") or []

    if total == 0 or not patents_raw:
        result = json.dumps({
            "found": False,
            "patent_count": 0,
            "company_name": company_name,
            "source_url": source_url,
            "note": "No US patents found for this assignee name.",
        })
        if cache:
            cache.put("uspto", cache_params, result, url=source_url, ttl_seconds=_TTL)
        return result

    patents = []
    for p in patents_raw[:10]:
        patents.append({
            "patent_id": p.get("patent_id"),
            "title": p.get("patent_title"),
            "date": p.get("patent_date"),
        })

    result = json.dumps({
        "found": True,
        "patent_count": total,
        "patents": patents,
        "source_url": source_url,
        "note": (
            "Data from USPTO PatentsView. PRIMARY_DOCUMENT tier — US patent grants "
            "are official government records. Use HIGH confidence."
        ),
    })

    if cache:
        cache.put("uspto", cache_params, result, url=source_url, ttl_seconds=_TTL)
    return result
