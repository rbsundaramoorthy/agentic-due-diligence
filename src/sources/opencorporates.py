"""
OpenCorporates tool function for the ResearchAgent.

US-jurisdiction-only search. Queries the OpenCorporates v0.4 search API,
then filters results server-side to entries whose jurisdiction_code starts
with 'us_'. Returns incorporation_date and registered_address for the
best-matching US entity.

Requires OPENCORPORATES_API_KEY. Fails loudly when key is absent so the
agent logs the disabled state and falls back to web search. This is by
design — the assembler does not need to handle a 'partial' case.

Rate limit: 5 req/sec (registry entry). Cached with 30-day TTL.
"""

import json
import os
from typing import Optional

import httpx

from src.sources.cache import SourceCache

_BASE_URL = "https://api.opencorporates.com/v0.4/companies/search"
_TTL = 30 * 86400  # 30 days


async def opencorporates_search_company(
    company_name: str,
    cache: Optional[SourceCache] = None,
) -> str:
    """Search OpenCorporates for a US company registration.

    Returns JSON: {found, company_name, jurisdiction_code, incorporation_date,
                   registered_address, company_number, source_url}
    Returns JSON: {disabled, note} when OPENCORPORATES_API_KEY is not set.
    Returns JSON: {found: false, note} when no US match is found.
    """
    api_key = os.environ.get("OPENCORPORATES_API_KEY")
    if not api_key:
        return json.dumps({
            "disabled": True,
            "found": False,
            "note": (
                "OPENCORPORATES_API_KEY is not set. "
                "Set this environment variable to enable OpenCorporates lookups. "
                "Falling back to web search for incorporation date and registered address."
            ),
        })

    cache_params = {"op": "search", "name": company_name.strip()}
    if cache:
        hit = cache.get("opencorporates_us", cache_params)
        if hit:
            return hit

    params = {
        "q": company_name.strip(),
        "api_token": api_key,
        "per_page": 20,
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(_BASE_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return json.dumps({
            "found": False,
            "error": f"HTTP {e.response.status_code}",
            "note": "OpenCorporates API error.",
        })
    except Exception as e:
        return json.dumps({"found": False, "error": str(e)})

    companies = data.get("results", {}).get("companies", [])

    # Filter to US jurisdictions (jurisdiction_code starts with "us_")
    us_companies = [
        c["company"] for c in companies
        if c.get("company", {}).get("jurisdiction_code", "").startswith("us_")
    ]

    if not us_companies:
        result = json.dumps({
            "found": False,
            "company_name": company_name,
            "note": "No US entity registrations found in OpenCorporates.",
        })
        if cache:
            cache.put("opencorporates_us", cache_params, result, url=_BASE_URL, ttl_seconds=_TTL)
        return result

    # Prefer exact name match; fall back to first US result
    name_lower = company_name.strip().lower()
    best = next(
        (c for c in us_companies if c.get("name", "").lower() == name_lower),
        us_companies[0],
    )

    source_url = best.get("opencorporates_url", _BASE_URL)
    result = json.dumps({
        "found": True,
        "company_name": best.get("name"),
        "jurisdiction_code": best.get("jurisdiction_code"),
        "company_number": best.get("company_number"),
        "incorporation_date": best.get("incorporation_date"),
        "registered_address": best.get("registered_address_in_full"),
        "company_status": best.get("current_status"),
        "source_url": source_url,
        "note": (
            "Data from OpenCorporates (aggregated from US Secretaries of State). "
            "Tier 1 (reputable secondary) — authoritative for incorporation date "
            "and registered address; use HIGH confidence when data is present."
        ),
    })

    if cache:
        cache.put("opencorporates_us", cache_params, result, url=source_url, ttl_seconds=_TTL)
    return result
