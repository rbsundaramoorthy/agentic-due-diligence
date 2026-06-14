"""
CourtListener tool function for the RiskAgent.

Queries the CourtListener REST API (https://www.courtlistener.com/api/rest/v4/)
for dockets (PACER court cases) involving a company as a party. Data covers
US federal and state courts via the RECAP Archive.

Works unauthenticated at reduced rate limits. Set COURTLISTENER_API_KEY for
higher limits (free account at courtlistener.com).

Rate limit: 5 req/sec (registry entry). Docket data updates as cases proceed (TTL=86400s).
"""

import json
import os
from typing import Optional
from urllib.parse import quote

import httpx

from src.sources.cache import SourceCache

_SEARCH_URL = "https://www.courtlistener.com/api/rest/v4/search/"
_BASE_URL = "https://www.courtlistener.com"
_TTL = 86400  # 24h — cases update as proceedings advance


def _docket_url(result: dict) -> str:
    """Build a verifiable docket page URL from a search result dict.

    Preference order:
    1. docket_absolute_url (relative path returned by CourtListener v4 search)
    2. docket_id  → /docket/{id}/
    3. docketNumber → search URL with encoded docket number
    Never returns the bare API search endpoint.
    """
    rel = result.get("docket_absolute_url", "")
    if rel:
        return rel if rel.startswith("http") else f"{_BASE_URL}{rel}"

    docket_id = result.get("docket_id")
    if docket_id:
        return f"{_BASE_URL}/docket/{docket_id}/"

    docket_number = result.get("docketNumber") or result.get("docket_number", "")
    if docket_number:
        return f"{_BASE_URL}/?q={quote(docket_number)}&type=d"

    # Should never reach here with real API responses, but guarantee no bare endpoint
    return f"{_BASE_URL}/"


async def courtlistener_search_cases(
    company_name: str,
    cache: Optional[SourceCache] = None,
) -> str:
    """Search CourtListener for court cases involving a company.

    Returns JSON: {found, case_count, cases: [{case_name, court, date_filed,
                   docket_number, source_url, parties, cause}], source_url}
    Returns JSON: {found: false, note} when no cases are found or on error.
    """
    cache_params = {"op": "docket_search", "q": company_name.strip()}
    if cache:
        hit = cache.get("courtlistener", cache_params)
        if hit:
            return hit

    api_key = os.environ.get("COURTLISTENER_API_KEY")
    headers: dict = {"User-Agent": "AgenteAuditBot/1.0 contact@example.com"}
    if api_key:
        headers["Authorization"] = f"Token {api_key}"

    params = {
        "type": "d",  # dockets
        "q": f'"{company_name.strip()}"',
        "order_by": "score desc",
        "page_size": 10,
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(_SEARCH_URL, headers=headers, params=params)
            if resp.status_code == 429:
                return json.dumps({
                    "found": False,
                    "error": "rate_limited",
                    "note": (
                        "CourtListener rate limit reached. "
                        "Set COURTLISTENER_API_KEY for higher limits."
                    ),
                })
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return json.dumps({
            "found": False,
            "error": f"HTTP {e.response.status_code}",
            "note": "CourtListener API error.",
        })
    except Exception as e:
        return json.dumps({"found": False, "error": str(e)})

    count = data.get("count", 0)
    results = data.get("results") or []

    if count == 0 or not results:
        result = json.dumps({
            "found": False,
            "case_count": 0,
            "company_name": company_name,
            "source_url": _SEARCH_URL,
            "note": "No court cases found in CourtListener RECAP archive for this party name.",
        })
        if cache:
            cache.put("courtlistener", cache_params, result, url=_SEARCH_URL, ttl_seconds=_TTL)
        return result

    cases = []
    for c in results[:10]:
        cases.append({
            "case_name":     c.get("caseName") or c.get("case_name", ""),
            "court":         c.get("court_id") or c.get("court", ""),
            "date_filed":    c.get("dateFiled") or c.get("date_filed", ""),
            "docket_number": c.get("docketNumber") or c.get("docket_number", ""),
            "source_url":    _docket_url(c),
            "parties":       c.get("party") or [],
            "cause":         c.get("cause") or "",
        })

    result = json.dumps({
        "found": True,
        "case_count": count,
        "cases_returned": len(cases),
        "cases": cases,
        "source_url": _SEARCH_URL,
        "note": (
            "Data from CourtListener RECAP Archive. PRIMARY_DOCUMENT tier — "
            "sourced from PACER federal court records. "
            "Each case includes source_url (specific docket page), parties (named parties), "
            "and cause. Use each case's own source_url when citing it."
        ),
    })

    if cache:
        cache.put("courtlistener", cache_params, result, url=_SEARCH_URL, ttl_seconds=_TTL)
    return result
