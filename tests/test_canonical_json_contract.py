"""
Contract tests for the canonical JSON report format.

Loads tests/fixtures/sample_report.json and validates that it conforms to
the ReportDocument schema and carries expected Stripe data. If these tests
break, either the fixture is out of date or a breaking schema change was made
without bumping SCHEMA_VERSION.
"""

import json
from pathlib import Path

import pytest

from src.schemas.models import (
    ConfidenceLevel,
    ReportDocument,
    SCHEMA_VERSION,
    SourceTier,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sample_report.json"


@pytest.fixture(scope="module")
def doc() -> ReportDocument:
    raw = json.loads(FIXTURE.read_text())
    return ReportDocument.model_validate(raw)


# ── Fixture loads and validates ───────────────────────────────────────────────

def test_fixture_file_exists():
    assert FIXTURE.exists(), f"Fixture not found: {FIXTURE}"


def test_fixture_validates_against_model(doc):
    assert isinstance(doc, ReportDocument)


# ── Top-level fields ──────────────────────────────────────────────────────────

def test_schema_version(doc):
    assert doc.schema_version == SCHEMA_VERSION


def test_company_name(doc):
    assert doc.company_name == "Stripe"


def test_report_id_present(doc):
    assert doc.report_id
    assert len(doc.report_id) > 0


def test_generated_at_present(doc):
    assert doc.generated_at is not None


# ── Run metadata ──────────────────────────────────────────────────────────────

def test_run_metadata_trace_id(doc):
    assert doc.run_metadata.trace_id == "abc123def456"


def test_run_metadata_cost(doc):
    assert doc.run_metadata.cost_usd == pytest.approx(0.45)


def test_run_metadata_by_agent(doc):
    assert "research" in doc.run_metadata.agents
    assert "financial" in doc.run_metadata.agents


def test_run_metadata_section_confidences_populated(doc):
    assert doc.run_metadata.section_confidences, "section_confidences must be non-empty"
    assert "financial" in doc.run_metadata.section_confidences
    assert 0.0 < doc.run_metadata.section_confidences["financial"] <= 100.0


def test_run_metadata_overall_confidence_populated(doc):
    assert doc.run_metadata.overall_confidence is not None
    assert 0.0 < doc.run_metadata.overall_confidence <= 100.0


# ── Research section ──────────────────────────────────────────────────────────

def test_research_description(doc):
    assert doc.research is not None
    assert doc.research.description is not None
    assert "Stripe" in doc.research.description.value
    assert doc.research.description.confidence == ConfidenceLevel.HIGH
    assert doc.research.description.agent == "research"


def test_research_founded_year(doc):
    assert doc.research.founded_year is not None
    assert "2010" in doc.research.founded_year.value


def test_research_claim_ids_present(doc):
    assert doc.research.description.claim_id
    assert doc.research.founded_year.claim_id
    assert doc.research.description.claim_id != doc.research.founded_year.claim_id


def test_research_field_names(doc):
    assert doc.research.description.field_name == "description"
    assert doc.research.founded_year.field_name == "founded_year"


def test_research_leadership(doc):
    assert len(doc.research.key_leadership) >= 2
    names = [c.value for c in doc.research.key_leadership]
    assert any("Patrick Collison" in n for n in names)
    assert any("John Collison" in n for n in names)


def test_research_sources_typed(doc):
    # founded_year has a crunchbase source — verify it's typed as aggregator
    founders_sources = doc.research.founded_year.sources
    if founders_sources:
        assert founders_sources[0].tier == SourceTier.AGGREGATOR


# ── Financial section ─────────────────────────────────────────────────────────

def test_financial_total_funding(doc):
    assert doc.financial is not None
    assert doc.financial.total_funding is not None
    assert "$8.7B" in doc.financial.total_funding.value


def test_financial_last_funding_round_confidence(doc):
    assert doc.financial.last_funding_round is not None
    assert doc.financial.last_funding_round.confidence == ConfidenceLevel.HIGH


def test_financial_revenue_confidence_is_low(doc):
    # Private company revenue is always estimated
    assert doc.financial.revenue is not None
    assert doc.financial.revenue.confidence == ConfidenceLevel.LOW


def test_financial_sources_include_tier(doc):
    # revenue has a reuters source
    rev_sources = doc.financial.revenue.sources
    if rev_sources:
        assert rev_sources[0].tier == SourceTier.REPUTABLE_SECONDARY


# ── Synthesis section ─────────────────────────────────────────────────────────

def test_synthesis_recommendation_present(doc):
    assert doc.synthesis is not None
    assert doc.synthesis.recommendation is not None
    assert doc.synthesis.recommendation.value == "proceed_with_conditions"


def test_synthesis_executive_summary_present(doc):
    assert doc.synthesis.executive_summary is not None
    assert len(doc.synthesis.executive_summary.value) > 20


def test_synthesis_key_strengths(doc):
    assert len(doc.synthesis.key_strengths) >= 1


# ── Gaps ──────────────────────────────────────────────────────────────────────

def test_gaps_present(doc):
    assert len(doc.gaps) >= 1


def test_gaps_have_required_fields(doc):
    for gap in doc.gaps:
        assert gap.field
        assert gap.agent
        assert gap.reason


# ── Optional sections absent ──────────────────────────────────────────────────

def test_risk_section_is_none(doc):
    assert doc.risk is None


def test_social_media_section_is_none(doc):
    assert doc.social_media is None
