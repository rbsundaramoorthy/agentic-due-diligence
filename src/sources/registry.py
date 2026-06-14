"""
Source registry — authoritative mapping of source identifiers to tier and metadata.

The registry is the primary source of truth for tier classification of known
sources. _infer_tier() in assembler.py consults this registry before falling
back to hardcoded domain pattern sets for general web search results.

Adding a new source: add an entry here with the correct base_url. The tier
inference picks it up automatically — no changes to assembler.py needed.

P5 (domain profiles) will reference registry source_ids to declare which
sources each profile activates.
"""

from dataclasses import dataclass
from typing import Optional

from src.schemas.models import SourceTier


@dataclass(frozen=True)
class SourceEntry:
    source_id: str             # Stable identifier: "sec_edgar", "opencorporates_us", ...
    name: str                  # Human-readable display name
    tier: SourceTier           # Authoritative tier for all URLs from this source
    base_url: str              # Root URL for domain matching in _infer_tier()
    freshness_days: Optional[int]  # None = immutable; N = cache TTL in days
    rate_limit_rps: float      # Requests per second to respect
    requires_api_key: bool
    env_key: Optional[str]     # Environment variable name for the API key
    us_only: bool              # Whether queries must be restricted to US jurisdiction
    description: str


REGISTRY: dict[str, SourceEntry] = {
    "sec_edgar": SourceEntry(
        source_id="sec_edgar",
        name="SEC EDGAR",
        tier=SourceTier.PRIMARY_DOCUMENT,
        base_url="https://www.sec.gov",
        # Per-accession filing text is immutable; companyfacts uses 24h TTL in cache
        freshness_days=None,
        rate_limit_rps=10.0,
        requires_api_key=False,
        env_key=None,
        us_only=True,
        description=(
            "US SEC filings: 10-K, 10-Q, 8-K, S-1. Covers all SEC-reporting companies. "
            "EDGAR requires a User-Agent header — set EDGAR_USER_AGENT env var."
        ),
    ),
    "opencorporates_us": SourceEntry(
        source_id="opencorporates_us",
        name="OpenCorporates (US)",
        tier=SourceTier.REPUTABLE_SECONDARY,
        base_url="https://opencorporates.com",
        freshness_days=30,
        rate_limit_rps=5.0,
        requires_api_key=True,
        env_key="OPENCORPORATES_API_KEY",
        us_only=True,
        description=(
            "Entity registrations aggregated from US Secretaries of State. "
            "Classified Tier 1 (reputable_secondary) — it aggregates official registry "
            "data but is not itself a primary government source. "
            "US jurisdictions only; filter is required at the API call level. "
            "Free tier: ~50 calls/day. Set OPENCORPORATES_API_KEY for higher limits."
        ),
    ),
    "uspto": SourceEntry(
        source_id="uspto",
        name="USPTO PatentsView",
        tier=SourceTier.PRIMARY_DOCUMENT,
        base_url="https://search.patentsview.org",
        freshness_days=None,    # Granted patents are immutable
        rate_limit_rps=5.0,
        requires_api_key=False,
        env_key=None,
        us_only=True,
        description=(
            "USPTO patent grants and assignee data via the PatentsView API. "
            "No API key required. Covers non-US assignees with US patents."
        ),
    ),
    "sam_gov": SourceEntry(
        source_id="sam_gov",
        name="SAM.gov",
        tier=SourceTier.PRIMARY_DOCUMENT,
        base_url="https://www.sam.gov",
        freshness_days=1,       # Contract awards update daily
        rate_limit_rps=5.0,
        requires_api_key=True,
        env_key="SAM_GOV_API_KEY",
        us_only=True,
        description=(
            "US federal contract and grant awards. "
            "Free API key required (register at api.sam.gov). "
            "Set SAM_GOV_API_KEY; degrades gracefully if absent."
        ),
    ),
    "courtlistener": SourceEntry(
        source_id="courtlistener",
        name="CourtListener / RECAP",
        tier=SourceTier.PRIMARY_DOCUMENT,
        base_url="https://www.courtlistener.com",
        freshness_days=1,       # Docket data updates as cases proceed
        rate_limit_rps=5.0,
        requires_api_key=False,
        env_key="COURTLISTENER_API_KEY",
        us_only=True,
        description=(
            "US federal and state court records via PACER/RECAP. "
            "Works unauthenticated; set COURTLISTENER_API_KEY for higher rate limits."
        ),
    ),
}


def get_source(source_id: str) -> SourceEntry:
    """Return a registry entry by source_id. Raises KeyError for unknown IDs."""
    return REGISTRY[source_id]


def sources_by_tier(tier: SourceTier) -> list[SourceEntry]:
    """Return all registry entries for the given tier, in insertion order."""
    return [e for e in REGISTRY.values() if e.tier == tier]
