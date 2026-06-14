"""
SAM.gov tool function for the RiskAgent.

Queries the SAM.gov Entity Management API to determine whether a company is
registered as an active federal contractor/grantee.

Requires SAM_GOV_API_KEY. Degrades gracefully when absent — returns a
"no_api_key" response so the agent falls back to web search rather than
failing hard.

Rate limit: 5 req/sec (registry entry). Contract awards update daily (TTL=86400s).
"""

import json
import os
from typing import Optional

import httpx

from src.sources.cache import SourceCache

_ENTITY_URL = "https://api.sam.gov/entity-information/v3/entities"
_TTL = 86400  # 24h — entity data updated daily


async def samgov_search_contracts(
    company_name: str,
    cache: Optional[SourceCache] = None,
) -> str:
    """Search SAM.gov for a company's federal contractor registration status.

    Returns JSON: {found, entity_name, registration_status, uei, cage_code,
                   entity_type, naics_codes, source_url}
    Returns JSON: {no_api_key, note} when SAM_GOV_API_KEY is not set.
    Returns JSON: {found: false, note} when no entity is found.
    """
    api_key = os.environ.get("SAM_GOV_API_KEY")
    if not api_key:
        return json.dumps({
            "no_api_key": True,
            "found": False,
            "note": (
                "SAM_GOV_API_KEY is not set. Federal contract exposure cannot be "
                "verified via SAM.gov. Set this key for authoritative contractor data. "
                "Use web search as fallback for government contract information."
            ),
        })

    cache_params = {"op": "entity_search", "legalBusinessName": company_name.strip()}
    if cache:
        hit = cache.get("sam_gov", cache_params)
        if hit:
            return hit

    async def _query(param_name: str, name: str):
        """Single SAM.gov API call; returns (entities, total) or raises."""
        p = {
            "api_key": api_key,
            param_name: name,
            "registrationStatus": "A",
            "includeSections": "entityRegistration,coreData",
        }
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(_ENTITY_URL, params=p)
            resp.raise_for_status()
            data = resp.json()
        return data.get("entityData", []), data.get("totalRecords", 0)

    name = company_name.strip()
    try:
        # Pass 1 — match against legal business name
        entities, total = await _query("legalBusinessName", name)

        # Pass 2 — if nothing found, try DBA / trade name
        if not entities or total == 0:
            entities, total = await _query("dbaName", name)

    except httpx.HTTPStatusError as e:
        return json.dumps({
            "found": False,
            "error": f"HTTP {e.response.status_code}",
            "detail": e.response.text[:300],
            "note": "SAM.gov API error.",
        })
    except Exception as e:
        return json.dumps({"found": False, "error": str(e)})

    if not entities or total == 0:
        result = json.dumps({
            "found": False,
            "company_name": company_name,
            "source_url": _ENTITY_URL,
            "note": (
                "No active SAM.gov entity registration found. "
                "Company may not be a registered federal contractor, or name may differ."
            ),
        })
        if cache:
            cache.put("sam_gov", cache_params, result, url=_ENTITY_URL, ttl_seconds=_TTL)
        return result

    # Best match: prefer exact name
    name_lower = company_name.strip().lower()
    best = None
    for entity in entities:
        reg = entity.get("entityRegistration", {})
        legal_name = reg.get("legalBusinessName", "")
        if legal_name.lower() == name_lower:
            best = entity
            break
    if best is None:
        best = entities[0]

    reg = best.get("entityRegistration", {})
    core = best.get("coreData", {})
    general_info = core.get("entityInformation", {})

    uei = reg.get("ueiSAM", "")
    cage = reg.get("cageCode", "")
    legal_name = reg.get("legalBusinessName", company_name)
    reg_status = reg.get("registrationStatus", "")
    entity_type = general_info.get("entityTypeCode", "")
    naics_list = []
    for naics in core.get("naicsCode", {}).get("naicsList", [])[:5]:
        code = naics.get("naicsCode", "")
        desc = naics.get("naicsDescription", "")
        if code:
            naics_list.append(f"{code} — {desc}" if desc else code)

    entity_url = f"https://www.sam.gov/SAM/entity/{uei}" if uei else _ENTITY_URL

    result = json.dumps({
        "found": True,
        "entity_name": legal_name,
        "registration_status": reg_status,
        "uei": uei,
        "cage_code": cage,
        "entity_type": entity_type,
        "naics_codes": naics_list,
        "source_url": entity_url,
        "note": (
            "Active SAM.gov registration confirms company is an eligible federal "
            "contractor. PRIMARY_DOCUMENT tier — SAM.gov is the official US federal "
            "vendor registry. Use HIGH confidence."
        ),
    })

    if cache:
        cache.put("sam_gov", cache_params, result, url=entity_url, ttl_seconds=_TTL)
    return result
