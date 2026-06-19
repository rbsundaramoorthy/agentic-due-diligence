"""
Company pre-classifier — single LLM call before the parallel agent phase.

Returns a small context dict (sector, company_type, business_model,
primary_region, key_context) that is injected into each agent's system
prompt so they can write targeted searches from the start rather than
re-deriving the company's basic profile through trial and error.

Uses Haiku for cost efficiency (~$0.001 per call). Returns None on any
failure so agents fall back gracefully to the current behavior.
"""

import json
from typing import Optional

import anthropic

from src.agents.base import strip_json
from src.observability.tracer import AgentTracer


_MODEL = "claude-haiku-4-5-20251001"

_SYSTEM = (
    "You are a company classifier for a due diligence research system. "
    "Given a company name, return ONLY a JSON object with these fields:\n"
    "- sector: the industry/sector (e.g. 'Fintech / Payment Processing')\n"
    "- company_type: 'public', 'private', 'nonprofit', or 'government'\n"
    "- business_model: 'B2B', 'B2C', 'marketplace', 'dual', or 'other'\n"
    "- primary_region: primary country or region (e.g. 'United States', 'Global')\n"
    "- is_likely_public: true if this company is likely SEC-reporting (US-listed public "
    "company that files 10-K/10-Q with the SEC); false for private companies, non-US "
    "public companies, nonprofits, and subsidiaries of foreign parents\n"
    "- is_government_contractor: true if this company is known or likely to hold US "
    "federal contracts (defense, IT services, consulting, infrastructure firms); false "
    "for consumer-focused or companies with no obvious federal business\n"
    "- legal_name: the company's full SEC-registered legal name when it differs from the "
    "brand name given (e.g. 'Space Exploration Technologies Corp' for 'SpaceX', "
    "'Alphabet Inc.' for 'Google', 'Meta Platforms Inc.' for 'Facebook'). "
    "Use null if the input name IS already the legal name, or if the legal name is unknown.\n"
    "- ticker: the US stock exchange ticker symbol if known (e.g. 'AAPL', 'GOOGL'). "
    "Use null for private companies or when the ticker is unknown.\n"
    "- key_context: one sentence — the single most important fact a due diligence "
    "analyst should know about this company's operating context (regulatory environment, "
    "market position, or business model nuance)\n\n"
    "No markdown, no backticks, no explanation — raw JSON only."
)


async def classify_company(
    company_name: str,
    client: anthropic.AsyncAnthropic,
    tracer: AgentTracer,
) -> Optional[dict]:
    """Pre-classify a company with a single cheap LLM call.

    Returns a dict on success, None on any failure. Callers must handle
    None gracefully — agents work fine without pre-classification.
    """
    span = tracer.start_span(
        name="classify_company",
        agent="classifier",
        span_type="llm_call",
    )
    try:
        response = await client.messages.create(
            model=_MODEL,
            max_tokens=512,
            system=_SYSTEM,
            messages=[{"role": "user", "content": f"Classify this company: {company_name}"}],
        )
        tracer.end_span(
            span,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            model=_MODEL,
            prompt_version="1.0",
        )
        return json.loads(strip_json(response.content[0].text))
    except Exception as e:
        tracer.end_span(span, error=str(e))
        return None
