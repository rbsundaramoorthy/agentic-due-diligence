"""
Report assembler — converts raw agent outputs into the canonical ReportDocument.

This is the single point where agent-dict outputs are mapped into the
structured, versioned, tier-annotated canonical report. All renderers
(markdown, PDF, HTML) consume ReportDocument; they never receive raw dicts.

Public API:
    assemble_report(...)  -> ReportDocument
    claim_as_dp(claim)    -> dict  (legacy dict for backward-compat shim renderers)
    build_render_dicts(doc) -> tuple  (reconstructs legacy dicts from a ReportDocument)
"""

import copy
import uuid
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from src.schemas.models import (
    AgentRunMetadata,
    Claim,
    ConfidenceLevel,
    GapRecord,
    ReportDocument,
    ReportFinancial,
    ReportResearch,
    ReportRisk,
    ReportSocialMedia,
    ReportSynthesis,
    RunMetadata,
    SeverityLevel,
    SourceRef,
    SourceTier,
)
from src.sources.registry import REGISTRY


# ── Source tier inference ─────────────────────────────────────────────────────
# P1: tier is inferred from domain patterns.
# P3: tier will be recorded at retrieval time by the source registry.
#
# Generic *.gov rule: the .gov TLD is restricted to verified US government
# entities, so any *.gov domain is PRIMARY_DOCUMENT by default. The explicit
# entries below are kept for documentation; the generic rule catches the long
# tail without manual enumeration.

_PRIMARY_DOCUMENT = {
    # US government — core regulatory filings
    "sec.gov", "efts.sec.gov", "pacer.gov", "courts.gov",
    "sam.gov", "usaspending.gov", "ofac.treas.gov",
    "federalregister.gov", "federalreserve.gov",
    # US government — legislative and executive
    "congress.gov", "whitehouse.gov", "gpo.gov", "gao.gov", "cbo.gov",
    # US government — sectoral regulators
    "faa.gov", "fcc.gov", "ftc.gov", "dot.gov", "dol.gov",
    "nrc.gov", "epa.gov", "justice.gov", "state.gov", "treasury.gov",
    "nasa.gov", "uspto.gov",
    # US government — statistics
    "bls.gov", "bea.gov", "census.gov",
    # Non-.gov Tier 0 primary sources
    "courtlistener.com",
    "fred.stlouisfed.org",       # St. Louis Fed FRED economic data
    "patents.google.com",
    "patents.justia.com",
    "companieshouse.gov.uk",     # UK Companies House (.gov.uk not caught by *.gov rule)
    "sedar.com",                 # Canadian regulatory filings
    "usaspending.gov",
    # Official company primary domains (subdomains match via endswith rule)
    # Added as observed in real runs — company's own website is the authoritative
    # source for its own announcements, pricing, documentation, and status.
    "openai.com",                # covers developers.openai.com, help.openai.com, status.openai.com
    "anthropic.com",             # covers alignment.anthropic.com and other subdomains
    "claude.com",                # Claude product site (Anthropic); pricing, release notes
    "apple.com",                 # covers newsroom.apple.com, investor.apple.com, etc.
}

_REPUTABLE_SECONDARY = {
    # Wire services and major newspapers
    "reuters.com", "apnews.com", "bloomberg.com", "ft.com",
    "wsj.com", "nytimes.com", "washingtonpost.com", "usatoday.com",
    "latimes.com", "theguardian.com", "independent.co.uk",
    "bbc.com", "bbc.co.uk", "aljazeera.com",
    "economist.com", "theatlantic.com",
    # US broadcast / public media
    "cnbc.com", "cbsnews.com", "nbcnews.com", "abcnews.go.com",
    "cnn.com",                   # CNN — major US TV news network
    "npr.org", "pbs.org",
    # Business / finance press
    "forbes.com", "fortune.com", "businessinsider.com",
    "inc.com", "fastcompany.com", "axios.com",
    "politico.com", "theinformation.com", "morningstar.com",
    "marketwatch.com", "seekingalpha.com", "barrons.com",
    "investopedia.com", "fool.com", "usnews.com",
    # Technology press
    "techcrunch.com", "wired.com", "theverge.com", "arstechnica.com",
    "venturebeat.com", "zdnet.com", "cnet.com", "engadget.com",
    "thenextweb.com", "9to5mac.com",
    # Security / risk press
    "darkreading.com", "securityweek.com", "krebsonsecurity.com",
    # Aerospace / space industry press
    "spacenews.com", "nasaspaceflight.com", "payloadspace.com",
    "space.com",                 # Space.com — major science/space journalism
    # Reference / encyclopedic
    "britannica.com", "wikipedia.org",  # editorial standards justify reputable_secondary
    # Policy / think tanks
    "benton.org",
    # Analyst / legal commentary
    "nelsonmullins.com",
    "grellas.com",               # Grellas Shah LLP — Silicon Valley tech law firm
    "wsgr.com",                  # Wilson Sonsini — major tech/startup law firm
    # Financial and payments press
    "cmcmarkets.com",            # CMC Markets — established financial trading and news
    "ig.com",                    # IG Group — UK financial trading platform and news
    "investing.com",             # Investing.com — global financial news and data
    "pymnts.com",                # PYMNTS.com — payments industry journalism
    # SaaS / tech business press
    "saastr.com",                # SaaStr — SaaS business publication and analysis
    "thenewstack.io",            # The New Stack — cloud-native/developer journalism
    "techi.com",                 # Techi.com — tech news outlet
    # Regional newspapers (Gannett and local TV)
    "expressnews.com",           # San Antonio Express-News
    "floridatoday.com",          # Florida Today
    "valleycentral.com",         # Valley Central — Rio Grande Valley TV news
}

_AGGREGATOR = {
    # Startup / private company data
    "crunchbase.com", "pitchbook.com", "owler.com", "craft.co",
    "tracxn.com", "cbinsights.com", "dealroom.co", "forgeglobal.com",
    "equityzen.com", "growjo.com", "newcomer.co",
    # Financial data aggregators
    "macrotrends.net", "similarweb.com", "statista.com",
    "wisesheets.io", "tradingkey.com", "trustfinance.com",
    # Private-company revenue / metrics trackers
    "sacra.com", "revenuememo.com", "techmarketbriefs.com",
    "trueup.io", "quiltyspace.com",
    # Space / aerospace market aggregators
    "spacexstock.com", "tsginvest.com", "summit-ventures.net",
    "fed-spend.com", "premieralts.com",
    # B2B data / contact aggregators
    "apollo.io", "zoominfo.com", "rocketreach.co", "signalhire.com",
    # Business directories and review aggregators
    "bbb.org", "bizjournals.com",
    # Framework / analysis template sites (low editorial value)
    "pestel-analysis.com", "portersfiveforce.com",
    # Security / risk aggregators
    "thecyberexpress.com", "rankiteo.com", "speakrj.com",
    # Other aggregators observed in runs
    "kucoin.com", "papermag.com", "sqmagazine.co.uk",
    "endroid.com", "hrgrapevine.com", "gizmodo.com",
    # AI/tech content aggregators and analytics tools (OpenAI + Anthropic runs)
    "crescendo.ai",              # AI content aggregator / company blog
    "enterprise-ai.io",          # AI knowledge directory
    "highperformr.ai",           # Social media analytics and company profile aggregator
    "releasebot.io",             # Software release notes tracker
    "tweetstorm.ai",             # AI-curated social media content
    "amperly.com",               # Social media account aggregator
    "dexteragent.ai",            # AI company profile aggregator
    "favikon.com",               # Influencer analytics and marketing platform
    "getpanto.ai",               # AI statistics and pricing aggregator
    "intuitionlabs.ai",          # AI company blog / pricing analysis
    "makerstations.io",          # Tech company statistics aggregator
    "metacto.com",               # CTO-focused tech content aggregator
    "europeanbusinessmagazine.com", # European business magazine (online-only, lower editorial bar)
    # Company/job data aggregators
    "jobsbyculture.com",         # Company and job data aggregator
    "salestools.io",             # B2B sales data aggregator
    # VC-affiliated and niche research aggregators
    "contrary.com",              # Contrary Research — VC research compilation (covers research.contrary.com)
    # Financial news aggregators (SpaceX run)
    "stockpil.com",              # Financial news aggregator
    "newsbytesapp.com",          # News summarization app (Indian market; aggregates from wires)
}

_COMMUNITY = {
    # Social networks
    "reddit.com", "twitter.com", "x.com",
    "facebook.com", "instagram.com", "tiktok.com", "youtube.com",
    # Professional / employment
    "linkedin.com", "glassdoor.com", "indeed.com",
    "teamblind.com", "blind.app", "levels.fyi", "comparably.com",
    # Consumer reviews
    "trustpilot.com", "consumeraffairs.com", "pissedconsumer.com",
    # Discussion / Q&A
    "quora.com", "news.ycombinator.com", "hackernews.ycombinator.com",
    "stackoverflow.com", "stackexchange.com",
    # Developer / creator platforms (host both primary and community content;
    # default to community — revisit per-claim if needed)
    "github.com", "gitlab.com",
    "medium.com", "substack.com",
    # Fan wikis and community encyclopedias
    "fandom.com",                # covers *.fandom.com (e.g., starship-spacex.fandom.com)
    # Complaint / controversy commentary sites (OpenAI run)
    "chatgptdisaster.com",       # AI complaint/controversy commentary
    "openrealnews.com",          # User-generated complaints aggregator
}


def _infer_tier(url: str) -> SourceTier:
    """Infer the source tier from a URL's domain.

    Resolution order:
    1. Source registry — authoritative for known Tier 0/1 registered sources.
    2. Explicit domain sets — _PRIMARY_DOCUMENT, _REPUTABLE_SECONDARY,
       _AGGREGATOR, _COMMUNITY (subdomain matching included).
    3. Generic *.gov fallback — any .gov domain not matched above is
       PRIMARY_DOCUMENT. The .gov TLD is restricted to verified US government
       entities, so this is safe as a blanket rule.
    4. UNKNOWN for everything else.

    The www. prefix is stripped before matching.
    """
    try:
        raw = urlparse(url).netloc.lower()
        domain = raw.removeprefix("www.")
        # 1. Registry-first: authoritative tier for known sources
        for entry in REGISTRY.values():
            reg_domain = urlparse(entry.base_url).netloc.lower().removeprefix("www.")
            if domain == reg_domain or domain.endswith("." + reg_domain):
                return entry.tier
        # 2. Explicit domain sets
        for domain_set, tier in (
            (_PRIMARY_DOCUMENT,    SourceTier.PRIMARY_DOCUMENT),
            (_REPUTABLE_SECONDARY, SourceTier.REPUTABLE_SECONDARY),
            (_AGGREGATOR,          SourceTier.AGGREGATOR),
            (_COMMUNITY,           SourceTier.COMMUNITY),
        ):
            if domain in domain_set or any(domain.endswith("." + d) for d in domain_set):
                return tier
        # 3. Generic *.gov fallback
        if domain.endswith(".gov"):
            return SourceTier.PRIMARY_DOCUMENT
    except Exception:
        pass
    return SourceTier.UNKNOWN


# ── Credibility cap thresholds ────────────────────────────────────────────────
# All tunable numbers live here. Cap implementations read from this block only.
#
# Cap 1a — per-claim confidence ceiling tied to best source tier.
# Cap 1b — report-level data_quality ceiling from tier_coverage shares.
# Cap 2  — LOW ceiling + unverified_financial flag for material financial claims
#           whose sources carry no reputable or primary evidence.
#
# These are CAPS: they only ever lower confidence/quality, never raise it.

# Tiers that make a claim HIGH-eligible (Cap 1a).
_HIGH_ELIGIBLE_TIERS: frozenset = frozenset({
    SourceTier.PRIMARY_DOCUMENT,
    SourceTier.REPUTABLE_SECONDARY,
})

# Cap 1b thresholds (U = unknown share, P = primary+reputable share).
_CAP1B_UNKNOWN_FORCE_LOW: float  = 0.40  # U >= → force data_quality to "low"
_CAP1B_PRIMARY_FORCE_LOW: float  = 0.25  # P <  → force data_quality to "low"
_CAP1B_UNKNOWN_HIGH_MAX:  float  = 0.20  # U <  required for "high" data_quality
_CAP1B_PRIMARY_HIGH_MIN:  float  = 0.50  # P >= required for "high" data_quality

# Cap 2 material financial fields (quantitative dollar/pct figures).
_MATERIAL_FINANCIAL_FIELDS: frozenset = frozenset({
    "revenue", "revenue_growth", "profitability", "valuation", "total_funding",
})
# Cap 2 fires when ALL sources fall within these tiers (no primary/reputable evidence).
_CAP2_WEAK_TIERS: frozenset = frozenset({
    SourceTier.COMMUNITY,
    SourceTier.AGGREGATOR,
    SourceTier.UNKNOWN,
})

# Tier ordering used by cap helpers (lower index = higher quality).
_TIER_ORDER: Dict[SourceTier, int] = {
    SourceTier.PRIMARY_DOCUMENT:    0,
    SourceTier.REPUTABLE_SECONDARY: 1,
    SourceTier.AGGREGATOR:          2,
    SourceTier.COMMUNITY:           3,
    SourceTier.UNKNOWN:             4,
}
# Confidence ordering used by cap helpers (lower index = higher confidence).
_CONF_ORDER: Dict[str, int] = {"high": 0, "medium": 1, "low": 2, "unknown": 3}
# data_quality value ordering (lower index = higher quality).
_DQ_ORDER: Dict[str, int] = {"high": 0, "medium": 1, "low": 2}


def _best_tier(tiers: List[SourceTier]) -> Optional[SourceTier]:
    """Return the highest-quality tier from a list, or None if empty."""
    return min(tiers, key=lambda t: _TIER_ORDER.get(t, 99)) if tiers else None


def _best_tier_for_claim(claim: "Claim", claim_index: Dict[str, "Claim"]) -> Optional[SourceTier]:
    """Resolve the best source tier for a claim.

    Checks direct sources first. Falls back one level through derived_from or
    synthesized_from when no direct sources are present (e.g. synthesis claims
    that reference upstream claims by ID instead of carrying their own sources).
    """
    if claim.sources:
        return _best_tier([ref.tier for ref in claim.sources])
    ref_ids = claim.derived_from or claim.synthesized_from
    if ref_ids:
        tiers: List[SourceTier] = []
        for rid in ref_ids:
            parent = claim_index.get(rid)
            if parent:
                for ref in parent.sources:
                    tiers.append(ref.tier)
        return _best_tier(tiers) if tiers else None
    return None


def _cap1a_ceiling(claim: "Claim", claim_index: Dict[str, "Claim"]) -> "ConfidenceLevel":
    """Return the Cap 1a confidence ceiling for a claim based on its best source tier."""
    best = _best_tier_for_claim(claim, claim_index)
    if best is None:
        return ConfidenceLevel.LOW       # no sources at all
    if best in _HIGH_ELIGIBLE_TIERS:
        return ConfidenceLevel.HIGH      # primary/reputable — no cap
    return ConfidenceLevel.MEDIUM        # aggregator/community/unknown → MEDIUM ceiling


def _apply_cap1a(claim: "Claim", claim_index: Dict[str, "Claim"]) -> "Claim":
    """Apply Cap 1a: cap confidence to ceiling from best source tier. Never raises."""
    ceiling = _cap1a_ceiling(claim, claim_index)
    if _CONF_ORDER.get(claim.confidence.value, 99) < _CONF_ORDER.get(ceiling.value, 99):
        return claim.model_copy(update={"confidence": ceiling})
    return claim


def _apply_cap2(claim: "Claim", claim_index: Dict[str, "Claim"]) -> "Claim":
    """Apply Cap 2: LOW ceiling + unverified_financial flag for weak-sourced financial claims.

    Fires when:
    - field is in _MATERIAL_FINANCIAL_FIELDS, AND
    - ALL direct sources are in _CAP2_WEAK_TIERS (no primary/reputable), AND
    - claim is not derived from a primary/reputable source via derived_from/synthesized_from.

    Stricter than Cap 1a: result is LOW (not MEDIUM) plus the unverified_financial flag.
    """
    base_field = claim.field_name.split("[")[0]
    if base_field not in _MATERIAL_FINANCIAL_FIELDS:
        return claim
    # Exempt if any parent claim carries a strong source
    ref_ids = claim.derived_from or claim.synthesized_from
    for rid in ref_ids:
        parent = claim_index.get(rid)
        if parent and any(ref.tier not in _CAP2_WEAK_TIERS for ref in parent.sources):
            return claim
    # Exempt if any direct source is strong
    if claim.sources and any(ref.tier not in _CAP2_WEAK_TIERS for ref in claim.sources):
        return claim
    # All sources weak (or absent) → fire Cap 2
    updates: Dict = {"unverified_financial": True}
    if _CONF_ORDER.get(claim.confidence.value, 99) < _CONF_ORDER["low"]:
        updates["confidence"] = ConfidenceLevel.LOW
    return claim.model_copy(update=updates)


def _cap_section(section, claim_index: Dict[str, "Claim"], apply_cap2: bool = False):
    """Apply Cap 1a (and optionally Cap 2) to every Claim in a section model.

    Returns a model_copy when any claim is changed; returns the original object
    unchanged when no caps fire (avoids unnecessary allocations).
    """
    if section is None:
        return None
    updates: Dict = {}
    for fname in type(section).model_fields:
        val = getattr(section, fname)
        if isinstance(val, Claim):
            capped = _apply_cap1a(val, claim_index)
            if apply_cap2:
                capped = _apply_cap2(capped, claim_index)
            if capped is not val:
                updates[fname] = capped
        elif isinstance(val, list):
            new_list, changed = [], False
            for item in val:
                if isinstance(item, Claim):
                    capped = _apply_cap1a(item, claim_index)
                    if apply_cap2:
                        capped = _apply_cap2(capped, claim_index)
                    if capped is not item:
                        changed = True
                    new_list.append(capped)
                else:
                    new_list.append(item)
            if changed:
                updates[fname] = new_list
    return section.model_copy(update=updates) if updates else section


def _apply_cap1b(
    data_quality_claim: Optional["Claim"],
    tier_coverage: Dict[str, float],
) -> Optional["Claim"]:
    """Apply Cap 1b: cap the data_quality VALUE based on tier_coverage shares.

    U = unknown share; P = primary_document + reputable_secondary share.
    Ceiling logic:
      "high"  allowed ONLY IF U < _CAP1B_UNKNOWN_HIGH_MAX AND P >= _CAP1B_PRIMARY_HIGH_MIN
      "low"   forced   IF U >= _CAP1B_UNKNOWN_FORCE_LOW OR P < _CAP1B_PRIMARY_FORCE_LOW
      otherwise "medium"
    Only lowers the VALUE — never raises it.
    """
    if data_quality_claim is None:
        return None
    u = tier_coverage.get("unknown", 0.0)
    p = (tier_coverage.get("primary_document", 0.0)
         + tier_coverage.get("reputable_secondary", 0.0))
    if u >= _CAP1B_UNKNOWN_FORCE_LOW or p < _CAP1B_PRIMARY_FORCE_LOW:
        ceiling = "low"
    elif u < _CAP1B_UNKNOWN_HIGH_MAX and p >= _CAP1B_PRIMARY_HIGH_MIN:
        ceiling = "high"
    else:
        ceiling = "medium"
    declared = data_quality_claim.value
    if _DQ_ORDER.get(declared, 99) < _DQ_ORDER.get(ceiling, 99):
        return data_quality_claim.model_copy(update={"value": ceiling})
    return data_quality_claim


def _build_claim_index(*sections) -> Dict[str, "Claim"]:
    """Build a claim_id → Claim mapping from all provided sections."""
    index: Dict[str, "Claim"] = {}
    for claim in _iter_section_claims(*sections):
        index[claim.claim_id] = claim
    return index


def _apply_credibility_caps(
    research,
    financial,
    risk,
    social_media,
    synthesis,
    tier_coverage: Dict[str, float],
) -> tuple:
    """Apply Cap 1a, Cap 1b, and Cap 2 to all sections.

    Must be called after all EDGAR merges and gap pruning, and BEFORE
    section_confidence computation so that aggregates reflect post-cap values.
    """
    # Build claim index from specialist sections for tier traversal in Cap 1a/2.
    # Synthesis claims reference upstream specialist claims via synthesized_from.
    claim_index = _build_claim_index(research, financial, risk, social_media)
    # Cap 1a on all sections; Cap 2 only on financial (material financial fields).
    research     = _cap_section(research,     claim_index, apply_cap2=False)
    financial    = _cap_section(financial,    claim_index, apply_cap2=True)
    risk         = _cap_section(risk,         claim_index, apply_cap2=False)
    social_media = _cap_section(social_media, claim_index, apply_cap2=False)
    synthesis    = _cap_section(synthesis,    claim_index, apply_cap2=False)
    # Cap 1b: cap the data_quality VALUE from tier_coverage shares (separate from Cap 1a
    # which caps confidence; Cap 1b caps the quality assessment value itself).
    if synthesis is not None and synthesis.data_quality is not None:
        new_dq = _apply_cap1b(synthesis.data_quality, tier_coverage)
        if new_dq is not synthesis.data_quality:
            synthesis = synthesis.model_copy(update={"data_quality": new_dq})
    return research, financial, risk, social_media, synthesis


# ── Synthesis field classification ───────────────────────────────────────────
# Specific synthesis: directly derived from one or more upstream claims.
# synthesized_from MUST be non-empty. The Claim validator enforces this.
SPECIFIC_SYNTHESIS_FIELDS: frozenset[str] = frozenset({
    "key_strengths",
    "key_concerns",
    "red_flags",
    "data_conflicts",
})

# General synthesis: editorial judgment over the full pattern of findings.
# synthesized_from may be empty IF reasoning is non-empty.
# Both non-empty is preferred; neither is a validation error at the Claim level
# (assembly-level validation handles hard failures on dangling references).
GENERAL_SYNTHESIS_FIELDS: frozenset[str] = frozenset({
    "executive_summary",
    "recommendation",
    "recommendation_rationale",
    "follow_up_questions",
    "data_quality",
})

SYNTHESIS_FIELDS: frozenset[str] = SPECIFIC_SYNTHESIS_FIELDS | GENERAL_SYNTHESIS_FIELDS


# ── Claim-id pre-assignment ───────────────────────────────────────────────────

def _new_claim_id() -> str:
    return uuid.uuid4().hex[:12]


def _walk_annotate(obj: object) -> None:
    """Recursively inject _claim_id into every DataPoint-shaped dict."""
    if isinstance(obj, dict):
        if "value" in obj and "confidence" in obj and "_claim_id" not in obj:
            obj["_claim_id"] = _new_claim_id()
        for v in obj.values():
            _walk_annotate(v)
    elif isinstance(obj, list):
        for item in obj:
            _walk_annotate(item)


def annotate_claim_ids(data: Optional[dict]) -> Optional[dict]:
    """Return a deep copy of an agent output dict with _claim_id pre-assigned.

    Call this on Phase 1 outputs before building the synthesis task and before
    calling assemble_report. The synthesis agent sees these IDs in its prompt
    and records which ones it drew from in synthesized_from. The assembler
    honours the pre-assigned IDs (via _dp_to_claim) so every upstream Claim
    keeps the same ID the synthesis agent cited — the chain is stable end-to-end.

    IMPORTANT — ID preservation contract:
    Any future intermediate pipeline stage inserted between Phase 1 and assembly
    (e.g., P6 conflict resolution, P2 passage-level evidence enrichment) MUST
    preserve _claim_id values unchanged on DataPoints it passes through. Rewriting
    or dropping _claim_id breaks the synthesized_from → Claim linkage that makes
    the provenance chain auditable. New claims introduced by an intermediate stage
    should inject their own _claim_id via annotate_claim_ids or _new_claim_id().
    """
    if data is None:
        return None
    result = copy.deepcopy(data)
    _walk_annotate(result)
    return result


# ── DataPoint → Claim conversion ──────────────────────────────────────────────


def _is_url(s: str) -> bool:
    return s.startswith("http://") or s.startswith("https://")


def _build_field_map(data: Optional[dict]) -> dict:
    """Map field paths to _claim_id for an annotated agent output dict.

    Enables agents to use field names (e.g. "revenue", "recent_financial_events[0]")
    in derived_from instead of self-assigned _claim_ids. The assembler resolves
    these paths to actual claim_ids before creating Claim objects, so _validate_derived_from
    can enforce hard correctness without requiring LLM look-ahead.
    """
    if not data:
        return {}
    field_map: dict = {}
    for key, val in data.items():
        if key == "company_name":
            continue
        if isinstance(val, dict) and "_claim_id" in val:
            field_map[key] = val["_claim_id"]
        elif isinstance(val, list):
            for i, item in enumerate(val):
                if isinstance(item, dict) and "_claim_id" in item:
                    field_map[f"{key}[{i}]"] = item["_claim_id"]
    return field_map


def _dp_to_claim(
    dp: Optional[dict],
    agent: str,
    field_name: str,
    field_map: Optional[dict] = None,
) -> Optional[Claim]:
    """Convert a DataPoint dict to a Claim.

    Returns None if dp is absent or fully unknown (value='unknown' AND
    confidence='unknown'). Those become GapRecord entries instead.

    field_map: mapping from field paths to _claim_id; used to resolve agent-supplied
    field names in derived_from (e.g. "revenue") to actual claim_ids.
    """
    if dp is None:
        return None
    value = dp.get("value", "unknown")
    conf_raw = dp.get("confidence", "unknown")
    if value == "unknown" and conf_raw == "unknown":
        return None

    try:
        conf = ConfidenceLevel(conf_raw)
    except ValueError:
        conf = ConfidenceLevel.UNKNOWN

    sev: Optional[SeverityLevel] = None
    if dp.get("severity"):
        try:
            sev = SeverityLevel(dp["severity"])
        except ValueError:
            pass

    url_sources = [s for s in dp.get("sources", []) if _is_url(s)]

    raw_derived_from = dp.get("derived_from") or []
    resolved_derived_from = [(field_map or {}).get(ref, ref) for ref in raw_derived_from]

    return Claim(
        claim_id=dp.get("_claim_id") or _new_claim_id(),
        field_name=field_name,
        value=value,
        confidence=conf,
        severity=sev,
        sources=[SourceRef(url=u, tier=_infer_tier(u)) for u in url_sources],
        agent=agent,
        reasoning=dp.get("reasoning"),
        synthesized_from=dp.get("synthesized_from") or [],
        derived=bool(dp.get("derived", False)),
        derived_from=resolved_derived_from,
    )


def _dp_list_to_claims(
    items: list,
    agent: str,
    field_name: str,
    field_map: Optional[dict] = None,
) -> List[Claim]:
    """Convert a list of DataPoint dicts to Claims, skipping None/unknown items."""
    claims = []
    for i, dp in enumerate(items or []):
        claim = _dp_to_claim(dp, agent, f"{field_name}[{i}]", field_map=field_map)
        if claim is not None:
            claims.append(claim)
    return claims


def _collect_gaps(data: Optional[dict], agent: str) -> List[GapRecord]:
    """Extract fields with confidence=unknown from an agent output dict."""
    if not data:
        return []
    gaps = []
    for key, val in data.items():
        if key == "company_name":
            continue
        if isinstance(val, dict) and val.get("confidence") == "unknown":
            gaps.append(GapRecord(field=key, agent=agent, reason="No reliable data found"))
        elif isinstance(val, list) and val:
            if all(isinstance(item, dict) and item.get("confidence") == "unknown" for item in val):
                gaps.append(GapRecord(field=key, agent=agent, reason="No reliable data found"))
    return gaps


def _is_assembled_empty(val) -> bool:
    """Return True when an assembled section field carries no real value.

    Scalar field (Optional[Claim]): empty when None.
    List field   (List[Claim]):     empty when the collection is empty.

    This is the single definition of "empty/gap" used by both _prune_gaps and
    the invariant guard test. It operates on the assembled Claim layer, where
    _dp_to_claim has already flattened fully-unknown DataPoints
    ({value: "unknown", confidence: "unknown"}) to None. A surviving Claim
    always carries a real value; the placeholder case cannot occur.
    """
    if isinstance(val, list):
        return len(val) == 0
    return val is None


def _prune_gaps(gaps: List[GapRecord], *sections) -> List[GapRecord]:
    """Remove from gaps any field that is now populated in the assembled sections.

    Must be called AFTER all merges (EDGAR overlay, etc.) so the gaps list
    reflects the final document state, not an intermediate one.

    Why prune rather than recompute from scratch: _collect_gaps preserves a
    semantic that is lost in the Claim layer — None in the agent dict means
    "field not attempted" (not a gap), while {confidence: unknown} means "tried
    and found nothing" (a gap). Both become None in the assembled Claim. Pruning
    targets only the specific invariant: a populated field must not be in gaps.
    Relies on _dp_to_claim's flattening guarantee: unknown DataPoints → None,
    so _is_assembled_empty(None) is always the correct empty test.
    """
    populated: set[str] = {
        field_name
        for section in sections
        if section is not None
        for field_name in type(section).model_fields
        if not _is_assembled_empty(getattr(section, field_name))
    }
    return [g for g in gaps if g.field not in populated]


# ── EDGAR merge helpers ───────────────────────────────────────────────────────

def _merge_edgar(
    financial: Optional[ReportFinancial],
    edgar_data: dict,
) -> Optional[ReportFinancial]:
    """Overlay EDGAR-sourced revenue/profitability onto the financial section.

    EDGAR values take precedence when edgar_lookup_status == 'succeeded' and
    the DataPoint confidence is not unknown. Financial agent values are kept
    for all other fields (investors, funding, valuation, etc.).
    """
    if financial is None or not edgar_data:
        return financial
    if edgar_data.get("edgar_lookup_status") != "succeeded":
        return financial

    updates: dict = {}
    for field in ("revenue", "profitability"):
        dp = edgar_data.get(field)
        if dp and dp.get("confidence") != "unknown":
            claim = _dp_to_claim(dp, "edgar", field)
            if claim is not None:
                updates[field] = claim
    return financial.model_copy(update=updates) if updates else financial


def _merge_edgar_into_risk(
    risk: Optional[ReportRisk],
    edgar_data: dict,
) -> Tuple[Optional[ReportRisk], List[GapRecord]]:
    """Prepend SEC-disclosed risk factors to the regulatory_risks list.

    Returns (updated_risk, new_gaps). When EDGAR lookup succeeded but risk-factor
    extraction yielded no content, a GapRecord is returned so the absence is
    visible in the report rather than silently missing.
    """
    new_gaps: List[GapRecord] = []
    if risk is None or not edgar_data:
        return risk, new_gaps
    if edgar_data.get("edgar_lookup_status") != "succeeded":
        return risk, new_gaps

    sec_factors = edgar_data.get("sec_risk_factors") or []
    edgar_claims = _dp_list_to_claims(sec_factors, "edgar", "regulatory_risks")
    if not edgar_claims:
        new_gaps.append(GapRecord(
            field="sec_risk_factors",
            agent="edgar",
            reason=(
                "EDGAR lookup succeeded (CIK resolved, filing located) but risk-factor "
                "extraction returned no content. The filing agent may need to retry or "
                "the section uses non-standard headings."
            ),
        ))
        return risk, new_gaps
    return (
        risk.model_copy(update={"regulatory_risks": edgar_claims + list(risk.regulatory_risks)}),
        new_gaps,
    )


# ── Section confidence ────────────────────────────────────────────────────────

_CONFIDENCE_SCORES: Dict[str, float] = {
    "high": 1.0,
    "medium": 0.66,
    "low": 0.33,
    "unknown": 0.0,
}

# Weights used for overall_confidence (Financial 40%, Risk 40%, Social Media 20%).
_SECTION_WEIGHTS: Dict[str, float] = {
    "financial": 0.40,
    "risk": 0.40,
    "social_media": 0.20,
}


def _section_confidence_score(section) -> Optional[float]:
    """Return the mean confidence score (0.0–1.0) for all Claims in a section.

    Returns None if the section is None or has no Claims.
    """
    if section is None:
        return None
    total, count = 0.0, 0
    for fname in type(section).model_fields:
        val = getattr(section, fname)
        if isinstance(val, Claim):
            total += _CONFIDENCE_SCORES.get(val.confidence.value, 0.0)
            count += 1
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, Claim):
                    total += _CONFIDENCE_SCORES.get(item.confidence.value, 0.0)
                    count += 1
    return total / count if count > 0 else None


def compute_section_confidences(doc: "ReportDocument") -> Dict[str, float]:
    """Compute weighted-average confidence per section, as a percentage (0.0–100.0).

    Scores every Claim in each section: HIGH=1.0, MEDIUM=0.66, LOW=0.33, UNKNOWN=0.0.
    Sections with no Claims are omitted from the result dict.

    This is the authoritative computation — renderers read from
    run_metadata.section_confidences rather than recomputing.
    """
    sections = {
        "research":    doc.research,
        "financial":   doc.financial,
        "risk":        doc.risk,
        "social_media": doc.social_media,
        "synthesis":   doc.synthesis,
    }
    result: Dict[str, float] = {}
    for name, section in sections.items():
        score = _section_confidence_score(section)
        if score is not None:
            result[name] = round(score * 100, 2)
    return result


def compute_overall_confidence(section_confidences: Dict[str, float]) -> Optional[float]:
    """Compute the overall weighted confidence across Financial (40%), Risk (40%), Social Media (20%).

    Returns None if none of the weighted sections are present. Result is in the
    same percentage scale (0.0–100.0) as section_confidences.
    """
    weighted_sum, total_weight = 0.0, 0.0
    for section_name, weight in _SECTION_WEIGHTS.items():
        pct = section_confidences.get(section_name)
        if pct is not None:
            weighted_sum += pct * weight
            total_weight += weight
    if total_weight == 0.0:
        return None
    return round(weighted_sum / total_weight, 2)


# ── Tier coverage ─────────────────────────────────────────────────────────────

def _iter_section_claims(*sections):
    """Yield every Claim from a sequence of optional section Pydantic models."""
    for section in sections:
        if section is None:
            continue
        for field_name in type(section).model_fields:
            val = getattr(section, field_name)
            if isinstance(val, Claim):
                yield val
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, Claim):
                        yield item


def _compute_tier_coverage(*sections) -> Tuple[Dict[str, float], Dict[str, int]]:
    """Return (tier_coverage, tier_attempts) across all provided sections.

    tier_attempts: raw source-URL count per tier name.
    tier_coverage: fraction of total source URLs per tier (0.0–1.0).
    """
    totals: Dict[str, int] = {}
    for claim in _iter_section_claims(*sections):
        for ref in claim.sources:
            name = ref.tier.value
            totals[name] = totals.get(name, 0) + 1
    total = sum(totals.values())
    coverage = {t: round(c / total, 3) for t, c in totals.items()} if total > 0 else {}
    return coverage, totals


# ── Section assemblers ────────────────────────────────────────────────────────

def _assemble_research(data: Optional[dict]) -> Optional[ReportResearch]:
    if not data:
        return None
    a = "research"
    data = annotate_claim_ids(data)
    fm = _build_field_map(data)
    return ReportResearch(
        description=_dp_to_claim(data.get("description"), a, "description", fm),
        founded_year=_dp_to_claim(data.get("founded_year"), a, "founded_year", fm),
        headquarters=_dp_to_claim(data.get("headquarters"), a, "headquarters", fm),
        employee_count=_dp_to_claim(data.get("employee_count"), a, "employee_count", fm),
        industry=_dp_to_claim(data.get("industry"), a, "industry", fm),
        website=_dp_to_claim(data.get("website"), a, "website", fm),
        key_products=_dp_list_to_claims(data.get("key_products", []), a, "key_products", fm),
        key_leadership=_dp_list_to_claims(data.get("key_leadership", []), a, "key_leadership", fm),
        technology_stack=_dp_list_to_claims(data.get("technology_stack", []), a, "technology_stack", fm),
        recent_developments=_dp_list_to_claims(data.get("recent_developments", []), a, "recent_developments", fm),
        patent_count=_dp_to_claim(data.get("patent_count"), a, "patent_count", fm),
        notable_patents=_dp_list_to_claims(data.get("notable_patents", []), a, "notable_patents", fm),
    )


def _assemble_financial(data: Optional[dict]) -> Optional[ReportFinancial]:
    if not data:
        return None
    a = "financial"
    data = annotate_claim_ids(data)
    fm = _build_field_map(data)
    return ReportFinancial(
        revenue=_dp_to_claim(data.get("revenue"), a, "revenue", fm),
        revenue_growth=_dp_to_claim(data.get("revenue_growth"), a, "revenue_growth", fm),
        profitability=_dp_to_claim(data.get("profitability"), a, "profitability", fm),
        total_funding=_dp_to_claim(data.get("total_funding"), a, "total_funding", fm),
        last_funding_round=_dp_to_claim(data.get("last_funding_round"), a, "last_funding_round", fm),
        valuation=_dp_to_claim(data.get("valuation"), a, "valuation", fm),
        revenue_model=_dp_to_claim(data.get("revenue_model"), a, "revenue_model", fm),
        key_investors=_dp_list_to_claims(data.get("key_investors", []), a, "key_investors", fm),
        key_customers=_dp_list_to_claims(data.get("key_customers", []), a, "key_customers", fm),
        financial_risks=_dp_list_to_claims(data.get("financial_risks", []), a, "financial_risks", fm),
        recent_financial_events=_dp_list_to_claims(data.get("recent_financial_events", []), a, "recent_financial_events", fm),
    )


def _assemble_risk(data: Optional[dict]) -> Optional[ReportRisk]:
    if not data:
        return None
    a = "risk"
    data = annotate_claim_ids(data)
    fm = _build_field_map(data)
    return ReportRisk(
        overall_risk_rating=_dp_to_claim(data.get("overall_risk_rating"), a, "overall_risk_rating", fm),
        risk_summary=_dp_to_claim(data.get("risk_summary"), a, "risk_summary", fm),
        regulatory_risks=_dp_list_to_claims(data.get("regulatory_risks", []), a, "regulatory_risks", fm),
        legal_risks=_dp_list_to_claims(data.get("legal_risks", []), a, "legal_risks", fm),
        cybersecurity_risks=_dp_list_to_claims(data.get("cybersecurity_risks", []), a, "cybersecurity_risks", fm),
        operational_risks=_dp_list_to_claims(data.get("operational_risks", []), a, "operational_risks", fm),
        reputational_risks=_dp_list_to_claims(data.get("reputational_risks", []), a, "reputational_risks", fm),
        esg_risks=_dp_list_to_claims(data.get("esg_risks", []), a, "esg_risks", fm),
        pending_litigation=_dp_list_to_claims(data.get("pending_litigation", []), a, "pending_litigation", fm),
        government_contract_exposure=_dp_to_claim(data.get("government_contract_exposure"), a, "government_contract_exposure", fm),
        notable_federal_contracts=_dp_list_to_claims(data.get("notable_federal_contracts", []), a, "notable_federal_contracts", fm),
    )


def _assemble_social_media(data: Optional[dict]) -> Optional[ReportSocialMedia]:
    if not data:
        return None
    a = "social_media"
    data = annotate_claim_ids(data)
    fm = _build_field_map(data)
    return ReportSocialMedia(
        overall_sentiment=_dp_to_claim(data.get("overall_sentiment"), a, "overall_sentiment", fm),
        sentiment_summary=_dp_to_claim(data.get("sentiment_summary"), a, "sentiment_summary", fm),
        twitter_presence=_dp_to_claim(data.get("twitter_presence"), a, "twitter_presence", fm),
        linkedin_presence=_dp_to_claim(data.get("linkedin_presence"), a, "linkedin_presence", fm),
        reddit_sentiment=_dp_to_claim(data.get("reddit_sentiment"), a, "reddit_sentiment", fm),
        glassdoor_rating=_dp_to_claim(data.get("glassdoor_rating"), a, "glassdoor_rating", fm),
        notable_mentions=_dp_list_to_claims(data.get("notable_mentions", []), a, "notable_mentions", fm),
        trending_topics=_dp_list_to_claims(data.get("trending_topics", []), a, "trending_topics", fm),
        customer_complaints=_dp_list_to_claims(data.get("customer_complaints", []), a, "customer_complaints", fm),
        positive_signals=_dp_list_to_claims(data.get("positive_signals", []), a, "positive_signals", fm),
    )


def _assemble_synthesis(data: Optional[dict]) -> Optional[ReportSynthesis]:
    if not data:
        return None
    a = "synthesis"
    return ReportSynthesis(
        # investment_recommendation in agent output → recommendation in canonical doc
        recommendation=_dp_to_claim(data.get("investment_recommendation"), a, "recommendation"),
        recommendation_rationale=_dp_to_claim(data.get("recommendation_rationale"), a, "recommendation_rationale"),
        executive_summary=_dp_to_claim(data.get("executive_summary"), a, "executive_summary"),
        key_strengths=_dp_list_to_claims(data.get("key_strengths", []), a, "key_strengths"),
        key_concerns=_dp_list_to_claims(data.get("key_concerns", []), a, "key_concerns"),
        red_flags=_dp_list_to_claims(data.get("red_flags", []), a, "red_flags"),
        data_conflicts=_dp_list_to_claims(data.get("data_conflicts", []), a, "data_conflicts"),
        follow_up_questions=_dp_list_to_claims(data.get("follow_up_questions", []), a, "follow_up_questions"),
        data_quality=_dp_to_claim(data.get("data_quality"), a, "data_quality"),
    )


def _build_run_metadata(
    trace_summary: dict,
    tier_coverage: Optional[Dict[str, float]] = None,
    tier_attempts: Optional[Dict[str, int]] = None,
    edgar_lookup_status: Optional[str] = None,
    edgar_cik: Optional[str] = None,
    section_confidences: Optional[Dict[str, float]] = None,
    overall_confidence: Optional[float] = None,
) -> RunMetadata:
    agents = {}
    for name, data in (trace_summary.get("by_agent") or {}).items():
        agents[name] = AgentRunMetadata(
            llm_calls=data.get("llm_calls", 0),
            tool_calls=data.get("tool_calls", 0),
            input_tokens=data.get("input_tokens", 0),
            output_tokens=data.get("output_tokens", 0),
            cost_usd=data.get("cost_usd", 0.0),
            duration_ms=data.get("duration_ms", 0.0),
            errors=data.get("errors", 0),
        )
    return RunMetadata(
        trace_id=trace_summary.get("trace_id", ""),
        cost_usd=trace_summary.get("total_cost_usd", 0.0),
        duration_ms=trace_summary.get("total_duration_ms", 0.0),
        total_llm_calls=trace_summary.get("total_llm_calls", 0),
        total_tool_calls=trace_summary.get("total_tool_calls", 0),
        total_input_tokens=trace_summary.get("total_input_tokens", 0),
        total_output_tokens=trace_summary.get("total_output_tokens", 0),
        agents=agents,
        tier_coverage=tier_coverage or {},
        tier_attempts=tier_attempts or {},
        edgar_lookup_status=edgar_lookup_status,
        edgar_cik=edgar_cik,
        section_confidences=section_confidences or {},
        overall_confidence=overall_confidence,
    )


# ── Synthesis provenance validation ──────────────────────────────────────────

def _collect_upstream_claim_ids(doc: "ReportDocument") -> set[str]:
    """Return the set of claim_ids from all non-synthesis sections."""
    ids: set[str] = set()
    for section in (doc.research, doc.financial, doc.risk, doc.social_media):
        if section is None:
            continue
        for field_name in type(section).model_fields:
            val = getattr(section, field_name)
            if isinstance(val, Claim):
                ids.add(val.claim_id)
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, Claim):
                        ids.add(item.claim_id)
    return ids


def _validate_synthesized_from(doc: "ReportDocument") -> None:
    """Strip dangling synthesized_from references and warn; never crash the pipeline.

    A dangling reference means the synthesis agent cited a claim_id that doesn't
    exist in the assembled document — most often a hallucinated ID. Stripping it
    preserves the document and the claim; the remaining reasoning field carries
    the provenance chain for GENERAL synthesis fields.

    For SPECIFIC synthesis fields (key_strengths, key_concerns, red_flags,
    data_conflicts) that are left with an empty synthesized_from after stripping,
    an additional warning is emitted — these fields are supposed to be directly
    traceable to upstream claims.
    """
    if doc.synthesis is None:
        return
    upstream_ids = _collect_upstream_claim_ids(doc)
    for field_name in type(doc.synthesis).model_fields:
        val = getattr(doc.synthesis, field_name)
        claims: List[Claim] = []
        if isinstance(val, Claim):
            claims = [val]
        elif isinstance(val, list):
            claims = [c for c in val if isinstance(c, Claim)]
        for claim in claims:
            dangling = [r for r in claim.synthesized_from if r not in upstream_ids]
            if dangling:
                valid = [r for r in claim.synthesized_from if r in upstream_ids]
                print(
                    f"[assembler] WARNING: synthesis claim '{claim.field_name}' "
                    f"(claim_id={claim.claim_id}) had {len(dangling)} dangling "
                    f"synthesized_from reference(s) stripped: {dangling}",
                    file=sys.stderr,
                )
                claim.synthesized_from = valid
                base_field = claim.field_name.split("[")[0]
                if base_field in SPECIFIC_SYNTHESIS_FIELDS and not valid:
                    print(
                        f"[assembler] WARNING: specific synthesis field '{claim.field_name}' "
                        f"now has empty synthesized_from after stripping — claim is kept but "
                        f"has no traceable upstream provenance.",
                        file=sys.stderr,
                    )


def _collect_all_claim_ids(doc: "ReportDocument") -> set[str]:
    """Return the set of claim_ids from all sections including synthesis."""
    ids: set[str] = set()
    for section in (doc.research, doc.financial, doc.risk, doc.social_media, doc.synthesis):
        if section is None:
            continue
        for field_name in type(section).model_fields:
            val = getattr(section, field_name)
            if isinstance(val, Claim):
                ids.add(val.claim_id)
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, Claim):
                        ids.add(item.claim_id)
    return ids


def _validate_derived_from(doc: "ReportDocument") -> None:
    """Raise ValueError if any derived_from entry references a non-existent claim_id.

    After field-path resolution in _dp_to_claim, all derived_from entries that used
    valid field names (e.g. "revenue", "recent_financial_events[0]") have been
    replaced with real claim_ids. A dangling reference here means the agent cited a
    field path that does not exist in its own output — a hard error, not a warning.

    This mirrors _validate_synthesized_from: both provenance layers enforce the same
    invariant (cited IDs must exist in the document) at the same hard-failure level.
    """
    all_ids = _collect_all_claim_ids(doc)
    for section in (doc.research, doc.financial, doc.risk, doc.social_media, doc.synthesis):
        if section is None:
            continue
        for field_name in type(section).model_fields:
            val = getattr(section, field_name)
            claims: List[Claim] = []
            if isinstance(val, Claim):
                claims = [val]
            elif isinstance(val, list):
                claims = [c for c in val if isinstance(c, Claim)]
            for claim in claims:
                for ref_id in claim.derived_from:
                    if ref_id not in all_ids:
                        raise ValueError(
                            f"[derived_from] Claim '{claim.field_name}' "
                            f"(agent={claim.agent}) references unknown claim_id "
                            f"'{ref_id}'. Use the field name of the atomic input "
                            f"claim in derived_from (e.g. \"revenue\", "
                            f"\"recent_financial_events[0]\") so the assembler can "
                            f"resolve it to the correct claim_id."
                        )


# ── Public API ────────────────────────────────────────────────────────────────

def assemble_report(
    research_data: Optional[dict],
    financial_data: Optional[dict],
    risk_data: Optional[dict],
    social_media_data: Optional[dict],
    synthesis_data: Optional[dict],
    trace_summary: dict,
    edgar_data: Optional[dict] = None,
) -> ReportDocument:
    """Assemble all agent outputs into a canonical, validated ReportDocument.

    This is the only place where raw agent dicts become the structured,
    tier-annotated canonical report. Gaps (confidence=unknown fields) are
    collected from all four specialist agents and surfaced in doc.gaps.
    EDGAR data (when present) is merged into financial and risk sections.
    """
    company_name = (
        (research_data or {}).get("company_name")
        or (financial_data or {}).get("company_name")
        or (synthesis_data or {}).get("company_name")
        or "Unknown Company"
    )

    gaps: List[GapRecord] = []
    for data, agent_name in (
        (research_data,    "research"),
        (financial_data,   "financial"),
        (risk_data,        "risk"),
        (social_media_data, "social_media"),
    ):
        gaps.extend(_collect_gaps(data, agent_name))

    # Assemble sections, then apply EDGAR overlays before tier coverage
    research = _assemble_research(research_data)
    financial = _assemble_financial(financial_data)
    risk = _assemble_risk(risk_data)
    social_media = _assemble_social_media(social_media_data)
    synthesis = _assemble_synthesis(synthesis_data)

    if edgar_data:
        financial = _merge_edgar(financial, edgar_data)
        risk, edgar_risk_gaps = _merge_edgar_into_risk(risk, edgar_data)
        gaps.extend(edgar_risk_gaps)

    # Reconcile gaps with the final assembled state. Must run after all merges
    # so that fields populated by EDGAR (or any future overlay) are not
    # simultaneously reported as information gaps.
    gaps = _prune_gaps(gaps, research, financial, risk, social_media, synthesis)

    # Tier coverage — computed before caps (source URLs never change during capping).
    tier_coverage, tier_attempts = _compute_tier_coverage(
        research, financial, risk, social_media, synthesis
    )
    edgar_status = (edgar_data or {}).get("edgar_lookup_status")
    edgar_cik = (edgar_data or {}).get("cik")

    # Apply credibility caps (Cap 1a, Cap 1b, Cap 2).  Must run after all merges
    # and before section_confidence computation so that aggregates reflect post-cap
    # claim confidences, not the agent-declared pre-cap values.
    research, financial, risk, social_media, synthesis = _apply_credibility_caps(
        research, financial, risk, social_media, synthesis, tier_coverage
    )

    # Build a temporary doc to compute section/overall confidence from capped sections.
    # Section confidence is computed here (assembly time) so it's persisted in the
    # canonical JSON — renderers read from run_metadata rather than recomputing.
    _tmp_doc = ReportDocument(
        report_id=trace_summary.get("trace_id", uuid.uuid4().hex[:12]),
        company_name=company_name,
        generated_at=datetime.now(timezone.utc),
        run_metadata=_build_run_metadata(trace_summary),
        synthesis=synthesis,
        research=research,
        financial=financial,
        risk=risk,
        social_media=social_media,
        gaps=gaps,
    )
    section_confidences = compute_section_confidences(_tmp_doc)
    overall_confidence = compute_overall_confidence(section_confidences)

    doc = _tmp_doc.model_copy(update={
        "run_metadata": _build_run_metadata(
            trace_summary,
            tier_coverage=tier_coverage,
            tier_attempts=tier_attempts,
            edgar_lookup_status=edgar_status,
            edgar_cik=edgar_cik,
            section_confidences=section_confidences,
            overall_confidence=overall_confidence,
        )
    })
    _validate_synthesized_from(doc)
    _validate_derived_from(doc)
    return doc


# ── Backward-compat bridge for shim renderers ─────────────────────────────────

def claim_as_dp(claim: Optional[Claim]) -> dict:
    """Convert a Claim to the dict shape expected by legacy render helpers.

    Sources are flattened from SourceRef objects back to plain URL strings
    so the existing rendering code (which expects List[str]) works unchanged.
    """
    if claim is None:
        return {"value": "unknown", "confidence": "unknown", "sources": [], "severity": None, "reasoning": None}
    value = f"{claim.value} *(derived)*" if claim.derived else claim.value
    return {
        "value": value,
        "confidence": claim.confidence.value,
        "severity": claim.severity.value if claim.severity else None,
        "sources": [s.url for s in claim.sources],
        "reasoning": claim.reasoning,
        "derived": claim.derived,
    }


def _claims_as_dps(claims: List[Claim]) -> List[dict]:
    return [claim_as_dp(c) for c in claims]


def build_render_dicts(doc: ReportDocument) -> tuple:
    """Reconstruct legacy agent-dict shapes from a ReportDocument.

    Returns (research_data, financial_data, risk_data, social_media_data,
             synthesis_data, trace_summary) — the six arguments the old
             dict-based renderer signatures expected.

    Used only by the shim render functions; not part of the normal pipeline.
    """
    r = doc.research
    research_data = None if r is None else {
        "company_name": doc.company_name,
        "description":         claim_as_dp(r.description),
        "founded_year":        claim_as_dp(r.founded_year),
        "headquarters":        claim_as_dp(r.headquarters),
        "employee_count":      claim_as_dp(r.employee_count),
        "industry":            claim_as_dp(r.industry),
        "website":             claim_as_dp(r.website),
        "key_products":        _claims_as_dps(r.key_products),
        "key_leadership":      _claims_as_dps(r.key_leadership),
        "technology_stack":    _claims_as_dps(r.technology_stack),
        "recent_developments": _claims_as_dps(r.recent_developments),
        "patent_count":        claim_as_dp(r.patent_count),
        "notable_patents":     _claims_as_dps(r.notable_patents),
    }

    f = doc.financial
    financial_data = None if f is None else {
        "company_name": doc.company_name,
        "revenue":               claim_as_dp(f.revenue),
        "revenue_growth":        claim_as_dp(f.revenue_growth),
        "profitability":         claim_as_dp(f.profitability),
        "total_funding":         claim_as_dp(f.total_funding),
        "last_funding_round":    claim_as_dp(f.last_funding_round),
        "valuation":             claim_as_dp(f.valuation),
        "revenue_model":         claim_as_dp(f.revenue_model),
        "key_investors":         _claims_as_dps(f.key_investors),
        "key_customers":         _claims_as_dps(f.key_customers),
        "financial_risks":       _claims_as_dps(f.financial_risks),
        "recent_financial_events": _claims_as_dps(f.recent_financial_events),
    }

    ri = doc.risk
    risk_data = None if ri is None else {
        "company_name": doc.company_name,
        "overall_risk_rating":          claim_as_dp(ri.overall_risk_rating),
        "risk_summary":                 claim_as_dp(ri.risk_summary),
        "regulatory_risks":             _claims_as_dps(ri.regulatory_risks),
        "legal_risks":                  _claims_as_dps(ri.legal_risks),
        "cybersecurity_risks":          _claims_as_dps(ri.cybersecurity_risks),
        "operational_risks":            _claims_as_dps(ri.operational_risks),
        "reputational_risks":           _claims_as_dps(ri.reputational_risks),
        "esg_risks":                    _claims_as_dps(ri.esg_risks),
        "pending_litigation":           _claims_as_dps(ri.pending_litigation),
        "government_contract_exposure": claim_as_dp(ri.government_contract_exposure),
        "notable_federal_contracts":    _claims_as_dps(ri.notable_federal_contracts),
    }

    sm = doc.social_media
    social_media_data = None if sm is None else {
        "company_name": doc.company_name,
        "overall_sentiment":  claim_as_dp(sm.overall_sentiment),
        "sentiment_summary":  claim_as_dp(sm.sentiment_summary),
        "twitter_presence":   claim_as_dp(sm.twitter_presence),
        "linkedin_presence":  claim_as_dp(sm.linkedin_presence),
        "reddit_sentiment":   claim_as_dp(sm.reddit_sentiment),
        "glassdoor_rating":   claim_as_dp(sm.glassdoor_rating),
        "notable_mentions":   _claims_as_dps(sm.notable_mentions),
        "trending_topics":    _claims_as_dps(sm.trending_topics),
        "customer_complaints": _claims_as_dps(sm.customer_complaints),
        "positive_signals":   _claims_as_dps(sm.positive_signals),
    }

    sy = doc.synthesis
    synthesis_data = None if sy is None else {
        "company_name": doc.company_name,
        "executive_summary":        claim_as_dp(sy.executive_summary),
        "investment_recommendation": claim_as_dp(sy.recommendation),
        "recommendation_rationale": claim_as_dp(sy.recommendation_rationale),
        "key_strengths":    _claims_as_dps(sy.key_strengths),
        "key_concerns":     _claims_as_dps(sy.key_concerns),
        "red_flags":        _claims_as_dps(sy.red_flags),
        "data_conflicts":   _claims_as_dps(sy.data_conflicts),
        "follow_up_questions": _claims_as_dps(sy.follow_up_questions),
        "data_quality":     claim_as_dp(sy.data_quality),
    }

    m = doc.run_metadata
    trace_summary = {
        "trace_id":            m.trace_id,
        "total_cost_usd":      m.cost_usd,
        "total_duration_ms":   m.duration_ms,
        "total_llm_calls":     m.total_llm_calls,
        "total_tool_calls":    m.total_tool_calls,
        "total_input_tokens":  m.total_input_tokens,
        "total_output_tokens": m.total_output_tokens,
        "errors": [],
        "by_agent": {
            name: {
                "llm_calls":     a.llm_calls,
                "tool_calls":    a.tool_calls,
                "input_tokens":  a.input_tokens,
                "output_tokens": a.output_tokens,
                "cost_usd":      a.cost_usd,
                "duration_ms":   a.duration_ms,
                "errors":        a.errors,
            }
            for name, a in m.agents.items()
        },
    }

    return research_data, financial_data, risk_data, social_media_data, synthesis_data, trace_summary
