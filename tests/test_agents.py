"""
Tests for Research and Financial agents.

Tests cover:
- Schema validation (CompanyFinancials)
- JSON parsing (clean, markdown-wrapped, with preamble)
- Tool dispatch (web_search, web_fetch, unknown tools)
- Agent configuration (name, tools, system prompt)
- Full agent loop with mocked LLM responses
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace

import pytest

from src.agents.research import ResearchAgent
from src.agents.financial import FinancialAgent
from src.agents.risk import RiskAgent
from src.agents.social_media import SocialMediaAgent
from src.agents.classifier import classify_company
from src.observability.tracer import AgentTracer
from src.schemas.models import CompanyFinancials, CompanyResearch, CompanyRisks, CompanySocialMedia, ConfidenceLevel, SeverityLevel, DataPoint
from src.observability.agent_db import AgentDB
from src.synthesis.report_generator import _sort_by_severity, _compute_section_confidence, _compute_report_confidence


# ── Fixtures ─────────────────────────────────────────────────────

def make_tracer():
    return AgentTracer()


def make_mock_client():
    client = MagicMock()
    client.messages.create = AsyncMock()
    return client


def make_data_point(value="test", confidence="high"):
    return {"value": value, "confidence": confidence, "sources": ["https://example.com"], "reasoning": None}


# ── Valid JSON responses for agent parsing ─────────────────────────

VALID_RESEARCH_JSON = {
    "company_name": "TestCo",
    "description": make_data_point("A test company"),
    "founded_year": make_data_point("2020"),
    "headquarters": make_data_point("SF"),
    "employee_count": make_data_point("100"),
    "industry": make_data_point("Tech"),
    "key_products": [make_data_point("Product A")],
    "key_leadership": [],
    "technology_stack": [],
    "recent_developments": [],
    "website": make_data_point("https://testco.com"),
}

VALID_FINANCIAL_JSON = {
    "company_name": "TestCo",
    "revenue": make_data_point("$10M"),
    "revenue_growth": make_data_point("50% YoY"),
    "profitability": make_data_point("Not profitable"),
    "total_funding": make_data_point("$25M"),
    "last_funding_round": make_data_point("Series A"),
    "valuation": make_data_point("$100M"),
    "key_investors": [make_data_point("Sequoia")],
    "revenue_model": make_data_point("SaaS subscription"),
    "key_customers": [],
    "financial_risks": [],
    "recent_financial_events": [],
}

VALID_RISK_JSON = {
    "company_name": "TestCo",
    "overall_risk_rating": make_data_point("medium"),
    "risk_summary": make_data_point("Moderate risk profile"),
    "regulatory_risks": [{"value": "GDPR compliance", "confidence": "high", "severity": "medium", "sources": ["https://example.com"], "reasoning": None}],
    "legal_risks": [],
    "cybersecurity_risks": [],
    "operational_risks": [],
    "reputational_risks": [],
    "esg_risks": [],
}

VALID_SOCIAL_MEDIA_JSON = {
    "company_name": "TestCo",
    "overall_sentiment": make_data_point("positive"),
    "sentiment_summary": make_data_point("Generally positive sentiment"),
    "twitter_presence": make_data_point("10K followers"),
    "linkedin_presence": make_data_point("5K followers"),
    "reddit_discussions": [],
    "glassdoor_rating": make_data_point("4.2/5"),
    "customer_complaints": [],
    "notable_mentions": [],
    "positive_signals": [make_data_point("Industry award winner")],
}


# ── Mock response helpers ──────────────────────────────────────────

def _make_text_response(text: str):
    """Create a mock Anthropic response with a text block."""
    block = SimpleNamespace(type="text", text=text)
    usage = SimpleNamespace(input_tokens=100, output_tokens=50)
    return SimpleNamespace(content=[block], stop_reason="end_turn", usage=usage)


def _make_tool_response(tool_name: str, tool_input: dict):
    """Create a mock Anthropic response with a tool_use block."""
    block = SimpleNamespace(type="tool_use", name=tool_name, input=tool_input, id="tool_123")
    usage = SimpleNamespace(input_tokens=100, output_tokens=50)
    return SimpleNamespace(content=[block], stop_reason="tool_use", usage=usage)


def _make_truncated_response(text: str):
    """Create a mock response cut off by max_tokens (unparseable truncated JSON)."""
    block = SimpleNamespace(type="text", text=text)
    usage = SimpleNamespace(input_tokens=100, output_tokens=4096)
    return SimpleNamespace(content=[block], stop_reason="max_tokens", usage=usage)


# ── CompanyFinancials Schema Tests ────────────────────────────────

class TestCompanyFinancials:
    def test_full_financials(self):
        fin = CompanyFinancials(**VALID_FINANCIAL_JSON)
        assert fin.company_name == "TestCo"
        assert fin.revenue.value == "$10M"
        assert fin.revenue.confidence == ConfidenceLevel.HIGH
        assert len(fin.key_investors) == 1
        assert fin.key_investors[0].value == "Sequoia"

    def test_minimal_financials(self):
        fin = CompanyFinancials(
            company_name="Mini",
            revenue=make_data_point("unknown", "low"),
            revenue_growth=make_data_point("unknown", "low"),
            profitability=make_data_point("unknown", "low"),
            total_funding=make_data_point("unknown", "low"),
            last_funding_round=make_data_point("unknown", "low"),
            valuation=make_data_point("unknown", "low"),
            revenue_model=make_data_point("unknown", "low"),
        )
        assert fin.company_name == "Mini"
        assert fin.key_investors == []

    def test_financials_from_json(self):
        raw = json.dumps(VALID_FINANCIAL_JSON)
        parsed = json.loads(raw)
        fin = CompanyFinancials(**parsed)
        assert fin.company_name == "TestCo"
        assert fin.revenue.value == "$10M"

    def test_financials_serialization_roundtrip(self):
        fin = CompanyFinancials(**VALID_FINANCIAL_JSON)
        d = fin.model_dump()
        fin2 = CompanyFinancials(**d)
        assert fin2.company_name == fin.company_name
        assert fin2.revenue.value == fin.revenue.value


# ── Research Agent Parse Tests ────────────────────────────────────

class TestResearchParseOutput:
    def setup_method(self):
        self.agent = ResearchAgent(tracer=make_tracer(), client=make_mock_client())

    def test_parse_clean_json(self):
        result = self.agent.parse_final_output(json.dumps(VALID_RESEARCH_JSON))
        assert result["company_name"] == "TestCo"

    def test_parse_markdown_wrapped(self):
        text = f"```json\n{json.dumps(VALID_RESEARCH_JSON)}\n```"
        result = self.agent.parse_final_output(text)
        assert result["company_name"] == "TestCo"

    def test_parse_with_preamble(self):
        text = f"Here is the research data:\n{json.dumps(VALID_RESEARCH_JSON)}"
        result = self.agent.parse_final_output(text)
        assert result["company_name"] == "TestCo"

    def test_parse_with_trailing_text(self):
        text = f"{json.dumps(VALID_RESEARCH_JSON)}\n\nI hope this helps!"
        result = self.agent.parse_final_output(text)
        assert result["company_name"] == "TestCo"

    def test_parse_invalid_json_raises(self):
        with pytest.raises(Exception):
            self.agent.parse_final_output("not valid json at all")

    def test_parse_wrong_schema_raises(self):
        with pytest.raises(Exception):
            self.agent.parse_final_output('{"wrong": "schema"}')


# ── Financial Agent Parse Tests ───────────────────────────────────

class TestFinancialParseOutput:
    def setup_method(self):
        self.agent = FinancialAgent(tracer=make_tracer(), client=make_mock_client())

    def test_parse_clean_json(self):
        result = self.agent.parse_final_output(json.dumps(VALID_FINANCIAL_JSON))
        assert result["company_name"] == "TestCo"

    def test_parse_markdown_wrapped(self):
        text = f"```json\n{json.dumps(VALID_FINANCIAL_JSON)}\n```"
        result = self.agent.parse_final_output(text)
        assert result["company_name"] == "TestCo"

    def test_parse_with_preamble(self):
        text = f"Here is the data:\n{json.dumps(VALID_FINANCIAL_JSON)}"
        result = self.agent.parse_final_output(text)
        assert result["company_name"] == "TestCo"

    def test_parse_with_trailing_text(self):
        text = f"{json.dumps(VALID_FINANCIAL_JSON)}\nDone!"
        result = self.agent.parse_final_output(text)
        assert result["company_name"] == "TestCo"

    def test_parse_invalid_json_raises(self):
        with pytest.raises(Exception):
            self.agent.parse_final_output("this is not json")

    def test_parse_wrong_schema_raises(self):
        with pytest.raises(Exception):
            self.agent.parse_final_output('{"bad": "data"}')


# ── Agent Config Tests ────────────────────────────────────────────

class TestAgentConfig:
    def setup_method(self):
        tracer = make_tracer()
        client = make_mock_client()
        self.research = ResearchAgent(tracer=tracer, client=client)
        self.financial = FinancialAgent(tracer=tracer, client=client)
        self.risk = RiskAgent(tracer=tracer, client=client)
        self.social_media = SocialMediaAgent(tracer=tracer, client=client)

    def test_agent_names(self):
        assert self.research.AGENT_NAME == "research"
        assert self.financial.AGENT_NAME == "financial"
        assert self.risk.AGENT_NAME == "risk"
        assert self.social_media.AGENT_NAME == "social_media"

    def test_tools_have_web_search_and_fetch(self):
        for agent in [self.research, self.financial, self.risk, self.social_media]:
            tools = agent.get_tools()
            tool_names = [t["name"] for t in tools]
            assert "web_search" in tool_names
            assert "web_fetch" in tool_names

    def test_tool_schemas_valid(self):
        for agent in [self.research, self.financial, self.risk, self.social_media]:
            for tool in agent.get_tools():
                assert "name" in tool
                assert "description" in tool
                assert "input_schema" in tool
                assert tool["input_schema"]["type"] == "object"

    def test_system_prompt_not_empty(self):
        for agent in [self.research, self.financial, self.risk, self.social_media]:
            prompt = agent.get_system_prompt()
            assert len(prompt) > 100

    def test_financial_prompt_mentions_financial_terms(self):
        prompt = self.financial.get_system_prompt()
        assert "revenue" in prompt.lower()
        assert "funding" in prompt.lower()

    def test_research_prompt_mentions_research_terms(self):
        prompt = self.research.get_system_prompt()
        assert "research" in prompt.lower()

    def test_risk_prompt_mentions_risk_terms(self):
        prompt = self.risk.get_system_prompt()
        assert "risk" in prompt.lower()

    def test_social_media_prompt_mentions_social_terms(self):
        prompt = self.social_media.get_system_prompt()
        assert "social" in prompt.lower() or "sentiment" in prompt.lower()


# ── Tool Dispatch Tests ───────────────────────────────────────────

class TestToolDispatch:
    def setup_method(self):
        self.agent = ResearchAgent(tracer=make_tracer(), client=make_mock_client())

    @pytest.mark.asyncio
    async def test_web_search_dispatch(self):
        with patch("src.agents.base.web_search", new_callable=AsyncMock) as mock:
            mock.return_value = '[{"title": "test", "url": "http://test.com"}]'
            result = await self.agent.handle_tool_call("web_search", {"query": "test"})
            assert "test" in result
            mock.assert_called_once_with(query="test", max_results=5)

    @pytest.mark.asyncio
    async def test_web_fetch_dispatch(self):
        with patch("src.agents.base.web_fetch", new_callable=AsyncMock) as mock:
            mock.return_value = "Page content here"
            result = await self.agent.handle_tool_call("web_fetch", {"url": "http://test.com"})
            assert result == "Page content here"

    @pytest.mark.asyncio
    async def test_web_fetch_error_handled(self):
        with patch("src.agents.base.web_fetch", new_callable=AsyncMock) as mock:
            mock.side_effect = Exception("Connection timeout")
            result = await self.agent.handle_tool_call("web_fetch", {"url": "http://bad.com"})
            assert "error" in result.lower() or "Error" in result

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self):
        result = await self.agent.handle_tool_call("unknown_tool", {})
        assert "unknown" in result.lower() or "error" in result.lower()

    @pytest.mark.asyncio
    async def test_web_search_respects_max_results(self):
        with patch("src.agents.base.web_search", new_callable=AsyncMock) as mock:
            mock.return_value = "[]"
            await self.agent.handle_tool_call(
                "web_search", {"query": "test", "max_results": 3}
            )
            mock.assert_called_once_with(query="test", max_results=3)


# ── Research Agent Loop Tests ──────────────────────────────────────

class TestResearchAgentLoop:
    @pytest.mark.asyncio
    async def test_direct_json_response(self):
        client = make_mock_client()
        client.messages.create.return_value = _make_text_response(
            json.dumps(VALID_RESEARCH_JSON)
        )
        agent = ResearchAgent(tracer=make_tracer(), client=client)
        result = await agent.run("Research TestCo")
        assert result["status"] == "complete"
        assert result["data"]["company_name"] == "TestCo"

    @pytest.mark.asyncio
    async def test_tool_call_then_json(self):
        client = make_mock_client()
        client.messages.create.side_effect = [
            _make_tool_response("web_search", {"query": "TestCo overview"}),
            _make_text_response(json.dumps(VALID_RESEARCH_JSON)),
        ]
        agent = ResearchAgent(tracer=make_tracer(), client=client)
        with patch("src.agents.base.web_search", new_callable=AsyncMock) as mock:
            mock.return_value = '[{"title": "TestCo", "url": "http://test.com", "snippet": "info"}]'
            result = await agent.run("Research TestCo")
        assert result["status"] == "complete"
        assert result["data"]["company_name"] == "TestCo"

    @pytest.mark.asyncio
    async def test_bad_json_triggers_retry(self):
        client = make_mock_client()
        client.messages.create.side_effect = [
            _make_text_response("not valid json"),
            _make_text_response(json.dumps(VALID_RESEARCH_JSON)),
        ]
        agent = ResearchAgent(tracer=make_tracer(), client=client)
        result = await agent.run("Research TestCo")
        assert result["status"] == "complete"
        assert client.messages.create.call_count == 2

    @pytest.mark.asyncio
    async def test_llm_error_returns_failed(self):
        client = make_mock_client()
        client.messages.create.side_effect = Exception("API error")
        agent = ResearchAgent(tracer=make_tracer(), client=client)
        result = await agent.run("Research TestCo")
        assert result["status"] == "failed"
        assert "API error" in result["error_summary"]


# ── Financial Agent Loop Tests ─────────────────────────────────────

class TestFinancialAgentLoop:
    @pytest.mark.asyncio
    async def test_direct_json_response(self):
        client = make_mock_client()
        client.messages.create.return_value = _make_text_response(
            json.dumps(VALID_FINANCIAL_JSON)
        )
        agent = FinancialAgent(tracer=make_tracer(), client=client)
        result = await agent.run("Research TestCo financials")
        assert result["status"] == "complete"
        assert result["data"]["company_name"] == "TestCo"

    @pytest.mark.asyncio
    async def test_tool_call_then_json(self):
        client = make_mock_client()
        client.messages.create.side_effect = [
            _make_tool_response("web_search", {"query": "TestCo revenue"}),
            _make_text_response(json.dumps(VALID_FINANCIAL_JSON)),
        ]
        agent = FinancialAgent(tracer=make_tracer(), client=client)
        with patch("src.agents.base.web_search", new_callable=AsyncMock) as mock:
            mock.return_value = '[{"title": "TestCo Revenue", "url": "http://test.com"}]'
            result = await agent.run("Research TestCo financials")
        assert result["status"] == "complete"

    @pytest.mark.asyncio
    async def test_bad_json_triggers_retry(self):
        client = make_mock_client()
        client.messages.create.side_effect = [
            _make_text_response("garbage"),
            _make_text_response(json.dumps(VALID_FINANCIAL_JSON)),
        ]
        agent = FinancialAgent(tracer=make_tracer(), client=client)
        result = await agent.run("Research TestCo financials")
        assert result["status"] == "complete"
        assert client.messages.create.call_count == 2

    @pytest.mark.asyncio
    async def test_llm_error_returns_failed(self):
        client = make_mock_client()
        client.messages.create.side_effect = Exception("API error")
        agent = FinancialAgent(tracer=make_tracer(), client=client)
        result = await agent.run("Research TestCo financials")
        assert result["status"] == "failed"

    @pytest.mark.asyncio
    async def test_exhausted_retries_returns_partial(self):
        client = make_mock_client()
        client.messages.create.return_value = _make_text_response("bad json forever")
        agent = FinancialAgent(tracer=make_tracer(), client=client)
        result = await agent.run("Research TestCo financials")
        assert result["status"] == "partial"
        assert client.messages.create.call_count == 4  # 1 initial + 3 retries


# ── CompanySocialMedia Schema Tests ───────────────────────────────

class TestCompanySocialMedia:
    def test_full_social_media(self):
        sm = CompanySocialMedia(**VALID_SOCIAL_MEDIA_JSON)
        assert sm.company_name == "TestCo"
        assert sm.overall_sentiment.value == "positive"
        assert len(sm.positive_signals) == 1
        assert sm.positive_signals[0].value == "Industry award winner"

    def test_minimal_social_media(self):
        sm = CompanySocialMedia(
            company_name="Mini",
            overall_sentiment=make_data_point("neutral", "low"),
            sentiment_summary=make_data_point("No data", "low"),
        )
        assert sm.company_name == "Mini"
        assert sm.reddit_sentiment.value == "unknown"

    def test_social_media_from_json(self):
        raw = json.dumps(VALID_SOCIAL_MEDIA_JSON)
        parsed = json.loads(raw)
        sm = CompanySocialMedia(**parsed)
        assert sm.company_name == "TestCo"

    def test_social_media_serialization_roundtrip(self):
        sm = CompanySocialMedia(**VALID_SOCIAL_MEDIA_JSON)
        d = sm.model_dump()
        sm2 = CompanySocialMedia(**d)
        assert sm2.company_name == sm.company_name


# ── Social Media Agent Parse Tests ────────────────────────────────

class TestSocialMediaParseOutput:
    def setup_method(self):
        self.agent = SocialMediaAgent(tracer=make_tracer(), client=make_mock_client())

    def test_parse_clean_json(self):
        result = self.agent.parse_final_output(json.dumps(VALID_SOCIAL_MEDIA_JSON))
        assert result["company_name"] == "TestCo"

    def test_parse_markdown_wrapped(self):
        text = f"```json\n{json.dumps(VALID_SOCIAL_MEDIA_JSON)}\n```"
        result = self.agent.parse_final_output(text)
        assert result["company_name"] == "TestCo"

    def test_parse_with_preamble(self):
        text = f"Here is the data:\n{json.dumps(VALID_SOCIAL_MEDIA_JSON)}"
        result = self.agent.parse_final_output(text)
        assert result["company_name"] == "TestCo"

    def test_parse_with_trailing_text(self):
        text = f"{json.dumps(VALID_SOCIAL_MEDIA_JSON)}\nDone!"
        result = self.agent.parse_final_output(text)
        assert result["company_name"] == "TestCo"

    def test_parse_invalid_json_raises(self):
        with pytest.raises(Exception):
            self.agent.parse_final_output("not json")

    def test_parse_wrong_schema_raises(self):
        with pytest.raises(Exception):
            self.agent.parse_final_output('{"bad": "schema"}')


# ── Social Media Tool Dispatch Tests ──────────────────────────────

class TestSocialMediaToolDispatch:
    def setup_method(self):
        self.agent = SocialMediaAgent(tracer=make_tracer(), client=make_mock_client())

    @pytest.mark.asyncio
    async def test_web_search_dispatch(self):
        with patch("src.agents.base.web_search", new_callable=AsyncMock) as mock:
            mock.return_value = '[{"title": "test"}]'
            result = await self.agent.handle_tool_call("web_search", {"query": "test"})
            assert "test" in result

    @pytest.mark.asyncio
    async def test_web_fetch_dispatch(self):
        with patch("src.agents.base.web_fetch", new_callable=AsyncMock) as mock:
            mock.return_value = "Page content"
            result = await self.agent.handle_tool_call("web_fetch", {"url": "http://test.com"})
            assert result == "Page content"

    @pytest.mark.asyncio
    async def test_web_fetch_error_handled(self):
        with patch("src.agents.base.web_fetch", new_callable=AsyncMock) as mock:
            mock.side_effect = Exception("Timeout")
            result = await self.agent.handle_tool_call("web_fetch", {"url": "http://bad.com"})
            assert "error" in result.lower() or "Error" in result

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self):
        result = await self.agent.handle_tool_call("unknown_tool", {})
        assert "unknown" in result.lower() or "error" in result.lower()


# ── Social Media Agent Loop Tests ──────────────────────────────────

class TestSocialMediaAgentLoop:
    @pytest.mark.asyncio
    async def test_direct_json_response(self):
        client = make_mock_client()
        client.messages.create.return_value = _make_text_response(
            json.dumps(VALID_SOCIAL_MEDIA_JSON)
        )
        agent = SocialMediaAgent(tracer=make_tracer(), client=client)
        result = await agent.run("Assess TestCo social media")
        assert result["status"] == "complete"

    @pytest.mark.asyncio
    async def test_tool_call_then_json(self):
        client = make_mock_client()
        client.messages.create.side_effect = [
            _make_tool_response("web_search", {"query": "TestCo twitter"}),
            _make_text_response(json.dumps(VALID_SOCIAL_MEDIA_JSON)),
        ]
        agent = SocialMediaAgent(tracer=make_tracer(), client=client)
        with patch("src.agents.base.web_search", new_callable=AsyncMock) as mock:
            mock.return_value = '[{"title": "TestCo Twitter"}]'
            result = await agent.run("Assess TestCo social media")
        assert result["status"] == "complete"

    @pytest.mark.asyncio
    async def test_bad_json_triggers_retry(self):
        client = make_mock_client()
        client.messages.create.side_effect = [
            _make_text_response("garbage"),
            _make_text_response(json.dumps(VALID_SOCIAL_MEDIA_JSON)),
        ]
        agent = SocialMediaAgent(tracer=make_tracer(), client=client)
        result = await agent.run("Assess TestCo social media")
        assert result["status"] == "complete"

    @pytest.mark.asyncio
    async def test_llm_error_returns_failed(self):
        client = make_mock_client()
        client.messages.create.side_effect = Exception("API error")
        agent = SocialMediaAgent(tracer=make_tracer(), client=client)
        result = await agent.run("Assess TestCo social media")
        assert result["status"] == "failed"

    @pytest.mark.asyncio
    async def test_exhausted_retries_returns_partial(self):
        client = make_mock_client()
        client.messages.create.return_value = _make_text_response("bad json")
        agent = SocialMediaAgent(tracer=make_tracer(), client=client)
        result = await agent.run("Assess TestCo social media")
        assert result["status"] == "partial"


# ── CompanyRisks Schema Tests ─────────────────────────────────────

class TestCompanyRisks:
    def test_full_risks(self):
        risks = CompanyRisks(**VALID_RISK_JSON)
        assert risks.company_name == "TestCo"
        assert risks.overall_risk_rating.value == "medium"
        assert len(risks.regulatory_risks) == 1

    def test_severity_fields(self):
        risks = CompanyRisks(**VALID_RISK_JSON)
        reg = risks.regulatory_risks[0]
        assert reg.severity == SeverityLevel.MEDIUM

    def test_minimal_risks(self):
        risks = CompanyRisks(
            company_name="Mini",
            overall_risk_rating=make_data_point("low", "medium"),
            risk_summary=make_data_point("Low risk", "medium"),
        )
        assert risks.company_name == "Mini"
        assert risks.regulatory_risks == []

    def test_risks_from_json(self):
        raw = json.dumps(VALID_RISK_JSON)
        parsed = json.loads(raw)
        risks = CompanyRisks(**parsed)
        assert risks.company_name == "TestCo"

    def test_risks_serialization_roundtrip(self):
        risks = CompanyRisks(**VALID_RISK_JSON)
        d = risks.model_dump()
        risks2 = CompanyRisks(**d)
        assert risks2.company_name == risks.company_name


# ── Severity Sorting Tests ────────────────────────────────────────

class TestSeveritySorting:
    def test_sort_by_severity_order(self):
        items = [
            {"severity": "low", "confidence": "high"},
            {"severity": "critical", "confidence": "high"},
            {"severity": "medium", "confidence": "high"},
            {"severity": "high", "confidence": "high"},
        ]
        result = _sort_by_severity(items)
        assert [r["severity"] for r in result] == ["critical", "high", "medium", "low"]

    def test_sort_tiebreaker_by_confidence(self):
        items = [
            {"severity": "high", "confidence": "low"},
            {"severity": "high", "confidence": "high"},
        ]
        result = _sort_by_severity(items)
        assert result[0]["confidence"] == "high"
        assert result[1]["confidence"] == "low"

    def test_sort_empty_list(self):
        assert _sort_by_severity([]) == []

    def test_sort_missing_severity_goes_last(self):
        items = [
            {"severity": "high", "confidence": "high"},
            {"confidence": "high"},
        ]
        result = _sort_by_severity(items)
        assert result[0]["severity"] == "high"


# ── Risk Agent Parse Tests ────────────────────────────────────────

class TestRiskParseOutput:
    def setup_method(self):
        self.agent = RiskAgent(tracer=make_tracer(), client=make_mock_client())

    def test_parse_clean_json(self):
        result = self.agent.parse_final_output(json.dumps(VALID_RISK_JSON))
        assert result["company_name"] == "TestCo"

    def test_parse_markdown_wrapped(self):
        text = f"```json\n{json.dumps(VALID_RISK_JSON)}\n```"
        result = self.agent.parse_final_output(text)
        assert result["company_name"] == "TestCo"

    def test_parse_with_preamble(self):
        text = f"Here is the data:\n{json.dumps(VALID_RISK_JSON)}"
        result = self.agent.parse_final_output(text)
        assert result["company_name"] == "TestCo"

    def test_parse_with_trailing_text(self):
        text = f"{json.dumps(VALID_RISK_JSON)}\nDone!"
        result = self.agent.parse_final_output(text)
        assert result["company_name"] == "TestCo"

    def test_parse_invalid_json_raises(self):
        with pytest.raises(Exception):
            self.agent.parse_final_output("not json")

    def test_parse_wrong_schema_raises(self):
        with pytest.raises(Exception):
            self.agent.parse_final_output('{"bad": "schema"}')


# ── Risk Tool Dispatch Tests ──────────────────────────────────────

class TestRiskToolDispatch:
    def setup_method(self):
        self.agent = RiskAgent(tracer=make_tracer(), client=make_mock_client())

    @pytest.mark.asyncio
    async def test_web_search_dispatch(self):
        with patch("src.agents.base.web_search", new_callable=AsyncMock) as mock:
            mock.return_value = '[{"title": "test"}]'
            result = await self.agent.handle_tool_call("web_search", {"query": "test"})
            assert "test" in result

    @pytest.mark.asyncio
    async def test_web_fetch_dispatch(self):
        with patch("src.agents.base.web_fetch", new_callable=AsyncMock) as mock:
            mock.return_value = "Page content"
            result = await self.agent.handle_tool_call("web_fetch", {"url": "http://test.com"})
            assert result == "Page content"

    @pytest.mark.asyncio
    async def test_web_fetch_error_handled(self):
        with patch("src.agents.base.web_fetch", new_callable=AsyncMock) as mock:
            mock.side_effect = Exception("Timeout")
            result = await self.agent.handle_tool_call("web_fetch", {"url": "http://bad.com"})
            assert "error" in result.lower() or "Error" in result

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self):
        result = await self.agent.handle_tool_call("unknown_tool", {})
        assert "unknown" in result.lower() or "error" in result.lower()


# ── Risk Agent Loop Tests ──────────────────────────────────────────

class TestRiskAgentLoop:
    @pytest.mark.asyncio
    async def test_direct_json_response(self):
        client = make_mock_client()
        client.messages.create.return_value = _make_text_response(
            json.dumps(VALID_RISK_JSON)
        )
        agent = RiskAgent(tracer=make_tracer(), client=client)
        result = await agent.run("Assess TestCo risks")
        assert result["status"] == "complete"

    @pytest.mark.asyncio
    async def test_tool_call_then_json(self):
        client = make_mock_client()
        client.messages.create.side_effect = [
            _make_tool_response("web_search", {"query": "TestCo lawsuits"}),
            _make_text_response(json.dumps(VALID_RISK_JSON)),
        ]
        agent = RiskAgent(tracer=make_tracer(), client=client)
        with patch("src.agents.base.web_search", new_callable=AsyncMock) as mock:
            mock.return_value = '[{"title": "TestCo Lawsuit"}]'
            result = await agent.run("Assess TestCo risks")
        assert result["status"] == "complete"

    @pytest.mark.asyncio
    async def test_bad_json_triggers_retry(self):
        client = make_mock_client()
        client.messages.create.side_effect = [
            _make_text_response("garbage"),
            _make_text_response(json.dumps(VALID_RISK_JSON)),
        ]
        agent = RiskAgent(tracer=make_tracer(), client=client)
        result = await agent.run("Assess TestCo risks")
        assert result["status"] == "complete"

    @pytest.mark.asyncio
    async def test_llm_error_returns_failed(self):
        client = make_mock_client()
        client.messages.create.side_effect = Exception("API error")
        agent = RiskAgent(tracer=make_tracer(), client=client)
        result = await agent.run("Assess TestCo risks")
        assert result["status"] == "failed"

    @pytest.mark.asyncio
    async def test_exhausted_retries_returns_partial(self):
        client = make_mock_client()
        client.messages.create.return_value = _make_text_response("bad json")
        agent = RiskAgent(tracer=make_tracer(), client=client)
        result = await agent.run("Assess TestCo risks")
        assert result["status"] == "partial"


# ── Report Confidence Score Tests ──────────────────────────────────

class TestReportConfidenceScore:
    def test_section_confidence_all_high(self):
        data = {"revenue": make_data_point("$10M", "high"), "growth": make_data_point("50%", "high")}
        assert _compute_section_confidence(data) == pytest.approx(1.0)

    def test_section_confidence_all_unknown(self):
        data = {"revenue": make_data_point("?", "unknown"), "growth": make_data_point("?", "unknown")}
        assert _compute_section_confidence(data) == pytest.approx(0.0)

    def test_section_confidence_mixed(self):
        data = {"a": make_data_point("x", "high"), "b": make_data_point("x", "low")}
        score = _compute_section_confidence(data)
        assert 0.0 < score < 1.0

    def test_section_confidence_includes_list_items(self):
        data = {
            "name": make_data_point("x", "high"),
            "items": [make_data_point("a", "high"), make_data_point("b", "low")],
        }
        score = _compute_section_confidence(data)
        assert 0.0 < score < 1.0

    def test_section_confidence_empty_data(self):
        assert _compute_section_confidence({}) == pytest.approx(0.0)

    def test_report_confidence_weighted(self):
        fin = {"company_name": "T", "revenue": make_data_point("$10M", "high")}
        risk = {"company_name": "T", "overall_risk_rating": make_data_point("low", "high")}
        social = {"company_name": "T", "overall_sentiment": make_data_point("positive", "high")}
        score = _compute_report_confidence(fin, risk, social)
        assert score == pytest.approx(1.0)

    def test_report_confidence_partial_sections(self):
        fin = {"company_name": "T", "revenue": make_data_point("$10M", "high")}
        score = _compute_report_confidence(fin, None, None)
        assert score == pytest.approx(1.0)

    def test_report_confidence_none_when_no_data(self):
        assert _compute_report_confidence(None, None, None) is None

    def test_report_confidence_weights_applied(self):
        fin = {"company_name": "T", "revenue": make_data_point("$10M", "high")}
        risk = {"company_name": "T", "overall_risk_rating": make_data_point("low", "unknown")}
        social = {"company_name": "T", "overall_sentiment": make_data_point("positive", "high")}
        score = _compute_report_confidence(fin, risk, social)
        # (1.0 * 0.40 + 0.0 * 0.40 + 1.0 * 0.20) / (0.40 + 0.40 + 0.20) = 0.60
        assert score == pytest.approx(0.60)


# ── Soft Budget Tests ──────────────────────────────────────────────

class TestSoftBudget:
    @pytest.mark.asyncio
    async def test_budget_exhausted_forces_partial_with_valid_data(self):
        """Budget=0 expires before turn 0; tools are omitted, result is partial with non-null data."""
        client = make_mock_client()
        client.messages.create.return_value = _make_text_response(
            json.dumps(VALID_RESEARCH_JSON)
        )
        agent = ResearchAgent(tracer=make_tracer(), client=client)
        agent.soft_budget_seconds = 0  # elapsed >= 0 is always True at turn 0

        result = await agent.run("Research TestCo")

        assert result["status"] == "partial"
        assert result["data"] is not None, "data must be non-null even on budget-forced partial"
        assert result["data"]["company_name"] == "TestCo"
        assert any("budget" in g.lower() for g in result["gaps"])

        # The LLM call must not include tools when budget is exhausted
        call_kwargs = client.messages.create.call_args.kwargs
        assert "tools" not in call_kwargs, "tools must be omitted when budget is exhausted"

    @pytest.mark.asyncio
    async def test_max_tokens_truncation_is_sticky_and_recovers(self):
        """A max_tokens-truncated turn must force budget exhaustion that STICKS:
        the retry uses max_tokens=8192 and omits tools, breaking the 4096 storm.

        Regression for the retry storm: previously _budget_exhausted was recomputed
        from the soft-budget timer at the top of every turn, clobbering the True set
        by the max_tokens handler, so every retry re-truncated at 4096 until timeout.
        """
        client = make_mock_client()
        # Turn 0: truncated, unparseable JSON (cut off by max_tokens).
        # Turn 1 (retry): full valid JSON.
        client.messages.create.side_effect = [
            _make_truncated_response('{"company_name": "TestCo", "descrip'),
            _make_text_response(json.dumps(VALID_RESEARCH_JSON)),
        ]
        agent = ResearchAgent(tracer=make_tracer(), client=client)
        # No soft budget — exhaustion here must come solely from the max_tokens hit.
        agent.soft_budget_seconds = None

        result = await agent.run("Research TestCo")

        # Recovered with valid data on the retry.
        assert result["data"] is not None
        assert result["data"]["company_name"] == "TestCo"

        # Two LLM calls were made; the SECOND must carry the sticky budget:
        # max_tokens bumped to 8192 and tools omitted.
        assert client.messages.create.call_count == 2
        retry_kwargs = client.messages.create.call_args_list[1].kwargs
        assert retry_kwargs["max_tokens"] == 8192, (
            "retry after max_tokens truncation must request 8192 tokens, not 4096"
        )
        assert "tools" not in retry_kwargs, (
            "retry after max_tokens truncation must omit tools"
        )

    @pytest.mark.asyncio
    async def test_status_reflects_terminal_outcome_not_budget_history(self):
        """Status must derive from the terminal outcome, not from whether any
        intermediate turn was budget-forced.

          • max_tokens bump → recovered to a clean end_turn emit  → COMPLETE
          • soft-budget cutoff (run halted mid-work)              → PARTIAL

        Before the fix, _budget_forced was set on ANY budget exhaustion (including
        the max_tokens stickiness), so the recovered run was mislabeled partial.
        This test fails before the fix (recovery returns partial) and passes after.
        """
        # ── Arm 1: max_tokens bump then clean recovery → complete ──────────────
        client = make_mock_client()
        client.messages.create.side_effect = [
            _make_truncated_response('{"company_name": "TestCo", "descrip'),
            _make_text_response(json.dumps(VALID_RESEARCH_JSON)),
        ]
        agent = ResearchAgent(tracer=make_tracer(), client=client)
        agent.soft_budget_seconds = None  # only the max_tokens bump occurred

        recovered = await agent.run("Research TestCo")
        assert recovered["status"] == "complete", (
            "a max_tokens bump that recovered to a clean terminal emit is complete, "
            "not partial — the mere presence of a bump must not imply partial"
        )
        assert recovered["data"]["company_name"] == "TestCo"
        assert recovered["gaps"] == [], (
            "a recovered-complete run must not carry a budget-reached gap"
        )

        # ── Arm 2: soft-budget cutoff (no clean mid-work completion) → partial ──
        client2 = make_mock_client()
        client2.messages.create.return_value = _make_text_response(
            json.dumps(VALID_RESEARCH_JSON)
        )
        agent2 = ResearchAgent(tracer=make_tracer(), client=client2)
        agent2.soft_budget_seconds = 0  # timer fires at turn 0 → genuine cutoff

        cutoff = await agent2.run("Research TestCo")
        assert cutoff["status"] == "partial", (
            "a soft-budget cutoff halts the run mid-work and must stay partial"
        )
        assert any("budget" in g.lower() for g in cutoff["gaps"])

    @pytest.mark.asyncio
    async def test_no_budget_completes_normally(self):
        """Without soft_budget_seconds, a successful run returns status=complete."""
        client = make_mock_client()
        client.messages.create.return_value = _make_text_response(
            json.dumps(VALID_RESEARCH_JSON)
        )
        agent = ResearchAgent(tracer=make_tracer(), client=client)
        # soft_budget_seconds defaults to None

        result = await agent.run("Research TestCo")

        assert result["status"] == "complete"
        assert result["gaps"] == []

        # Tools must be included when budget is not set
        call_kwargs = client.messages.create.call_args.kwargs
        assert "tools" in call_kwargs


# ── Web Search Volume Cap Tests ────────────────────────────────────

class TestWebSearchVolumeCaps:
    def setup_method(self):
        self.agent = FinancialAgent(tracer=make_tracer(), client=make_mock_client())

    @pytest.mark.asyncio
    async def test_web_search_caps_above_max(self):
        """Requests for more than MAX_SEARCH_RESULTS are silently capped."""
        with patch("src.agents.base.web_search", new_callable=AsyncMock) as mock:
            mock.return_value = "[]"
            await self.agent.handle_tool_call(
                "web_search", {"query": "test", "max_results": 100}
            )
            mock.assert_called_once_with(
                query="test", max_results=self.agent.MAX_SEARCH_RESULTS
            )

    @pytest.mark.asyncio
    async def test_web_search_does_not_inflate_below_cap(self):
        """Requests below MAX_SEARCH_RESULTS are passed through unchanged."""
        with patch("src.agents.base.web_search", new_callable=AsyncMock) as mock:
            mock.return_value = "[]"
            await self.agent.handle_tool_call(
                "web_search", {"query": "test", "max_results": 2}
            )
            mock.assert_called_once_with(query="test", max_results=2)

    @pytest.mark.asyncio
    async def test_web_fetch_blocked_after_max_fetches(self):
        """web_fetch calls beyond MAX_FETCHES return a budget-exhausted error, not an exception."""
        with patch("src.agents.base.web_fetch", new_callable=AsyncMock) as mock:
            mock.return_value = "page content"
            for _ in range(self.agent.MAX_FETCHES):
                result = await self.agent.handle_tool_call(
                    "web_fetch", {"url": "http://test.com"}
                )
                assert result == "page content"
            # The (MAX_FETCHES+1)th call must be blocked
            blocked = await self.agent.handle_tool_call(
                "web_fetch", {"url": "http://test.com"}
            )
            blocked_data = json.loads(blocked)
            assert "error" in blocked_data
            assert "exhausted" in blocked_data["error"].lower()
            assert mock.call_count == self.agent.MAX_FETCHES

    @pytest.mark.asyncio
    async def test_fetch_count_is_zero_on_new_agent(self):
        """Each new agent instance starts with a fresh fetch counter."""
        a1 = FinancialAgent(tracer=make_tracer(), client=make_mock_client())
        a2 = FinancialAgent(tracer=make_tracer(), client=make_mock_client())
        assert a1._fetch_count == 0
        assert a2._fetch_count == 0


# ── AgentDB Tests ─────────────────────────────────────────────────

class TestAgentDB:
    def setup_method(self):
        self.db = AgentDB()

    def test_start_and_end_run(self):
        self.db.start_run("research", "Research TestCo", trace_id="t1")
        self.db.end_run(
            trace_id="t1", agent="research",
            status="complete", total_turns=3, total_tool_calls=2,
            total_input_tokens=500, total_output_tokens=200, total_cost_usd=0.01,
            result_json='{"company_name": "TestCo"}',
        )
        runs = self.db.get_runs("research")
        assert len(runs) == 1
        assert runs[0]["status"] == "complete"
        assert runs[0]["total_turns"] == 3
        assert runs[0]["total_duration_ms"] > 0

    def test_log_llm_call(self):
        self.db.log_llm_call(
            agent="research", turn=0, model="claude-sonnet-4-20250514",
            system_prompt="You are a research agent.",
            request_messages=[{"role": "user", "content": "Research TestCo"}],
            response_content='[{"type": "text", "text": "Hello"}]',
            input_tokens=100, output_tokens=50, cost_usd=0.001, duration_ms=500,
            stop_reason="end_turn", trace_id="t1",
        )
        calls = self.db.get_llm_calls("research")
        assert len(calls) == 1
        assert calls[0]["turn"] == 0
        assert calls[0]["input_tokens"] == 100
        assert calls[0]["stop_reason"] == "end_turn"
        assert calls[0]["trace_id"] == "t1"

    def test_log_tool_call(self):
        self.db.log_tool_call(
            agent="research", turn=0,
            tool_name="web_search", tool_input={"query": "TestCo"},
            tool_result='[{"title": "TestCo"}]', duration_ms=200,
            trace_id="t1",
        )
        tools = self.db.get_tool_calls("research")
        assert len(tools) == 1
        assert tools[0]["tool_name"] == "web_search"
        assert tools[0]["turn"] == 0
        assert tools[0]["trace_id"] == "t1"

    def test_log_error_in_llm_call(self):
        self.db.log_llm_call(
            agent="research", turn=0, model="test",
            system_prompt="test", request_messages=[],
            error="API rate limit",
        )
        calls = self.db.get_llm_calls("research")
        assert calls[0]["error"] == "API rate limit"

    def test_get_run_detail(self):
        self.db.start_run("research", "Research TestCo", trace_id="t1")
        self.db.log_llm_call(
            agent="research", turn=0, model="test",
            system_prompt="test", request_messages=[],
            input_tokens=100, output_tokens=50, trace_id="t1",
        )
        self.db.log_tool_call(
            agent="research", turn=0,
            tool_name="web_search", tool_input={"query": "TestCo"},
            trace_id="t1",
        )
        self.db.end_run(
            trace_id="t1", agent="research",
            status="complete", total_turns=1, total_tool_calls=1,
            total_input_tokens=100, total_output_tokens=50, total_cost_usd=0.001,
        )
        detail = self.db.get_run_detail(trace_id="t1", agent="research")
        assert detail["status"] == "complete"
        assert len(detail["llm_calls"]) == 1
        assert len(detail["tool_calls"]) == 1

    def test_multiple_agents_isolated(self):
        self.db.start_run("research", "task1", trace_id="t1")
        self.db.start_run("financial", "task2", trace_id="t1")
        self.db.log_llm_call(agent="research", turn=0, model="t", system_prompt="", request_messages=[], trace_id="t1")
        self.db.log_llm_call(agent="financial", turn=0, model="t", system_prompt="", request_messages=[], trace_id="t1")
        assert len(self.db.get_llm_calls("research")) == 1
        assert len(self.db.get_llm_calls("financial")) == 1
        assert len(self.db.get_llm_calls()) == 2

    @pytest.mark.asyncio
    async def test_research_agent_with_db(self):
        """Full integration: research agent logs to DB on successful run."""
        db = AgentDB()
        client = make_mock_client()
        client.messages.create.side_effect = [
            _make_tool_response("web_search", {"query": "TestCo overview"}),
            _make_text_response(json.dumps(VALID_RESEARCH_JSON)),
        ]

        agent = ResearchAgent(tracer=make_tracer(), client=client, db=db)
        with patch("src.agents.base.web_search", new_callable=AsyncMock) as mock:
            mock.return_value = '[{"title":"TestCo","url":"http://x","snippet":"info"}]'
            result = await agent.run("Research TestCo")

        assert result["status"] == "complete"
        runs = db.get_runs("research")
        assert len(runs) == 1
        assert runs[0]["status"] == "complete"
        assert runs[0]["total_turns"] == 2
        assert runs[0]["total_tool_calls"] == 1

        llm_calls = db.get_llm_calls("research")
        assert len(llm_calls) == 2

        tool_calls = db.get_tool_calls("research")
        assert len(tool_calls) == 1
        assert tool_calls[0]["tool_name"] == "web_search"

    @pytest.mark.asyncio
    async def test_research_agent_db_logs_failure(self):
        """DB logs failed runs correctly."""
        db = AgentDB()
        client = make_mock_client()
        client.messages.create.side_effect = Exception("API error")

        agent = ResearchAgent(tracer=make_tracer(), client=client, db=db)
        result = await agent.run("Research TestCo")

        assert result["status"] == "failed"
        runs = db.get_runs("research")
        assert len(runs) == 1
        assert runs[0]["status"] == "failed"
        assert "API error" in runs[0]["error"]

        llm_calls = db.get_llm_calls("research")
        assert len(llm_calls) == 1
        assert "API error" in llm_calls[0]["error"]

    def test_messages_table_created(self):
        """Messages table exists and is empty initially."""
        msgs = self.db.get_messages()
        assert msgs == []

    def test_log_message(self):
        """log_message stores and retrieves messages."""
        self.db.start_run("research", "task", trace_id="t1")
        self.db.log_message("t1", "research", 0, "user", "Research TestCo")
        self.db.log_message("t1", "research", 1, "assistant", '{"text": "result"}', tokens=50)
        self.db.log_message("t1", "research", 2, "tool_result", "[web_search] some data")

        msgs = self.db.get_messages(trace_id="t1", agent="research")
        assert len(msgs) == 3
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "Research TestCo"
        assert msgs[1]["role"] == "assistant"
        assert msgs[1]["tokens"] == 50
        assert msgs[2]["role"] == "tool_result"
        assert msgs[2]["sequence_number"] == 2

    def test_log_message_truncates_content(self):
        """Messages longer than 2000 chars are truncated."""
        self.db.start_run("research", "task", trace_id="t1")
        long_content = "x" * 5000
        self.db.log_message("t1", "research", 0, "user", long_content)
        msgs = self.db.get_messages(trace_id="t1", agent="research")
        assert len(msgs[0]["content"]) == 2000

    def test_get_messages_by_agent(self):
        """get_messages filters by agent."""
        self.db.start_run("research", "task1", trace_id="t1")
        self.db.start_run("financial", "task2", trace_id="t1")
        self.db.log_message("t1", "research", 0, "user", "msg1")
        self.db.log_message("t1", "financial", 0, "user", "msg2")
        assert len(self.db.get_messages(agent="research")) == 1
        assert len(self.db.get_messages(agent="financial")) == 1
        assert len(self.db.get_messages()) == 2

    def test_company_name_in_runs(self):
        """agent_runs stores company_name."""
        self.db.start_run("research", "task", company_name="TestCo", trace_id="t1")
        runs = self.db.get_runs()
        assert runs[0]["company_name"] == "TestCo"

    def test_run_detail_includes_messages(self):
        """get_run_detail includes messages."""
        self.db.start_run("research", "task", trace_id="t1")
        self.db.log_message("t1", "research", 0, "user", "hi")
        self.db.end_run(trace_id="t1", agent="research", status="complete",
                        total_turns=1, total_tool_calls=0,
                        total_input_tokens=0, total_output_tokens=0,
                        total_cost_usd=0)
        detail = self.db.get_run_detail(trace_id="t1", agent="research")
        assert len(detail["messages"]) == 1
        assert detail["messages"][0]["content"] == "hi"

    @pytest.mark.asyncio
    async def test_research_agent_logs_messages(self):
        """Full integration: research agent logs conversation messages."""
        db = AgentDB()
        client = make_mock_client()
        client.messages.create.side_effect = [
            _make_tool_response("web_search", {"query": "TestCo overview"}),
            _make_text_response(json.dumps(VALID_RESEARCH_JSON)),
        ]

        tracer = make_tracer()
        agent = ResearchAgent(tracer=tracer, client=client, db=db)
        with patch("src.agents.base.web_search", new_callable=AsyncMock) as mock:
            mock.return_value = '[{"title":"TestCo","url":"http://x","snippet":"info"}]'
            result = await agent.run("Research TestCo")

        assert result["status"] == "complete"
        msgs = db.get_messages(trace_id=tracer.run_id, agent="research")
        # Expect: user task, assistant (tool_use), tool_result, assistant (final)
        assert len(msgs) == 4
        assert msgs[0]["role"] == "user"
        assert "Research TestCo" in msgs[0]["content"]
        assert msgs[1]["role"] == "assistant"
        assert msgs[2]["role"] == "tool_result"
        assert "[web_search]" in msgs[2]["content"]
        assert msgs[3]["role"] == "assistant"


# ── _strip_json Tests ─────────────────────────────────────────────

class TestStripJson:
    def setup_method(self):
        # Any concrete agent works; we're testing the BaseAgent helper
        self.agent = ResearchAgent(tracer=make_tracer(), client=make_mock_client())

    def test_clean_json_returned_unchanged(self):
        text = '{"key": "value"}'
        assert self.agent._strip_json(text) == '{"key": "value"}'

    def test_strips_json_code_fence(self):
        text = '```json\n{"key": "value"}\n```'
        assert self.agent._strip_json(text) == '{"key": "value"}'

    def test_strips_plain_code_fence(self):
        text = '```\n{"key": "value"}\n```'
        assert self.agent._strip_json(text) == '{"key": "value"}'

    def test_strips_preamble_before_brace(self):
        text = 'Here is the JSON:\n{"key": "value"}'
        assert self.agent._strip_json(text) == '{"key": "value"}'

    def test_strips_trailing_text_after_brace(self):
        text = '{"key": "value"}\nI hope that helps!'
        assert self.agent._strip_json(text) == '{"key": "value"}'

    def test_strips_preamble_and_trailing(self):
        text = 'Here:\n{"key": "value"}\nDone.'
        assert self.agent._strip_json(text) == '{"key": "value"}'

    def test_result_is_valid_json(self):
        text = '```json\n{"a": 1, "b": [1, 2, 3]}\n```'
        result = self.agent._strip_json(text)
        parsed = json.loads(result)
        assert parsed == {"a": 1, "b": [1, 2, 3]}

    def test_no_brace_raises_value_error(self):
        with pytest.raises(ValueError, match="No JSON object found"):
            self.agent._strip_json("no json here at all")

    def test_no_closing_brace_raises_value_error(self):
        with pytest.raises(ValueError, match="No closing brace"):
            self.agent._strip_json('{"key": "value"')


# ── company_context / _format_context Tests ───────────────────────

class TestCompanyContext:
    def setup_method(self):
        self.tracer = make_tracer()
        self.client = make_mock_client()
        self.full_ctx = {
            "sector": "Fintech / Payment Processing",
            "company_type": "private",
            "business_model": "B2B",
            "primary_region": "United States",
            "key_context": "Subject to PCI-DSS and CFPB oversight.",
        }

    def test_no_context_contains_current_date(self):
        agent = ResearchAgent(tracer=self.tracer, client=self.client)
        block = agent._format_context()
        assert "CURRENT DATE" in block
        assert "COMPANY CONTEXT" not in block

    def test_none_context_contains_current_date(self):
        agent = ResearchAgent(tracer=self.tracer, client=self.client, company_context=None)
        block = agent._format_context()
        assert "CURRENT DATE" in block
        assert "COMPANY CONTEXT" not in block

    def test_empty_dict_contains_current_date(self):
        agent = ResearchAgent(tracer=self.tracer, client=self.client, company_context={})
        block = agent._format_context()
        assert "CURRENT DATE" in block
        assert "COMPANY CONTEXT" not in block

    def test_full_context_contains_all_fields(self):
        agent = ResearchAgent(tracer=self.tracer, client=self.client, company_context=self.full_ctx)
        block = agent._format_context()
        assert "Fintech / Payment Processing" in block
        assert "private" in block
        assert "B2B" in block
        assert "United States" in block
        assert "PCI-DSS" in block

    def test_full_context_has_header(self):
        agent = ResearchAgent(tracer=self.tracer, client=self.client, company_context=self.full_ctx)
        assert "COMPANY CONTEXT" in agent._format_context()

    def test_partial_context_skips_missing_fields(self):
        agent = ResearchAgent(tracer=self.tracer, client=self.client, company_context={"sector": "SaaS"})
        block = agent._format_context()
        assert "SaaS" in block
        assert "company_type" not in block
        assert "primary_region" not in block

    def test_context_injected_into_system_prompt(self):
        agent = ResearchAgent(tracer=self.tracer, client=self.client, company_context=self.full_ctx)
        prompt = agent.get_system_prompt()
        assert "COMPANY CONTEXT" in prompt
        assert "Fintech" in prompt

    def test_no_context_block_in_prompt_when_absent(self):
        agent = ResearchAgent(tracer=self.tracer, client=self.client)
        assert "COMPANY CONTEXT" not in agent.get_system_prompt()

    def test_all_four_agents_accept_and_expose_context(self):
        for AgentClass in [ResearchAgent, FinancialAgent, RiskAgent, SocialMediaAgent]:
            agent = AgentClass(
                tracer=self.tracer, client=self.client, company_context=self.full_ctx
            )
            assert agent.company_context == self.full_ctx
            assert "COMPANY CONTEXT" in agent._format_context()
            assert "COMPANY CONTEXT" in agent.get_system_prompt(), \
                f"{AgentClass.AGENT_NAME} missing context in system prompt"


# ── classify_company Tests ────────────────────────────────────────

class TestClassifyCompany:
    @pytest.mark.asyncio
    async def test_successful_classification_returns_dict(self):
        payload = {
            "sector": "Fintech",
            "company_type": "private",
            "business_model": "B2B",
            "primary_region": "United States",
            "key_context": "Payment infrastructure.",
        }
        client = make_mock_client()
        client.messages.create.return_value = _make_text_response(json.dumps(payload))

        result = await classify_company("Stripe", client, make_tracer())

        assert result is not None
        assert result["sector"] == "Fintech"
        assert result["company_type"] == "private"

    @pytest.mark.asyncio
    async def test_api_error_returns_none(self):
        client = make_mock_client()
        client.messages.create.side_effect = Exception("API error")

        result = await classify_company("Stripe", client, make_tracer())

        assert result is None

    @pytest.mark.asyncio
    async def test_malformed_json_returns_none(self):
        client = make_mock_client()
        client.messages.create.return_value = _make_text_response("not valid json {{{")

        result = await classify_company("Stripe", client, make_tracer())

        assert result is None

    @pytest.mark.asyncio
    async def test_strips_markdown_fences(self):
        payload = {"sector": "SaaS", "company_type": "public"}
        client = make_mock_client()
        client.messages.create.return_value = _make_text_response(
            f"```json\n{json.dumps(payload)}\n```"
        )

        result = await classify_company("TestCo", client, make_tracer())

        assert result is not None
        assert result["sector"] == "SaaS"

    @pytest.mark.asyncio
    async def test_creates_span_in_tracer(self):
        client = make_mock_client()
        client.messages.create.return_value = _make_text_response('{"sector": "Tech"}')
        tracer = make_tracer()

        await classify_company("TestCo", client, tracer)

        assert len(tracer.spans) == 1
        assert tracer.spans[0].agent == "classifier"
        assert tracer.spans[0].span_type == "llm_call"

    @pytest.mark.asyncio
    async def test_uses_haiku_model(self):
        client = make_mock_client()
        client.messages.create.return_value = _make_text_response('{"sector": "Tech"}')

        await classify_company("TestCo", client, make_tracer())

        model = client.messages.create.call_args.kwargs["model"]
        assert "haiku" in model
