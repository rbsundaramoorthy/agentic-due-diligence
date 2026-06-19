"""
Structured output schemas for the due diligence system.

Design philosophy: Every data point carries its value, confidence, and provenance.
This mirrors the entity resolution pattern from ERaaS at Hartford — when you're making
decisions based on data, you need to know how trustworthy each data point is.

Two distinct schema layers:

  Agent output layer   — DataPoint, CompanyResearch, CompanyFinancials, etc.
                         These are what agents produce. sources: List[str] (URLs).

  Canonical report layer — Claim, ReportDocument, and section models.
                           These are the source of truth for all renderers.
                           sources: List[SourceRef] with tier annotations.
"""

from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional
from pydantic import BaseModel, Field, model_validator

# Bump this when a field is added or removed from ReportDocument.
# Policy: MAJOR for breaking changes (removed/renamed fields),
#         MINOR for additive fields with no default (consumers must handle absence),
#         PATCH for additive fields with a default/None (fully backward compatible).
# Regenerate schema/report.schema.json after any change:
#   python -c "import json; from src.schemas.models import ReportDocument; \
#              print(json.dumps(ReportDocument.model_json_schema(), indent=2))" \
#   > schema/report.schema.json
SCHEMA_VERSION = "1.0.7"


# ── Agent output layer ────────────────────────────────────────────────────────

class ConfidenceLevel(str, Enum):
    """How much we trust this data point.

    In entity resolution systems, every resolved field carried a confidence score.
    LLM-extracted data needs the same discipline — maybe more so, because
    LLMs can hallucinate with high confidence.
    """
    HIGH = "high"        # Multiple reliable sources confirm
    MEDIUM = "medium"    # One reliable source or multiple weak sources
    LOW = "low"          # Estimated or inferred, not directly confirmed
    UNKNOWN = "unknown"  # Could not determine


class SeverityLevel(str, Enum):
    """How severe or impactful a risk is.

    Separate from confidence (data reliability). A risk can be HIGH severity
    with LOW confidence (e.g., unconfirmed but potentially catastrophic).
    """
    CRITICAL = "critical"  # Existential threat, immediate action needed
    HIGH = "high"          # Major impact, needs attention
    MEDIUM = "medium"      # Notable but manageable
    LOW = "low"            # Minor, limited impact


class EdgarLookupStatus(str, Enum):
    """Result of the EdgarAgent's company lookup attempt."""
    SUCCEEDED = "succeeded"            # Company found and financials extracted
    NOT_SEC_REPORTING = "not_sec_reporting"  # Private or non-US company (informational)
    LOOKUP_FAILED = "lookup_failed"    # API or parsing error (actionable)
    RATE_LIMITED = "rate_limited"      # EDGAR rate limit hit (actionable)


class DataPoint(BaseModel):
    """A single piece of information with provenance and confidence.

    This is the atomic unit of the agent output layer. Every fact the agents
    extract is wrapped in a DataPoint so the Synthesis Agent (and the
    human reading the report) knows exactly how much to trust it.

    sources is a list of raw URLs at this layer. The canonical report layer
    wraps these into SourceRef objects with tier annotations.

    synthesized_from: populated only by the synthesis agent — a list of
    upstream claim_ids (pre-assigned before synthesis runs) that this claim
    draws from. Empty for all upstream agent DataPoints.
    """
    value: str
    confidence: ConfidenceLevel
    sources: List[str] = Field(default_factory=list)
    reasoning: Optional[str] = None
    severity: Optional[SeverityLevel] = None
    synthesized_from: List[str] = Field(default_factory=list)
    derived: bool = False
    derived_from: List[str] = Field(default_factory=list)


class CompanyResearch(BaseModel):
    """Output schema for the Research Agent."""
    company_name: str
    description: DataPoint
    founded_year: DataPoint
    headquarters: DataPoint
    employee_count: DataPoint
    industry: DataPoint
    key_products: List[DataPoint] = Field(default_factory=list)
    key_leadership: List[DataPoint] = Field(default_factory=list)
    technology_stack: List[DataPoint] = Field(default_factory=list)
    recent_developments: List[DataPoint] = Field(default_factory=list)
    website: DataPoint = DataPoint(
        value="unknown", confidence=ConfidenceLevel.UNKNOWN
    )
    # P3b: USPTO patent data (optional — set by uspto_patent_search tool)
    patent_count: Optional[DataPoint] = None
    notable_patents: List[DataPoint] = Field(default_factory=list)


class CompanyFinancials(BaseModel):
    """Output schema for the Financial Agent."""
    company_name: str
    revenue: DataPoint = DataPoint(value="unknown", confidence=ConfidenceLevel.UNKNOWN)
    revenue_growth: DataPoint = DataPoint(value="unknown", confidence=ConfidenceLevel.UNKNOWN)
    profitability: DataPoint = DataPoint(value="unknown", confidence=ConfidenceLevel.UNKNOWN)
    total_funding: DataPoint = DataPoint(value="unknown", confidence=ConfidenceLevel.UNKNOWN)
    last_funding_round: DataPoint = DataPoint(value="unknown", confidence=ConfidenceLevel.UNKNOWN)
    valuation: DataPoint = DataPoint(value="unknown", confidence=ConfidenceLevel.UNKNOWN)
    key_investors: List[DataPoint] = Field(default_factory=list)
    revenue_model: DataPoint = DataPoint(value="unknown", confidence=ConfidenceLevel.UNKNOWN)
    key_customers: List[DataPoint] = Field(default_factory=list)
    financial_risks: List[DataPoint] = Field(default_factory=list)
    recent_financial_events: List[DataPoint] = Field(default_factory=list)


class CompanyRisks(BaseModel):
    """Output schema for the Risk Agent."""
    company_name: str
    overall_risk_rating: DataPoint = DataPoint(value="unknown", confidence=ConfidenceLevel.UNKNOWN)
    risk_summary: DataPoint = DataPoint(value="unknown", confidence=ConfidenceLevel.UNKNOWN)
    regulatory_risks: List[DataPoint] = Field(default_factory=list)
    legal_risks: List[DataPoint] = Field(default_factory=list)
    cybersecurity_risks: List[DataPoint] = Field(default_factory=list)
    operational_risks: List[DataPoint] = Field(default_factory=list)
    reputational_risks: List[DataPoint] = Field(default_factory=list)
    esg_risks: List[DataPoint] = Field(default_factory=list)
    pending_litigation: List[DataPoint] = Field(default_factory=list)
    # P3b: SAM.gov federal contract data (optional — set by samgov_contract_search tool)
    government_contract_exposure: Optional[DataPoint] = None
    notable_federal_contracts: List[DataPoint] = Field(default_factory=list)


class CompanySocialMedia(BaseModel):
    """Output schema for the Social Media Agent."""
    company_name: str
    overall_sentiment: DataPoint = DataPoint(value="unknown", confidence=ConfidenceLevel.UNKNOWN)
    sentiment_summary: DataPoint = DataPoint(value="unknown", confidence=ConfidenceLevel.UNKNOWN)
    twitter_presence: DataPoint = DataPoint(value="unknown", confidence=ConfidenceLevel.UNKNOWN)
    linkedin_presence: DataPoint = DataPoint(value="unknown", confidence=ConfidenceLevel.UNKNOWN)
    reddit_sentiment: DataPoint = DataPoint(value="unknown", confidence=ConfidenceLevel.UNKNOWN)
    glassdoor_rating: DataPoint = DataPoint(value="unknown", confidence=ConfidenceLevel.UNKNOWN)
    notable_mentions: List[DataPoint] = Field(default_factory=list)
    trending_topics: List[DataPoint] = Field(default_factory=list)
    customer_complaints: List[DataPoint] = Field(default_factory=list)
    positive_signals: List[DataPoint] = Field(default_factory=list)


class CompanySynthesis(BaseModel):
    """Output schema for the Synthesis Agent."""
    company_name: str
    executive_summary: DataPoint
    investment_recommendation: DataPoint  # value: strong_proceed|proceed|proceed_with_conditions|caution|do_not_proceed
    recommendation_rationale: DataPoint
    key_strengths: List[DataPoint] = Field(default_factory=list)
    key_concerns: List[DataPoint] = Field(default_factory=list)
    red_flags: List[DataPoint] = Field(default_factory=list)
    data_conflicts: List[DataPoint] = Field(default_factory=list)
    follow_up_questions: List[DataPoint] = Field(default_factory=list)
    data_quality: DataPoint = DataPoint(value="unknown", confidence=ConfidenceLevel.UNKNOWN)


class CompanyEdgarFinancials(BaseModel):
    """Output schema for the EdgarAgent.

    Fields map to CompanyFinancials for assembler merge — EDGAR values take
    precedence over Financial agent values when is_sec_reporting is True.
    sec_risk_factors are merged into CompanyRisks.regulatory_risks.
    """
    company_name: str
    cik: Optional[str] = None
    is_sec_reporting: bool = False
    edgar_lookup_status: EdgarLookupStatus = EdgarLookupStatus.NOT_SEC_REPORTING
    revenue: DataPoint = DataPoint(value="unknown", confidence=ConfidenceLevel.UNKNOWN)
    revenue_prior_year: Optional[DataPoint] = None
    revenue_growth_pct: Optional[float] = None
    profitability: DataPoint = DataPoint(value="unknown", confidence=ConfidenceLevel.UNKNOWN)
    fiscal_year_end: DataPoint = DataPoint(value="unknown", confidence=ConfidenceLevel.UNKNOWN)
    most_recent_filing: DataPoint = DataPoint(value="unknown", confidence=ConfidenceLevel.UNKNOWN)
    sec_risk_factors: List[DataPoint] = Field(default_factory=list)


# ── Canonical report layer ────────────────────────────────────────────────────
# These models define the source of truth. All renderers (markdown, PDF, API)
# consume ReportDocument, never raw agent dicts.

class SourceTier(str, Enum):
    """Provenance tier for a source URL.

    Mirrors the tiered source model in docs/STRATEGY.md §3 Priority 3.
    P1: tier is inferred from domain patterns by the assembler.
    P3: tier will be recorded at retrieval time by the source registry.
    """
    PRIMARY_DOCUMENT   = "primary_document"    # SEC EDGAR, court records, gov databases
    REPUTABLE_SECONDARY = "reputable_secondary" # Reuters, FT, Bloomberg, major press
    AGGREGATOR         = "aggregator"           # Crunchbase, PitchBook, Owler
    COMMUNITY          = "community"            # Reddit, Twitter/X, Glassdoor, LinkedIn
    UNKNOWN            = "unknown"              # Unclassified; default until P3


class SourceRef(BaseModel):
    """A single source reference with provenance tier.

    retrieved_at is always null in P1 — it is part of the eventual P2/P3 contract
    where passage-level evidence is captured at retrieval time.
    """
    url: str
    tier: SourceTier = SourceTier.UNKNOWN
    retrieved_at: Optional[datetime] = None


# Fields where synthesized_from MUST be non-empty for synthesis claims.
# Directly derived from specific upstream claims; no editorial-judgment exception.
_SPECIFIC_SYNTHESIS_FIELDS: frozenset[str] = frozenset({
    "key_strengths",
    "key_concerns",
    "red_flags",
    "data_conflicts",
})

# Fields where synthesized_from may be empty IF reasoning is non-empty.
# These are editorial judgments over the full findings pattern — no single
# claim chain captures the basis; reasoning carries the explanation instead.
_GENERAL_SYNTHESIS_FIELDS: frozenset[str] = frozenset({
    "executive_summary",
    "recommendation",
    "recommendation_rationale",
    "follow_up_questions",
    "data_quality",
})

_SYNTHESIS_FIELDS: frozenset[str] = _SPECIFIC_SYNTHESIS_FIELDS | _GENERAL_SYNTHESIS_FIELDS


class Claim(BaseModel):
    """A single extracted fact in the canonical report layer.

    Extends DataPoint with: a stable claim_id, the field name it maps to,
    the agent that produced it, and SourceRef objects (typed, tier-annotated)
    instead of bare URL strings.

    synthesized_from: non-empty only on synthesis-agent claims. Each entry is
    a claim_id of an upstream Claim (research/financial/risk/social_media/edgar)
    that this synthesis claim draws from, forming an auditable provenance chain.

    derived: True when the value contains a number or result that the agent
    computed (percentage, growth rate, margin, ratio, sum, average). False when
    the value is taken verbatim from a source. A source stating "85% growth" is
    not derived; the agent computing 85% from two revenue figures is.

    derived_from: non-empty only when derived=True. Each entry is a claim_id of
    an atomic input claim in the same document whose value was used in the
    computation. Enables calibration to distinguish source-grounded claims (wrong
    source) from derived claims (wrong arithmetic) — different failure modes.

    Invariant: derived=True ↔ derived_from is non-empty (enforced by validator).

    Validation rule for synthesis claims:
    - Specific synthesis fields (key_strengths, key_concerns, red_flags,
      data_conflicts, follow_up_questions): synthesized_from MUST be non-empty.
    - General synthesis fields (executive_summary, recommendation,
      recommendation_rationale, data_quality): synthesized_from OR reasoning
      must be non-empty. Both is preferred; neither is a hard error only at
      the Claim level — assembly-level validation (_validate_synthesized_from)
      catches dangling references.
    """
    claim_id: str
    field_name: str
    value: str
    confidence: ConfidenceLevel
    severity: Optional[SeverityLevel] = None
    sources: List[SourceRef] = Field(default_factory=list)
    agent: str
    reasoning: Optional[str] = None
    synthesized_from: List[str] = Field(default_factory=list)
    derived: bool = False
    derived_from: List[str] = Field(default_factory=list)
    unverified_financial: bool = Field(
        default=False,
        description=(
            "Set by Cap 2 in the assembler. True when this is a material financial claim "
            "(revenue, revenue_growth, profitability, valuation, total_funding) whose "
            "sources are entirely community/aggregator/unknown with no reputable or "
            "primary evidence. Confidence is also capped to LOW when this flag is set."
        ),
    )

    @model_validator(mode="after")
    def check_derived_consistency(self) -> "Claim":
        """Enforce derived/derived_from symmetry.

        derived=True requires non-empty derived_from (traceable inputs).
        derived=False with non-empty derived_from is contradictory — either
        the flag is wrong or the field is wrong.
        """
        if self.derived and not self.derived_from:
            raise ValueError(
                f"Claim for field '{self.field_name}' has derived=True but empty "
                f"derived_from. Populate derived_from with the claim_id(s) of the "
                f"atomic input claims used in this computation, or set derived=False "
                f"if the value is directly from a source."
            )
        if not self.derived and self.derived_from:
            raise ValueError(
                f"Claim for field '{self.field_name}' has derived=False but non-empty "
                f"derived_from. Set derived=True if this value was computed from other "
                f"claims, or clear derived_from if it was taken directly from a source."
            )
        return self

    @model_validator(mode="after")
    def check_synthesis_provenance(self) -> "Claim":
        if self.agent != "synthesis":
            return self
        base_field = self.field_name.split("[")[0]
        has_from = bool(self.synthesized_from)
        has_reasoning = bool(self.reasoning and self.reasoning.strip())
        if base_field in _SPECIFIC_SYNTHESIS_FIELDS and not has_from:
            raise ValueError(
                f"Synthesis claim for field '{self.field_name}' requires non-empty "
                f"synthesized_from. Cite upstream claim_id(s) or omit the claim."
            )
        if base_field in _GENERAL_SYNTHESIS_FIELDS and not has_from and not has_reasoning:
            raise ValueError(
                f"Synthesis claim for field '{self.field_name}' requires either "
                f"synthesized_from (with upstream claim_id(s)) or non-empty reasoning. "
                f"Both are empty."
            )
        return self


class GapRecord(BaseModel):
    """A field for which no reliable data was found."""
    field: str
    agent: str
    reason: str


class AgentRunMetadata(BaseModel):
    """Per-agent metrics for one pipeline run."""
    llm_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: float = 0.0
    errors: int = 0


class RunMetadata(BaseModel):
    """Pipeline-level metrics embedded in every canonical report."""
    trace_id: str
    cost_usd: float
    duration_ms: float
    total_llm_calls: int
    total_tool_calls: int
    total_input_tokens: int
    total_output_tokens: int
    agents: Dict[str, AgentRunMetadata] = Field(default_factory=dict)
    # P3: source quality metrics
    tier_coverage: Dict[str, float] = Field(default_factory=dict)
    tier_attempts: Dict[str, int] = Field(default_factory=dict)
    edgar_lookup_status: Optional[str] = None
    edgar_cik: Optional[str] = None
    # Section-level and overall confidence (0.0–100.0 percentage scale)
    section_confidences: Dict[str, float] = Field(default_factory=dict)
    overall_confidence: Optional[float] = None


class ReportResearch(BaseModel):
    """Research section of the canonical report."""
    description: Optional[Claim] = None
    founded_year: Optional[Claim] = None
    headquarters: Optional[Claim] = None
    employee_count: Optional[Claim] = None
    industry: Optional[Claim] = None
    website: Optional[Claim] = None
    key_products: List[Claim] = Field(default_factory=list)
    key_leadership: List[Claim] = Field(default_factory=list)
    technology_stack: List[Claim] = Field(default_factory=list)
    recent_developments: List[Claim] = Field(default_factory=list)
    patent_count: Optional[Claim] = None
    notable_patents: List[Claim] = Field(default_factory=list)


class ReportFinancial(BaseModel):
    """Financial section of the canonical report."""
    revenue: Optional[Claim] = None
    revenue_prior_year: Optional[Claim] = None
    revenue_growth: Optional[Claim] = None
    profitability: Optional[Claim] = None
    total_funding: Optional[Claim] = None
    last_funding_round: Optional[Claim] = None
    valuation: Optional[Claim] = None
    revenue_model: Optional[Claim] = None
    key_investors: List[Claim] = Field(default_factory=list)
    key_customers: List[Claim] = Field(default_factory=list)
    financial_risks: List[Claim] = Field(default_factory=list)
    recent_financial_events: List[Claim] = Field(default_factory=list)


class ReportRisk(BaseModel):
    """Risk section of the canonical report."""
    overall_risk_rating: Optional[Claim] = None
    risk_summary: Optional[Claim] = None
    regulatory_risks: List[Claim] = Field(default_factory=list)
    legal_risks: List[Claim] = Field(default_factory=list)
    cybersecurity_risks: List[Claim] = Field(default_factory=list)
    operational_risks: List[Claim] = Field(default_factory=list)
    reputational_risks: List[Claim] = Field(default_factory=list)
    esg_risks: List[Claim] = Field(default_factory=list)
    pending_litigation: List[Claim] = Field(default_factory=list)
    government_contract_exposure: Optional[Claim] = None
    notable_federal_contracts: List[Claim] = Field(default_factory=list)


class ReportSocialMedia(BaseModel):
    """Social media section of the canonical report."""
    overall_sentiment: Optional[Claim] = None
    sentiment_summary: Optional[Claim] = None
    twitter_presence: Optional[Claim] = None
    linkedin_presence: Optional[Claim] = None
    reddit_sentiment: Optional[Claim] = None
    glassdoor_rating: Optional[Claim] = None
    notable_mentions: List[Claim] = Field(default_factory=list)
    trending_topics: List[Claim] = Field(default_factory=list)
    customer_complaints: List[Claim] = Field(default_factory=list)
    positive_signals: List[Claim] = Field(default_factory=list)


class ReportSynthesis(BaseModel):
    """Synthesis section of the canonical report."""
    recommendation: Optional[Claim] = None
    recommendation_rationale: Optional[Claim] = None
    executive_summary: Optional[Claim] = None
    key_strengths: List[Claim] = Field(default_factory=list)
    key_concerns: List[Claim] = Field(default_factory=list)
    red_flags: List[Claim] = Field(default_factory=list)
    data_conflicts: List[Claim] = Field(default_factory=list)
    follow_up_questions: List[Claim] = Field(default_factory=list)
    data_quality: Optional[Claim] = None


class ReportDocument(BaseModel):
    """Canonical due diligence report — the source of truth for all renderers.

    Every run produces one ReportDocument, written to outputs/report_{slug}.json.
    The markdown, PDF, and HTML files are renders of this document, not edited
    independently.

    schema_version follows the policy in SCHEMA_VERSION above.
    """
    schema_version: str = Field(default=SCHEMA_VERSION, description="Schema version. See SCHEMA_VERSION in models.py for the versioning policy.")
    report_id: str = Field(description="trace_id of the pipeline run that produced this report")
    company_name: str
    generated_at: datetime
    run_metadata: RunMetadata
    synthesis: Optional[ReportSynthesis] = None
    research: Optional[ReportResearch] = None
    financial: Optional[ReportFinancial] = None
    risk: Optional[ReportRisk] = None
    social_media: Optional[ReportSocialMedia] = None
    gaps: List[GapRecord] = Field(default_factory=list)
