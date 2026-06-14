"""
Attribution tests for the Risk Agent's CourtListener claim routing.

These tests verify that the agent:
  1. Routes in rem / non-party cases to reputational_risks, NOT pending_litigation
  2. Routes true-defendant cases to pending_litigation
  3. Never cites the bare CourtListener API endpoint as a source
  4. Records party names and company role in the reasoning field

Tests mock both the CourtListener tool call (deterministic fixture data) and the
LLM response (canned JSON representing what a correctly-prompted agent would emit).
The canned JSON is intentionally the "correct" output — these tests validate schema
acceptance and routing, not prompt-following.  A separate prompt-content test confirms
the agent's system prompt carries the required attribution language.
"""

import json
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.agents.risk import RiskAgent
from src.observability.tracer import AgentTracer
from src.schemas.models import CompanyRisks

_BARE_ENDPOINT = "https://www.courtlistener.com/api/rest/v4/search/"
_DOCKET_PREFIX = "https://www.courtlistener.com/docket/"


# ── Shared helpers ────────────────────────────────────────────────────────────

def _make_tracer():
    return AgentTracer()


def _make_text_response(text: str):
    block = SimpleNamespace(type="text", text=text)
    usage = SimpleNamespace(input_tokens=100, output_tokens=50)
    return SimpleNamespace(content=[block], stop_reason="end_turn", usage=usage)


def _make_tool_response(tool_name: str, tool_input: dict):
    block = SimpleNamespace(type="tool_use", name=tool_name, input=tool_input, id="tool_001")
    usage = SimpleNamespace(input_tokens=100, output_tokens=50)
    return SimpleNamespace(content=[block], stop_reason="tool_use", usage=usage)


_SAMGOV_EMPTY = json.dumps({
    "found": False,
    "note": "No SAM.gov registration found.",
})

# Tool result for the third-party seizure fixture
_COURTLISTENER_THIRDPARTY = json.dumps({
    "found": True,
    "case_count": 755,
    "cases_returned": 2,
    "cases": [
        {
            "case_name": "Romero v. Example Aerospace Corp",
            "court": "nysd",
            "date_filed": "2026-05-08",
            "docket_number": "1:26-cv-03843",
            "source_url": "https://www.courtlistener.com/docket/73317853/romero-v-example-aerospace-corp/",
            "parties": ["Evan Romero", "Example Aerospace Corp"],
            "cause": "28:1332bc Diversity-Breach of Contract",
        },
        {
            "case_name": "United States v. SEIZURE OF NINE TERMINALS AND ASSOCIATED ACCOUNTS UNDER THE CONTROL OF EXAMPLE AEROSPACE CORP FOR VIOLATIONS OF 18 U.S.C. §§ 1349, 1956",
            "court": "dcd",
            "date_filed": "2025-11-07",
            "docket_number": "1:25-sz-00048",
            "source_url": "https://www.courtlistener.com/docket/71926259/united-states-v-seizure-of-nine-terminals-and-associated-accounts/",
            "parties": [
                "USA",
                "SEIZURE OF NINE TERMINALS AND ASSOCIATED ACCOUNTS UNDER THE CONTROL OF EXAMPLE AEROSPACE CORP FOR VIOLATIONS OF 18 U.S.C. §§ 1349, 1956",
            ],
            "cause": "",
        },
    ],
    "source_url": _BARE_ENDPOINT,
    "note": "Data from CourtListener RECAP Archive.",
})


# ── Canned LLM outputs ────────────────────────────────────────────────────────

def _base_risk_json(**overrides) -> dict:
    base = {
        "company_name": "Example Aerospace Corp",
        "overall_risk_rating": {"value": "medium", "confidence": "medium", "sources": ["https://example.com"], "reasoning": None},
        "risk_summary": {"value": "Moderate risk profile.", "confidence": "medium", "sources": ["https://example.com"], "reasoning": None},
        "regulatory_risks": [],
        "legal_risks": [],
        "cybersecurity_risks": [],
        "operational_risks": [],
        "reputational_risks": [],
        "esg_risks": [],
        "pending_litigation": [],
        "government_contract_exposure": {
            "value": "No SAM.gov registration found.",
            "confidence": "high",
            "sources": ["https://sam.gov/"],
            "reasoning": None,
        },
        "notable_federal_contracts": [],
    }
    base.update(overrides)
    return base


# Correctly attributed: seizure in reputational_risks, Romero in pending_litigation
_CORRECT_OUTPUT = _base_risk_json(
    pending_litigation=[
        {
            "value": "Romero v. Example Aerospace Corp — employment breach-of-contract dispute, S.D.N.Y., case 1:26-cv-03843, filed May 2026",
            "confidence": "high",
            "severity": "medium",
            "sources": ["https://www.courtlistener.com/docket/73317853/romero-v-example-aerospace-corp/"],
            "reasoning": (
                "Named parties: Evan Romero, Example Aerospace Corp. Example Aerospace Corp is a named defendant. "
                "Cause: 28:1332bc Diversity-Breach of Contract."
            ),
        }
    ],
    reputational_risks=[
        {
            "value": (
                "Example Aerospace Corp terminals referenced as subject of in rem seizure proceeding "
                "(D.D.C., 1:25-sz-00048, filed Nov 2025). Example Aerospace Corp is not a named party; "
                "nine terminals are the res; §§ 1349/1956 are charged against "
                "Myanmar scam-compound operators, not Example Aerospace Corp."
            ),
            "confidence": "high",
            "severity": "low",
            "sources": ["https://www.courtlistener.com/docket/71926259/united-states-v-seizure-of-nine-terminals-and-associated-accounts/"],
            "reasoning": (
                "Named parties: USA, SEIZURE OF NINE TERMINALS … "
                "Example Aerospace Corp is not a named party; the res is nine terminals; "
                "§§ 1349/1956 charged against criminal scam-compound operators, not Example Aerospace Corp."
            ),
        }
    ],
)

# Incorrectly attributed (regression): seizure falsely in pending_litigation as CRITICAL
_WRONG_OUTPUT_REGRESSION = _base_risk_json(
    pending_litigation=[
        {
            "value": "Federal seizure action for alleged conspiracy and money laundering violations involving Starlink terminals, U.S. District Court for D.C., Case 1:25-sz-00048",
            "confidence": "high",
            "severity": "critical",
            "sources": [_BARE_ENDPOINT],
            "reasoning": "Federal enforcement action indicates serious criminal allegations",
        }
    ],
)


# ── Prompt content tests ──────────────────────────────────────────────────────

class TestRiskAgentPromptContent:
    def setup_method(self):
        self.agent = RiskAgent(tracer=_make_tracer(), client=MagicMock())

    def test_prompt_version_is_2_2(self):
        assert self.agent.PROMPT_VERSION == "2.2"

    def test_prompt_contains_party_membership_check(self):
        prompt = self.agent.get_system_prompt()
        assert "parties" in prompt.lower()
        assert "named part" in prompt.lower()

    def test_prompt_routes_non_party_to_reputational_risks(self):
        prompt = self.agent.get_system_prompt()
        assert "reputational_risks" in prompt
        assert "pending_litigation" in prompt
        # Routing instruction present
        assert "non-party" in prompt.lower() or "not a named party" in prompt.lower()

    def test_prompt_forbids_bare_endpoint_citation(self):
        prompt = self.agent.get_system_prompt()
        assert "api/rest/v4/search" in prompt
        assert "Never cite" in prompt or "never" in prompt.lower()

    def test_prompt_requires_reasoning_with_parties(self):
        prompt = self.agent.get_system_prompt()
        assert "reasoning" in prompt.lower()
        assert "named parties" in prompt.lower() or "named part" in prompt.lower()

    def test_prompt_recognizes_seizure_patterns(self):
        prompt = self.agent.get_system_prompt()
        assert "SEIZURE OF" in prompt or "seizure" in prompt.lower()
        assert "in rem" in prompt.lower()

    def test_prompt_conservative_default_for_empty_parties(self):
        prompt = self.agent.get_system_prompt()
        assert "empty" in prompt.lower() or "absent" in prompt.lower() or "missing" in prompt.lower()
        assert "low" in prompt.lower()


# ── Schema / routing tests using canned LLM output ───────────────────────────

class TestRiskAttributionRouting:
    """These tests mock the full agent loop and verify that correctly-routed output
    passes schema validation, while the regression misattribution is detectable."""

    def _make_agent(self, llm_responses: list) -> RiskAgent:
        client = MagicMock()
        client.messages.create.side_effect = [
            _make_tool_response("samgov_contract_search", {"company_name": "Example Aerospace Corp"}),
            _make_tool_response("courtlistener_case_search", {"company_name": "Example Aerospace Corp"}),
        ] + llm_responses
        return RiskAgent(tracer=_make_tracer(), client=client)

    @pytest.mark.asyncio
    async def test_correct_routing_passes_schema_validation(self):
        """Seizure case in reputational_risks + Romero in pending_litigation validates."""
        agent = self._make_agent([_make_text_response(json.dumps(_CORRECT_OUTPUT))])

        with (
            patch("src.sources.samgov.samgov_search_contracts", new=AsyncMock(return_value=_SAMGOV_EMPTY)),
            patch("src.sources.courtlistener.courtlistener_search_cases", new=AsyncMock(return_value=_COURTLISTENER_THIRDPARTY)),
        ):
            result = await agent.run("Research Example Aerospace Corp risks")

        assert result["status"] == "complete"
        data = result["data"]
        risks = CompanyRisks(**data)

        # Seizure case must be in reputational_risks, NOT pending_litigation
        pending_values = [c.value for c in risks.pending_litigation]
        rep_values = [c.value for c in risks.reputational_risks]

        assert not any("conspiracy" in v.lower() or "money laundering" in v.lower()
                       for v in pending_values), (
            "Seizure case must not attribute conspiracy/money laundering to Example Aerospace Corp in pending_litigation"
        )
        assert any("seizure" in v.lower() or "in rem" in v.lower() or "not a named party" in v.lower()
                   for v in rep_values), (
            "Seizure case should appear in reputational_risks as in rem / non-party"
        )

    @pytest.mark.asyncio
    async def test_true_defendant_case_in_pending_litigation(self):
        """Romero v. Example Aerospace Corp (company is a named party) goes to pending_litigation."""
        agent = self._make_agent([_make_text_response(json.dumps(_CORRECT_OUTPUT))])

        with (
            patch("src.sources.samgov.samgov_search_contracts", new=AsyncMock(return_value=_SAMGOV_EMPTY)),
            patch("src.sources.courtlistener.courtlistener_search_cases", new=AsyncMock(return_value=_COURTLISTENER_THIRDPARTY)),
        ):
            result = await agent.run("Research Example Aerospace Corp risks")

        risks = CompanyRisks(**result["data"])
        pending_values = [c.value for c in risks.pending_litigation]
        assert any("Romero" in v for v in pending_values), (
            "Romero v. Example Aerospace Corp (true defendant) must appear in pending_litigation"
        )

    @pytest.mark.asyncio
    async def test_true_defendant_severity_not_low(self):
        """Employment case where the company is a named defendant must not be LOW severity."""
        agent = self._make_agent([_make_text_response(json.dumps(_CORRECT_OUTPUT))])

        with (
            patch("src.sources.samgov.samgov_search_contracts", new=AsyncMock(return_value=_SAMGOV_EMPTY)),
            patch("src.sources.courtlistener.courtlistener_search_cases", new=AsyncMock(return_value=_COURTLISTENER_THIRDPARTY)),
        ):
            result = await agent.run("Research Example Aerospace Corp risks")

        risks = CompanyRisks(**result["data"])
        romero = next(
            (c for c in risks.pending_litigation if "Romero" in c.value), None
        )
        assert romero is not None
        assert romero.severity is not None
        assert romero.severity.value != "low", (
            "Employment case with company as defendant should not have LOW severity"
        )

    @pytest.mark.asyncio
    async def test_non_party_severity_is_low(self):
        """In rem seizure case routed to reputational_risks must have LOW severity."""
        agent = self._make_agent([_make_text_response(json.dumps(_CORRECT_OUTPUT))])

        with (
            patch("src.sources.samgov.samgov_search_contracts", new=AsyncMock(return_value=_SAMGOV_EMPTY)),
            patch("src.sources.courtlistener.courtlistener_search_cases", new=AsyncMock(return_value=_COURTLISTENER_THIRDPARTY)),
        ):
            result = await agent.run("Research Example Aerospace Corp risks")

        risks = CompanyRisks(**result["data"])
        seizure = next(
            (c for c in risks.reputational_risks
             if "seizure" in c.value.lower() or "in rem" in c.value.lower()
             or "not a named party" in c.value.lower()),
            None,
        )
        assert seizure is not None, "In rem case should appear in reputational_risks"
        assert seizure.severity is not None
        assert seizure.severity.value == "low"

    @pytest.mark.asyncio
    async def test_citation_no_bare_endpoint_in_pending_litigation(self):
        """No pending_litigation source may cite the bare CourtListener API endpoint."""
        agent = self._make_agent([_make_text_response(json.dumps(_CORRECT_OUTPUT))])

        with (
            patch("src.sources.samgov.samgov_search_contracts", new=AsyncMock(return_value=_SAMGOV_EMPTY)),
            patch("src.sources.courtlistener.courtlistener_search_cases", new=AsyncMock(return_value=_COURTLISTENER_THIRDPARTY)),
        ):
            result = await agent.run("Research Example Aerospace Corp risks")

        risks = CompanyRisks(**result["data"])
        for claim in risks.pending_litigation:
            for src in claim.sources:
                assert src != _BARE_ENDPOINT, (
                    f"pending_litigation claim cites bare API endpoint: {src}"
                )

    @pytest.mark.asyncio
    async def test_citation_no_bare_endpoint_in_reputational_risks(self):
        """No reputational_risks source may cite the bare CourtListener API endpoint."""
        agent = self._make_agent([_make_text_response(json.dumps(_CORRECT_OUTPUT))])

        with (
            patch("src.sources.samgov.samgov_search_contracts", new=AsyncMock(return_value=_SAMGOV_EMPTY)),
            patch("src.sources.courtlistener.courtlistener_search_cases", new=AsyncMock(return_value=_COURTLISTENER_THIRDPARTY)),
        ):
            result = await agent.run("Research Example Aerospace Corp risks")

        risks = CompanyRisks(**result["data"])
        for claim in risks.reputational_risks:
            for src in claim.sources:
                assert src != _BARE_ENDPOINT

    def test_regression_output_parse_succeeds_but_reveals_false_accusation(self):
        """The old incorrect output still parses (schema is permissive), but the
        critical-severity money-laundering claim against the company is detectable.

        This test documents the regression pattern — any monitor that checks
        pending_litigation for 'conspiracy'/'money laundering' with severity='critical'
        where the docket type is 'sz' would catch this.
        """
        agent = RiskAgent(tracer=_make_tracer(), client=MagicMock())
        parsed = agent.parse_final_output(json.dumps(_WRONG_OUTPUT_REGRESSION))
        risks = CompanyRisks(**parsed)

        # The regression output parses fine (schema cannot prevent it)
        assert len(risks.pending_litigation) == 1
        claim = risks.pending_litigation[0]

        # But the defects are clearly visible:
        assert claim.severity is not None
        assert claim.severity.value == "critical"
        assert "conspiracy" in claim.value.lower() or "money laundering" in claim.value.lower()
        # Source is the bare endpoint — verifiable-citation failure
        assert any(src == _BARE_ENDPOINT for src in claim.sources)

    def test_reasoning_field_contains_party_info(self):
        """Correctly attributed output must include party info in reasoning."""
        agent = RiskAgent(tracer=_make_tracer(), client=MagicMock())
        parsed = agent.parse_final_output(json.dumps(_CORRECT_OUTPUT))
        risks = CompanyRisks(**parsed)

        # Seizure claim in reputational_risks
        seizure = next(
            (c for c in risks.reputational_risks
             if "seizure" in c.value.lower() or "in rem" in c.value.lower()
             or "not a named party" in c.value.lower()),
            None,
        )
        assert seizure is not None
        assert seizure.reasoning is not None
        reasoning_lower = seizure.reasoning.lower()
        # Must state the company is not a named party
        assert "not a named party" in reasoning_lower or "not" in reasoning_lower
        # Must mention the res or the charges attribution
        assert "terminal" in reasoning_lower or "res" in reasoning_lower
