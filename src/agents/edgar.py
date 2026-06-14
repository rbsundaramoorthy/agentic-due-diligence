"""
EDGAR Agent — extracts financial data and risk factors from SEC filings.

Runs in Phase 1 parallel alongside Research, Financial, Risk, and Social Media.
For US public (SEC-reporting) companies: retrieves XBRL financial facts from
the companyfacts API and risk factors from the 10-K text.
For private or non-US companies: returns immediately with
edgar_lookup_status=NOT_SEC_REPORTING — this is expected, not an error.

MAX_TURNS = 5: workflow is deterministic enough that 3 tool calls suffice.
  Turn 0: edgar_find_company → get CIK or confirm not-SEC-reporting
  Turn 1: edgar_get_financials → revenue + net income
  Turn 2: edgar_get_filing_text → risk factor text
  Turn 3: LLM synthesizes → structured JSON output
"""

import json
from typing import Optional

import anthropic

from src.agents.base import BaseAgent
from src.observability.agent_db import AgentDB
from src.observability.tracer import AgentTracer
from src.schemas.models import (
    CompanyEdgarFinancials,
    ConfidenceLevel,
    DataPoint,
    EdgarLookupStatus,
)
from src.sources.cache import SourceCache
from src.sources.edgar import (
    edgar_find_company,
    edgar_get_filing_text,
    edgar_get_financials,
)


_EDGAR_FIND_TOOL = {
    "name": "edgar_find_company",
    "description": (
        "Search SEC EDGAR for a company by CIK. Resolution cascade (use in order): "
        "(a) ticker → direct CIK lookup (fastest, no name matching); "
        "(b) legal_name → EFTS search using the SEC-registered legal name (bypasses "
        "brand-name / registered-name mismatch); "
        "(c) name alone (fallback, may fail for brand names like 'SpaceX'). "
        "Always pass ticker and legal_name from COMPANY CONTEXT when available. "
        "Searches 10-K first; falls back to S-1/424B for recent IPO registrants. "
        "Returns {found, cik, company_name, most_recent_filing, accession_no, "
        "filing_type (10-K|S-1/A|424B4|...), name_match, note}."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Company brand or display name (always required)",
            },
            "ticker": {
                "type": "string",
                "description": (
                    "US stock ticker symbol if known from COMPANY CONTEXT "
                    "(e.g. 'SPCX'). Enables direct CIK lookup via company_tickers.json."
                ),
            },
            "legal_name": {
                "type": "string",
                "description": (
                    "Full SEC-registered legal name from COMPANY CONTEXT when it "
                    "differs from the brand name (e.g. 'Space Exploration Technologies "
                    "Corp' for 'SpaceX'). Ensures filer-verification passes."
                ),
            },
        },
        "required": ["name"],
    },
}

_EDGAR_FINANCIALS_TOOL = {
    "name": "edgar_get_financials",
    "description": (
        "Fetch annual financial facts (revenue, net income) from EDGAR companyfacts. "
        "Call this after edgar_find_company returns found=true. "
        "Returns xbrl_available (bool), revenue, net_income, fiscal_year, source_url. "
        "For brand-new IPO registrants, xbrl_available=false and revenue/net_income "
        "will be null — this is expected, not an error. Gap the financials in that case."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "cik": {"type": "string", "description": "CIK from edgar_find_company"},
        },
        "required": ["cik"],
    },
}

_EDGAR_FILING_TEXT_TOOL = {
    "name": "edgar_get_filing_text",
    "description": (
        "Fetch text from a specific section of an EDGAR filing (10-K, S-1, or 424B). "
        "Use 'risk_factors' to extract risk factor disclosures. "
        "For accession_number: use revenue.accession_no from edgar_get_financials if "
        "available; otherwise use accession_no from edgar_find_company. "
        "Returns section text, source_url, and filing_form_type (10-K|S-1/A|424B4|...)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "cik": {"type": "string", "description": "Company CIK"},
            "accession_number": {
                "type": "string",
                "description": (
                    "Accession number — prefer revenue.accession_no from "
                    "edgar_get_financials; fall back to accession_no from "
                    "edgar_find_company when financials are gapped."
                ),
            },
            "section": {
                "type": "string",
                "enum": ["risk_factors", "business", "mda"],
                "description": "Section to extract",
            },
        },
        "required": ["cik", "accession_number", "section"],
    },
}

_OUTPUT_SCHEMA = """{
  "company_name": "string",
  "cik": "string or null",
  "is_sec_reporting": true,
  "edgar_lookup_status": "succeeded|not_sec_reporting|lookup_failed|rate_limited",
  "revenue": {
    "_claim_id": "edgar_rev — assign if another claim will reference this in derived_from",
    "value": "$4.2B (FY2024)",
    "confidence": "high",
    "sources": ["https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"],
    "derived": false,
    "derived_from": [],
    "reasoning": "From us-gaap.Revenues, FY2024 10-K"
  },
  "profitability": {
    "value": "Net income $1.1B (FY2024)",
    "confidence": "high",
    "sources": ["https://data.sec.gov/api/xbrl/companyfacts/CIK..."],
    "derived": false,
    "derived_from": [],
    "reasoning": "optional — if you compute a margin from revenue + net income, set derived=true and cite both claim_ids"
  },
  "fiscal_year_end": {
    "value": "September 28, 2024",
    "confidence": "high",
    "sources": ["https://data.sec.gov/..."],
    "derived": false,
    "derived_from": []
  },
  "most_recent_filing": {
    "value": "10-K filed 2024-11-01",
    "confidence": "high",
    "sources": ["https://www.sec.gov/Archives/..."],
    "derived": false,
    "derived_from": []
  },
  "sec_risk_factors": [
    {
      "value": "one material risk factor, summarized in 1-2 sentences",
      "confidence": "high",
      "sources": ["https://www.sec.gov/Archives/edgar/data/.../10k.htm"],
      "derived": false,
      "derived_from": [],
      "reasoning": "optional"
    }
  ]
}"""


class EdgarAgent(BaseAgent):
    AGENT_NAME = "edgar"
    PROMPT_VERSION = "1.5"
    MAX_TURNS = 5

    def __init__(
        self,
        tracer: AgentTracer,
        client: anthropic.Anthropic,
        db: Optional[AgentDB] = None,
        company_context: Optional[dict] = None,
        cache: Optional[SourceCache] = None,
    ):
        super().__init__(
            tracer=tracer, client=client, db=db, company_context=company_context
        )
        self.cache = cache

    def get_tools(self) -> list:
        return [_EDGAR_FIND_TOOL, _EDGAR_FINANCIALS_TOOL, _EDGAR_FILING_TEXT_TOOL]

    def get_system_prompt(self) -> str:
        return f"""You are an EDGAR Agent performing SEC filing research for company due diligence.

Your job is to look up a company in SEC EDGAR and extract financial facts and
risk factors from its most recent SEC filing (10-K annual report or, for
companies that recently IPO'd, S-1/424B prospectus).
{self._format_context()}
WORKFLOW (follow in order, TOOL CALL BUDGET: 3 tool calls maximum):

Step 1 — Call edgar_find_company to look up the company's CIK.
  ALWAYS call this tool — never skip it based on company_type or is_likely_public.
  The live EDGAR result is the authoritative source for SEC-reporting status.

  Name resolution (use the best available identifier from COMPANY CONTEXT):
  • Pass ticker from "Stock ticker" context if present (enables direct CIK lookup).
  • Pass legal_name from "SEC-registered legal name" context if present and
    different from the brand name — this is CRITICAL for companies where the
    brand name (e.g. "SpaceX") does not match the registered filer name
    (e.g. "Space Exploration Technologies Corp"). Without it, filer verification
    will reject the EDGAR hit as a mention-only document.
  • Always pass the company name as the required 'name' argument.

  • If found=false: the company does not file with the SEC. Set
    edgar_lookup_status="not_sec_reporting". Return unknown for all other
    fields. Do NOT call any further tools.
  • If found=true: note the CIK, accession_no, filing_type, and
    most_recent_filing date. Proceed to step 2.

Step 2 — Call edgar_get_financials with the CIK from step 1.
  • If xbrl_available=true: use revenue.formatted and net_income.formatted.
    Note revenue.accession_no for use in step 3.
  • If xbrl_available=false (brand-new IPO filer, XBRL not yet aggregated):
    Gap revenue and profitability — set value="unknown", confidence="unknown",
    sources=[]. Cite the filing in reasoning: "XBRL not yet available in
    companyfacts for [filing_type] filer; accession [accession_no]." Do NOT
    set edgar_lookup_status="lookup_failed" — this company IS an SEC filer;
    the data just isn't aggregated yet.
  • If result contains "error": set edgar_lookup_status="lookup_failed".
  • For step 3, use accession_no: prefer revenue.accession_no if available;
    fall back to accession_no from step 1 (the filing accession).

Step 3 — Call edgar_get_filing_text with section="risk_factors".
  • The tool handles both 10-K (Item 1A) and S-1/424B (standalone heading).
  • Check extraction_status in the result:

  extraction_status="extracted": The tool found the section. Extract the 3-5
    most material risk factors from the text field. Each is one DataPoint with:
    - confidence: "high" (directly from an SEC filing)
    - sources: [source_url from the tool result]
    - reasoning: "Risk factor from [filing_form_type] filed [most_recent_filing]."

    IMPORTANT — 424B/S-1 summary sections: Some prospectuses begin with a
    "Summary of Risk Factors" or "Risk Factors Summary" that lists risks as
    bullet points (&#8226;) before the detailed section. If the extracted text
    says "this summary should be read in conjunction with the Risk Factors
    section" or similar, the bullet points are still valid risk factors — extract
    3-5 of them directly. Do NOT add a placeholder or note that the section is
    incomplete; the bullet points ARE the risk factors to report.

  extraction_status="section_not_found": The heading was not found. Add ONE
    DataPoint to sec_risk_factors to make the gap visible:
    - value: "Risk factors section not found in [filing_form_type] (may use
      non-standard headings)"
    - confidence: "unknown"
    - sources: [source_url from the tool result]
    - reasoning: Note field from the tool result.

  extraction_status="fetch_failed" or an "error" field present: The filing
    could not be retrieved. Add ONE DataPoint to sec_risk_factors:
    - value: "Risk factors unavailable — filing fetch failed"
    - confidence: "unknown"
    - sources: [url from the tool result, or accession_no from step 1 as source]
    - reasoning: Error field from the tool result.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GAPS-NOT-FABRICATION:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
NEVER invent or estimate financial figures. If a value is not directly
available from EDGAR tools, leave it as value="unknown", confidence="unknown".
A correct honest gap is far better than a fabricated number. This applies
especially to revenue and profitability for recent IPO filers where
companyfacts returns xbrl_available=false.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

CONFIDENCE RULES:
- Audited financials from XBRL companyfacts → HIGH
- Risk factors from 10-K or S-1/424B filing text → HIGH
- Gapped financials (xbrl_available=false) → confidence="unknown"
- Never fabricate numbers — unknown is always the correct fallback

SOURCE URLS: Always use the exact URL returned by the tool (data.sec.gov,
www.sec.gov/Archives/...). Do not fabricate or guess URLs.

PROVENANCE: In the reasoning field of every claim, record the filing type
(10-K / S-1/A / 424B4 / etc.) and the filing date. Example:
  "From us-gaap.Revenues in 10-K filed 2024-11-01"
  "Risk factor from 424B4 (priced prospectus) filed 2026-06-12"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COMPUTATION DISCIPLINE:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EDGAR provides audited XBRL values — these are NOT derived, they are sourced
facts. However, if you compute a ratio or margin from two XBRL values (e.g.,
net margin = net income / revenue), mark the result:

  "derived": true
  "derived_from": ["_claim_id of revenue claim", "_claim_id of net income claim"]
  "reasoning": "Net margin computed from XBRL revenue and net income"

Assign "_claim_id" (e.g. "edgar_rev") to any DataPoint that will be cited in
another claim's derived_from. Pure transcription of XBRL values → derived=false.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

When complete, respond with ONLY this JSON (no markdown, no backticks):

{_OUTPUT_SCHEMA}"""

    async def handle_tool_call(self, tool_name: str, tool_input: dict) -> str:
        if tool_name == "edgar_find_company":
            return await edgar_find_company(
                tool_input["name"],
                cache=self.cache,
                ticker=tool_input.get("ticker"),
                legal_name=tool_input.get("legal_name"),
            )
        if tool_name == "edgar_get_financials":
            return await edgar_get_financials(tool_input["cik"], cache=self.cache)
        if tool_name == "edgar_get_filing_text":
            return await edgar_get_filing_text(
                cik=tool_input["cik"],
                accession_no=tool_input["accession_number"],
                section=tool_input.get("section", "risk_factors"),
                cache=self.cache,
            )
        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    def parse_final_output(self, text: str) -> dict:
        parsed = json.loads(self._strip_json(text))
        return CompanyEdgarFinancials(**parsed).model_dump()
