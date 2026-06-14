"""
Tests for the Research Agent.

Run with: pytest tests/ -v
"""

import json
import pytest
from src.schemas.models import (
    CompanyResearch,
    DataPoint,
    ConfidenceLevel,
)
from src.observability.tracer import AgentTracer, TraceSpan


class TestDataPoint:
    def test_create_data_point(self):
        dp = DataPoint(
            value="San Francisco, CA",
            confidence=ConfidenceLevel.HIGH,
            sources=["https://example.com"],
            reasoning="Listed on official company website",
        )
        assert dp.value == "San Francisco, CA"
        assert dp.confidence == ConfidenceLevel.HIGH
        assert len(dp.sources) == 1

    def test_data_point_defaults(self):
        dp = DataPoint(value="unknown", confidence=ConfidenceLevel.UNKNOWN)
        assert dp.sources == []
        assert dp.reasoning is None

    def test_data_point_serialization(self):
        dp = DataPoint(
            value="2015",
            confidence=ConfidenceLevel.MEDIUM,
            sources=["https://crunchbase.com/org/test"],
        )
        d = dp.model_dump()
        assert d["value"] == "2015"
        assert d["confidence"] == "medium"


class TestCompanyResearch:
    def test_full_company_research(self):
        research = CompanyResearch(
            company_name="Test Corp",
            description=DataPoint(
                value="A test company",
                confidence=ConfidenceLevel.HIGH,
                sources=["https://test.com"],
            ),
            founded_year=DataPoint(
                value="2015", confidence=ConfidenceLevel.MEDIUM, sources=[]
            ),
            headquarters=DataPoint(
                value="SF", confidence=ConfidenceLevel.HIGH, sources=[]
            ),
            employee_count=DataPoint(
                value="500", confidence=ConfidenceLevel.LOW, sources=[]
            ),
            industry=DataPoint(
                value="Tech", confidence=ConfidenceLevel.HIGH, sources=[]
            ),
        )
        assert research.company_name == "Test Corp"
        assert research.key_products == []
        assert research.key_leadership == []

    def test_company_research_from_json(self):
        """Test parsing a JSON response like the LLM would return."""
        raw = {
            "company_name": "Acme Inc",
            "description": {
                "value": "Cloud platform",
                "confidence": "high",
                "sources": ["https://acme.com"],
            },
            "founded_year": {
                "value": "2018",
                "confidence": "medium",
                "sources": [],
            },
            "headquarters": {
                "value": "NYC",
                "confidence": "high",
                "sources": [],
            },
            "employee_count": {
                "value": "1200",
                "confidence": "medium",
                "sources": [],
            },
            "industry": {
                "value": "SaaS",
                "confidence": "high",
                "sources": [],
            },
            "key_products": [
                {
                    "value": "Acme Cloud",
                    "confidence": "high",
                    "sources": ["https://acme.com/products"],
                }
            ],
            "key_leadership": [],
            "technology_stack": [],
            "recent_developments": [],
            "website": {
                "value": "https://acme.com",
                "confidence": "high",
                "sources": [],
            },
        }
        research = CompanyResearch(**raw)
        assert research.company_name == "Acme Inc"
        assert research.key_products[0].value == "Acme Cloud"


class TestAgentTracer:
    def test_tracer_basic_flow(self):
        tracer = AgentTracer()

        span = tracer.start_span("test_call", "research", "llm_call")
        tracer.end_span(
            span,
            input_tokens=500,
            output_tokens=200,
            model="claude-sonnet-4-20250514",
        )

        summary = tracer.summary()
        assert summary["total_llm_calls"] == 1
        assert summary["total_input_tokens"] == 500
        assert summary["total_output_tokens"] == 200
        assert summary["total_cost_usd"] > 0

    def test_tracer_multiple_agents(self):
        tracer = AgentTracer()

        s1 = tracer.start_span("research_llm", "research", "llm_call")
        tracer.end_span(s1, input_tokens=100, output_tokens=50, model="claude-sonnet-4-20250514")

        s2 = tracer.start_span("research_tool", "research", "tool_call")
        tracer.end_span(s2)

        s3 = tracer.start_span("financial_llm", "financial", "llm_call")
        tracer.end_span(s3, input_tokens=200, output_tokens=100, model="claude-sonnet-4-20250514")

        summary = tracer.summary()
        assert summary["total_llm_calls"] == 2
        assert summary["total_tool_calls"] == 1
        assert "research" in summary["by_agent"]
        assert "financial" in summary["by_agent"]

    def test_tracer_error_logging(self):
        tracer = AgentTracer()
        tracer.log_error("research", "API rate limit exceeded")
        summary = tracer.summary()
        assert len(summary["errors"]) == 1
        assert summary["errors"][0]["agent"] == "research"

    def test_span_cost_calculation(self):
        span = TraceSpan(
            name="test",
            agent="test",
            span_type="llm_call",
            input_tokens=1_000_000,  # 1M tokens
            output_tokens=1_000_000,
            model="claude-sonnet-4-20250514",
        )
        # Sonnet 4: $3/M input + $15/M output = $18
        assert span.cost_usd == pytest.approx(18.0, rel=0.01)

    def test_tracer_has_run_id_and_company(self):
        tracer = AgentTracer("Stripe")
        assert len(tracer.run_id) == 12
        assert tracer.company_name == "Stripe"
        summary = tracer.summary()
        assert summary["trace_id"] == tracer.run_id
        assert summary["company_name"] == "Stripe"

    def test_spans_carry_run_id(self):
        tracer = AgentTracer("Acme")
        span = tracer.start_span("test_call", "research", "llm_call")
        tracer.end_span(span, input_tokens=100, output_tokens=50, model="claude-sonnet-4-20250514")
        assert span.run_id == tracer.run_id
        assert len(span.span_id) == 12
        assert span.span_id != tracer.run_id

    def test_persist_to_sqlite(self, tmp_path):
        import sqlite3

        db_path = str(tmp_path / "test_trace.db")
        tracer = AgentTracer("TestCo")

        s1 = tracer.start_span("llm_turn_0", "research", "llm_call")
        tracer.end_span(s1, input_tokens=500, output_tokens=200, model="claude-sonnet-4-20250514")

        s2 = tracer.start_span("tool_search", "research", "tool_call", tool_name="web_search")
        s2.tool_name = "web_search"
        tracer.end_span(s2)

        tracer.persist(db_path)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # Verify runs table
        runs = [dict(r) for r in conn.execute("SELECT * FROM runs").fetchall()]
        assert len(runs) == 1
        assert runs[0]["trace_id"] == tracer.run_id
        assert runs[0]["company_name"] == "TestCo"
        assert runs[0]["status"] == "complete"
        assert runs[0]["total_cost_usd"] > 0

        # Verify spans table
        spans = [dict(r) for r in conn.execute("SELECT * FROM spans ORDER BY start_time").fetchall()]
        assert len(spans) == 2
        assert all(s["trace_id"] == tracer.run_id for s in spans)
        assert spans[0]["span_type"] == "llm_call"
        assert spans[1]["span_type"] == "tool_call"
        assert spans[1]["tool_name"] == "web_search"

        conn.close()

    def test_persist_error_status(self, tmp_path):
        import sqlite3

        db_path = str(tmp_path / "test_trace_err.db")
        tracer = AgentTracer("FailCo")
        tracer.log_error("research", "API timeout")
        tracer.persist(db_path)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        run = dict(conn.execute("SELECT * FROM runs").fetchone())
        assert run["status"] == "partial"

        spans = [dict(r) for r in conn.execute("SELECT * FROM spans").fetchall()]
        assert len(spans) == 1
        assert spans[0]["error"] == "API timeout"
        conn.close()
