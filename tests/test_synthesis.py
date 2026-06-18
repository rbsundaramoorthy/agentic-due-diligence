"""
Tests for the Synthesis Agent and related components.

Covers:
- CompanySynthesis schema validation and serialization
- build_synthesis_task() output structure and edge cases
- SynthesisAgent configuration (no tools, one-turn loop)
- parse_final_output with clean/wrapped/partial JSON
- Full agent loop with mocked LLM
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.agents.synthesis import SynthesisAgent, build_synthesis_task, _edgar_overlay_financial_dict
from src.observability.tracer import AgentTracer
from src.observability.agent_db import AgentDB
from src.schemas.models import CompanySynthesis, ConfidenceLevel, SeverityLevel


# ── Helpers ───────────────────────────────────────────────────────

def make_tracer():
    return AgentTracer("TestCo")


def make_mock_client():
    return MagicMock()


def make_dp(value="test", confidence="high", sources=None, severity=None, reasoning=None):
    d = {"value": value, "confidence": confidence, "sources": sources or [], "reasoning": reasoning}
    if severity is not None:
        d["severity"] = severity
    return d


def _make_text_response(text: str):
    block = SimpleNamespace(type="text", text=text)
    usage = SimpleNamespace(input_tokens=3000, output_tokens=500)
    return SimpleNamespace(content=[block], stop_reason="end_turn", usage=usage)


# ── Valid fixture ─────────────────────────────────────────────────

VALID_SYNTHESIS_JSON = {
    "company_name": "TestCo",
    "executive_summary": make_dp("TestCo is a growing SaaS company with strong revenue growth."),
    "investment_recommendation": make_dp("proceed_with_conditions"),
    "recommendation_rationale": make_dp("Strong metrics but regulatory exposure needs monitoring."),
    "key_strengths": [
        make_dp("50% YoY revenue growth", sources=["financial"]),
        make_dp("Market-leading product", sources=["research"]),
    ],
    "key_concerns": [
        make_dp("High cash burn rate", sources=["financial"]),
    ],
    "red_flags": [
        make_dp("Active regulatory investigation", confidence="medium",
                severity="high", sources=["risk"], reasoning="Could result in large fines."),
    ],
    "data_conflicts": [
        make_dp("Employee count: Research says 500, LinkedIn shows 800",
                confidence="low", sources=["research", "social_media"],
                reasoning="Discrepancy may reflect recent growth."),
    ],
    "follow_up_questions": [
        make_dp("Request audited financials for last 3 years",
                reasoning="Verify unaudited revenue figures."),
    ],
    "data_quality": make_dp("medium", reasoning="Good financials coverage, gaps in regulatory detail."),
}


# ── CompanySynthesis Schema Tests ─────────────────────────────────

class TestCompanySynthesis:
    def test_full_synthesis_valid(self):
        s = CompanySynthesis(**VALID_SYNTHESIS_JSON)
        assert s.company_name == "TestCo"
        assert s.investment_recommendation.value == "proceed_with_conditions"
        assert len(s.key_strengths) == 2
        assert len(s.red_flags) == 1
        assert s.red_flags[0].severity == SeverityLevel.HIGH
        assert len(s.data_conflicts) == 1

    def test_minimal_synthesis_uses_defaults(self):
        s = CompanySynthesis(
            company_name="Mini",
            executive_summary=make_dp("Summary."),
            investment_recommendation=make_dp("caution"),
            recommendation_rationale=make_dp("Too risky."),
        )
        assert s.key_strengths == []
        assert s.key_concerns == []
        assert s.red_flags == []
        assert s.data_conflicts == []
        assert s.follow_up_questions == []
        assert s.data_quality.confidence == ConfidenceLevel.UNKNOWN

    def test_recommendation_values_accepted(self):
        for rec in ["strong_proceed", "proceed", "proceed_with_conditions", "caution", "do_not_proceed"]:
            s = CompanySynthesis(
                company_name="Co",
                executive_summary=make_dp("x"),
                investment_recommendation=make_dp(rec),
                recommendation_rationale=make_dp("y"),
            )
            assert s.investment_recommendation.value == rec

    def test_serialization_roundtrip(self):
        s = CompanySynthesis(**VALID_SYNTHESIS_JSON)
        d = s.model_dump()
        s2 = CompanySynthesis(**d)
        assert s2.company_name == s.company_name
        assert s2.investment_recommendation.value == s.investment_recommendation.value
        assert len(s2.red_flags) == len(s.red_flags)

    def test_red_flag_severity_preserved(self):
        s = CompanySynthesis(**VALID_SYNTHESIS_JSON)
        d = s.model_dump()
        assert d["red_flags"][0]["severity"] == "high"

    def test_from_json_string_roundtrip(self):
        raw = json.dumps(VALID_SYNTHESIS_JSON)
        parsed = json.loads(raw)
        s = CompanySynthesis(**parsed)
        assert s.company_name == "TestCo"
        assert s.data_quality.value == "medium"


# ── build_synthesis_task Tests ────────────────────────────────────

class TestBuildSynthesisTask:
    def test_contains_all_section_headers(self):
        task = build_synthesis_task("Stripe", None, None, None, None)
        assert "== RESEARCH AGENT ==" in task
        assert "== FINANCIAL AGENT ==" in task
        assert "== RISK AGENT ==" in task
        assert "== SOCIAL MEDIA AGENT ==" in task

    def test_none_data_shows_not_completed_message(self):
        task = build_synthesis_task("Stripe", None, None, None, None)
        assert "agent did not complete" in task

    def test_provided_data_serialized_into_task(self):
        research = {"company_name": "Stripe", "founded_year": {"value": "2010", "confidence": "high", "sources": []}}
        task = build_synthesis_task("Stripe", research, None, None, None)
        assert "2010" in task
        assert "founded_year" in task

    def test_company_name_in_task(self):
        task = build_synthesis_task("Anthropic", None, None, None, None)
        assert "Anthropic" in task

    def test_long_data_truncated(self):
        big_data = {"company_name": "TestCo", "description": {"value": "x" * 15000, "confidence": "high", "sources": []}}
        task = build_synthesis_task("TestCo", big_data, None, None, None)
        # The research section should be truncated, not 15k chars long
        research_section_start = task.find("== RESEARCH AGENT ==")
        financial_section_start = task.find("== FINANCIAL AGENT ==")
        research_section = task[research_section_start:financial_section_start]
        assert len(research_section) < 10000


# ── SynthesisAgent Config Tests ───────────────────────────────────

class TestSynthesisAgentConfig:
    def setup_method(self):
        self.agent = SynthesisAgent(tracer=make_tracer(), client=make_mock_client())

    def test_agent_name(self):
        assert self.agent.AGENT_NAME == "synthesis"

    def test_get_tools_returns_empty_list(self):
        assert self.agent.get_tools() == []

    def test_system_prompt_not_empty(self):
        assert len(self.agent.get_system_prompt()) > 200

    def test_system_prompt_mentions_recommendation_values(self):
        prompt = self.agent.get_system_prompt()
        for value in ["strong_proceed", "proceed_with_conditions", "caution", "do_not_proceed"]:
            assert value in prompt

    @pytest.mark.asyncio
    async def test_handle_tool_call_returns_error(self):
        result = await self.agent.handle_tool_call("web_search", {"query": "x"})
        data = json.loads(result)
        assert "error" in data


# ── SynthesisAgent Parse Tests ────────────────────────────────────

class TestSynthesisAgentParse:
    def setup_method(self):
        self.agent = SynthesisAgent(tracer=make_tracer(), client=make_mock_client())

    def test_parse_clean_json(self):
        result = self.agent.parse_final_output(json.dumps(VALID_SYNTHESIS_JSON))
        assert result["company_name"] == "TestCo"
        assert result["investment_recommendation"]["value"] == "proceed_with_conditions"

    def test_parse_markdown_wrapped(self):
        text = f"```json\n{json.dumps(VALID_SYNTHESIS_JSON)}\n```"
        result = self.agent.parse_final_output(text)
        assert result["company_name"] == "TestCo"

    def test_parse_with_preamble(self):
        text = f"Here is my synthesis:\n{json.dumps(VALID_SYNTHESIS_JSON)}"
        result = self.agent.parse_final_output(text)
        assert result["company_name"] == "TestCo"

    def test_parse_preserves_red_flag_severity(self):
        result = self.agent.parse_final_output(json.dumps(VALID_SYNTHESIS_JSON))
        assert result["red_flags"][0]["severity"] == "high"

    def test_parse_invalid_json_raises(self):
        with pytest.raises(Exception):
            self.agent.parse_final_output("not valid json")

    def test_parse_wrong_schema_raises(self):
        with pytest.raises(Exception):
            self.agent.parse_final_output('{"wrong": "schema"}')


# ── SynthesisAgent Loop Tests ─────────────────────────────────────

class TestSynthesisAgentLoop:
    @pytest.mark.asyncio
    async def test_completes_in_one_turn(self):
        client = make_mock_client()
        client.messages.create.return_value = _make_text_response(
            json.dumps(VALID_SYNTHESIS_JSON)
        )
        agent = SynthesisAgent(tracer=make_tracer(), client=client)
        result = await agent.run("Synthesize TestCo findings.")

        assert result["status"] == "complete"
        assert result["data"]["company_name"] == "TestCo"
        assert client.messages.create.call_count == 1

    @pytest.mark.asyncio
    async def test_parse_failure_triggers_retry(self):
        client = make_mock_client()
        client.messages.create.side_effect = [
            _make_text_response("not valid json"),
            _make_text_response(json.dumps(VALID_SYNTHESIS_JSON)),
        ]
        agent = SynthesisAgent(tracer=make_tracer(), client=client)
        result = await agent.run("Synthesize TestCo findings.")

        assert result["status"] == "complete"
        assert client.messages.create.call_count == 2

    @pytest.mark.asyncio
    async def test_exhausted_retries_returns_partial(self):
        client = make_mock_client()
        client.messages.create.return_value = _make_text_response("bad json always")
        agent = SynthesisAgent(tracer=make_tracer(), client=client)
        result = await agent.run("Synthesize TestCo findings.")

        assert result["status"] == "partial"

    @pytest.mark.asyncio
    async def test_llm_error_returns_failed(self):
        client = make_mock_client()
        client.messages.create.side_effect = Exception("API error")
        agent = SynthesisAgent(tracer=make_tracer(), client=client)
        result = await agent.run("Synthesize TestCo findings.")

        assert result["status"] == "failed"
        assert "API error" in result["error_summary"]

    @pytest.mark.asyncio
    async def test_no_tools_called_during_loop(self):
        client = make_mock_client()
        client.messages.create.return_value = _make_text_response(
            json.dumps(VALID_SYNTHESIS_JSON)
        )
        tracer = make_tracer()
        agent = SynthesisAgent(tracer=tracer, client=client)
        await agent.run("Synthesize TestCo findings.")

        tool_spans = [s for s in tracer.spans if s.span_type == "tool_call"]
        assert tool_spans == []

    @pytest.mark.asyncio
    async def test_result_logged_to_db(self):
        client = make_mock_client()
        client.messages.create.return_value = _make_text_response(
            json.dumps(VALID_SYNTHESIS_JSON)
        )
        db = AgentDB()
        tracer = make_tracer()
        agent = SynthesisAgent(tracer=tracer, client=client, db=db)
        await agent.run("Synthesize TestCo findings.")

        runs = db.get_runs("synthesis")
        assert len(runs) == 1
        assert runs[0]["status"] == "complete"
        assert runs[0]["total_turns"] == 1


# ── _edgar_overlay_financial_dict Tests ──────────────────────────────

class TestEdgarOverlayFinancialDict:
    def _fin(self, rev_value="unknown", rev_conf="unknown", claim_id=None, **extra):
        dp = {"value": rev_value, "confidence": rev_conf, "sources": [], "reasoning": None}
        if claim_id:
            dp["_claim_id"] = claim_id
        return {"company_name": "TestCo", "revenue": dp, **extra}

    def _edgar(self, status="succeeded", rev_value="$100B", rev_conf="high"):
        return {
            "edgar_lookup_status": status,
            "revenue": {"value": rev_value, "confidence": rev_conf, "sources": ["https://sec.gov/x"]},
        }

    def test_overlays_revenue_when_edgar_succeeded(self):
        fin = self._fin(claim_id="abc123")
        result = _edgar_overlay_financial_dict(fin, self._edgar())
        assert result["revenue"]["value"] == "$100B"
        assert result["revenue"]["confidence"] == "high"

    def test_uses_edgar_claim_id_when_annotated(self):
        """edgar_data must be annotated before calling this function so the overlay
        carries the EDGAR DataPoint's own _claim_id (not the financial agent's)."""
        from src.synthesis.assembler import annotate_claim_ids
        fin = self._fin(claim_id="fin_abc123")
        edgar = annotate_claim_ids(self._edgar())  # annotate edgar so it has _claim_id
        result = _edgar_overlay_financial_dict(fin, edgar)
        # Overlay uses edgar's own _claim_id, NOT the financial agent's "fin_abc123"
        assert result["revenue"]["_claim_id"] == edgar["revenue"]["_claim_id"]
        assert result["revenue"]["_claim_id"] != "fin_abc123"

    def test_no_claim_id_when_edgar_not_annotated(self):
        """When edgar_data has no _claim_id (not annotated), overlay also lacks it."""
        fin = self._fin(claim_id="abc123")
        edgar = self._edgar()  # NOT annotated → no _claim_id on revenue
        result = _edgar_overlay_financial_dict(fin, edgar)
        assert result["revenue"]["value"] == "$100B"
        assert "_claim_id" not in result["revenue"]

    def test_overlays_profitability_when_present(self):
        from src.synthesis.assembler import annotate_claim_ids
        fin = {
            "company_name": "TestCo",
            "revenue": {"value": "unknown", "confidence": "unknown", "sources": [], "_claim_id": "r1"},
            "profitability": {"value": "unknown", "confidence": "unknown", "sources": [], "_claim_id": "p1"},
        }
        edgar = annotate_claim_ids({
            "edgar_lookup_status": "succeeded",
            "revenue": {"value": "$100B", "confidence": "high", "sources": []},
            "profitability": {"value": "Net income: $20B", "confidence": "high", "sources": []},
        })
        result = _edgar_overlay_financial_dict(fin, edgar)
        assert result["revenue"]["value"] == "$100B"
        assert result["revenue"]["_claim_id"] == edgar["revenue"]["_claim_id"]  # edgar's own ID
        assert result["profitability"]["value"] == "Net income: $20B"
        assert result["profitability"]["_claim_id"] == edgar["profitability"]["_claim_id"]

    def test_non_edgar_fields_untouched(self):
        fin = {
            "company_name": "TestCo",
            "revenue": {"value": "unknown", "confidence": "unknown", "sources": []},
            "total_funding": {"value": "$500M", "confidence": "high", "sources": []},
        }
        result = _edgar_overlay_financial_dict(fin, self._edgar())
        assert result["total_funding"]["value"] == "$500M"

    def test_not_applied_when_status_not_succeeded(self):
        fin = self._fin()
        edgar = self._edgar(status="not_sec_reporting")
        result = _edgar_overlay_financial_dict(fin, edgar)
        assert result is fin

    def test_not_applied_when_edgar_none(self):
        fin = self._fin()
        result = _edgar_overlay_financial_dict(fin, None)
        assert result is fin

    def test_not_applied_when_financial_none(self):
        edgar = self._edgar()
        result = _edgar_overlay_financial_dict(None, edgar)
        assert result is None

    def test_edgar_unknown_confidence_not_overlaid(self):
        fin = self._fin(claim_id="abc")
        edgar = {"edgar_lookup_status": "succeeded", "revenue": {"value": "unknown", "confidence": "unknown"}}
        result = _edgar_overlay_financial_dict(fin, edgar)
        assert result["revenue"]["value"] == "unknown"
        assert result["revenue"]["confidence"] == "unknown"

    def test_does_not_mutate_original_dict(self):
        fin = self._fin(rev_value="unknown", rev_conf="unknown", claim_id="abc")
        edgar = self._edgar()
        result = _edgar_overlay_financial_dict(fin, edgar)
        assert result is not fin
        assert fin["revenue"]["value"] == "unknown"  # original unchanged

    def test_no_claim_id_on_original_still_works(self):
        """Works cleanly when the original DataPoint has no _claim_id yet."""
        fin = {"revenue": {"value": "unknown", "confidence": "unknown"}}
        edgar = {"edgar_lookup_status": "succeeded", "revenue": {"value": "$100B", "confidence": "high"}}
        result = _edgar_overlay_financial_dict(fin, edgar)
        assert result["revenue"]["value"] == "$100B"
        assert "_claim_id" not in result["revenue"]


# ── build_synthesis_task EDGAR overlay integration Tests ─────────────

class TestBuildSynthesisTaskEdgarOverlay:
    def _financial_data(self):
        return {
            "company_name": "TestCo",
            "revenue": {
                "value": "unknown", "confidence": "unknown",
                "sources": [], "reasoning": "Deferred to EDGAR.",
                "_claim_id": "fin_rev_001",
            },
        }

    def _edgar_succeeded(self):
        return {
            "edgar_lookup_status": "succeeded",
            "cik": "0001234567",
            "most_recent_filing": {"value": "10-K 2025", "confidence": "high", "sources": []},
            "revenue": {"value": "$391.04B", "confidence": "high", "sources": ["https://data.sec.gov/x"]},
            "sec_risk_factors": [],
        }

    def test_financial_section_shows_edgar_revenue(self):
        """Synthesis sees merged revenue, not the pre-merge unknown placeholder."""
        task = build_synthesis_task(
            "TestCo", None, self._financial_data(), None, None,
            edgar_data=self._edgar_succeeded(),
        )
        fin_start = task.find("== FINANCIAL AGENT ==")
        risk_start = task.find("== RISK AGENT ==")
        financial_section = task[fin_start:risk_start]
        assert "$391.04B" in financial_section

    def test_financial_section_unchanged_without_edgar(self):
        """When no edgar_data, financial section shows original values."""
        task = build_synthesis_task(
            "TestCo", None, self._financial_data(), None, None,
            edgar_data=None,
        )
        fin_start = task.find("== FINANCIAL AGENT ==")
        risk_start = task.find("== RISK AGENT ==")
        financial_section = task[fin_start:risk_start]
        assert "Deferred to EDGAR" in financial_section

    def test_financial_section_unchanged_when_edgar_not_succeeded(self):
        """When EDGAR did not succeed, financial section shows original values."""
        edgar_not_sec = {"edgar_lookup_status": "not_sec_reporting"}
        task = build_synthesis_task(
            "TestCo", None, self._financial_data(), None, None,
            edgar_data=edgar_not_sec,
        )
        fin_start = task.find("== FINANCIAL AGENT ==")
        risk_start = task.find("== RISK AGENT ==")
        financial_section = task[fin_start:risk_start]
        assert "Deferred to EDGAR" in financial_section
        assert "$391.04B" not in financial_section

    def test_original_financial_data_not_mutated(self):
        """build_synthesis_task must not mutate the financial_data it receives."""
        fin = self._financial_data()
        _ = build_synthesis_task(
            "TestCo", None, fin, None, None,
            edgar_data=self._edgar_succeeded(),
        )
        assert fin["revenue"]["value"] == "unknown"
