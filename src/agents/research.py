"""
Research Agent — gathers core company profile information.

P3b: extends WebSearchMixin with two Tier 0 tools:
  - opencorporates_search: US entity registry (incorporation date, address)
  - uspto_patent_search: USPTO PatentsView (patent count, notable patents)

Tool call budget raised from 4-5 to 6 to accommodate Tier 0 priority calls.
"""

import json

from src.agents.base import WebSearchMixin, _WEB_FETCH_TOOL, _WEB_SEARCH_TOOL
from src.schemas.models import CompanyResearch
from src.sources.opencorporates import opencorporates_search_company
from src.sources.uspto import uspto_search_patents


# Tier 0 / 1 tool definitions

_OC_TOOL = {
    "name": "opencorporates_search",
    "description": (
        "Search OpenCorporates for a US company's official registration data: "
        "incorporation date, registered address, jurisdiction, company status. "
        "This is a Tier 1 (reputable secondary) source — use its data with HIGH "
        "confidence for founded_year and headquarters. Call this FIRST."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "company_name": {
                "type": "string",
                "description": "Company legal name to search for",
            },
        },
        "required": ["company_name"],
    },
}

_USPTO_TOOL = {
    "name": "uspto_patent_search",
    "description": (
        "Search USPTO PatentsView for US patents assigned to a company. "
        "Returns total patent count and up to 10 recent patents (title, grant date). "
        "PRIMARY_DOCUMENT tier — official USPTO records."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "company_name": {
                "type": "string",
                "description": "Company name (assignee) to search for patents",
            },
        },
        "required": ["company_name"],
    },
}


# The JSON schema the LLM must return — includes P3b fields
OUTPUT_SCHEMA = """{
  "company_name": "string",
  "description": {"value": "string", "confidence": "high|medium|low|unknown", "sources": ["url1"], "derived": false, "derived_from": [], "reasoning": "optional string"},
  "founded_year": {"value": "string", "confidence": "high|medium|low|unknown", "sources": ["url1"], "derived": false, "derived_from": [], "reasoning": "optional"},
  "headquarters": {"value": "string", "confidence": "high|medium|low|unknown", "sources": ["url1"], "derived": false, "derived_from": [], "reasoning": "optional"},
  "employee_count": {"value": "string", "confidence": "high|medium|low|unknown", "sources": ["url1"], "derived": false, "derived_from": [], "reasoning": "optional"},
  "industry": {"value": "string", "confidence": "high|medium|low|unknown", "sources": ["url1"], "derived": false, "derived_from": [], "reasoning": "optional"},
  "key_products": [{"value": "string", "confidence": "high|medium|low|unknown", "sources": ["url1"], "derived": false, "derived_from": [], "reasoning": "optional"}],
  "key_leadership": [{"value": "Name - Title", "confidence": "high|medium|low|unknown", "sources": ["url1"], "derived": false, "derived_from": [], "reasoning": "optional"}],
  "technology_stack": [{"value": "string", "confidence": "high|medium|low|unknown", "sources": ["url1"], "derived": false, "derived_from": [], "reasoning": "optional"}],
  "recent_developments": [{"value": "string", "confidence": "high|medium|low|unknown", "sources": ["url1"], "derived": false, "derived_from": [], "reasoning": "optional"}],
  "website": {"value": "string", "confidence": "high|medium|low|unknown", "sources": [], "derived": false, "derived_from": [], "reasoning": "optional"},
  "patent_count": {"value": "e.g. '1,234 US patents'", "confidence": "high|medium|low|unknown", "sources": ["url1"], "derived": false, "derived_from": [], "reasoning": "optional"},
  "notable_patents": [{"value": "Patent title or ID with date", "confidence": "high", "sources": ["url1"], "derived": false, "derived_from": [], "reasoning": "optional"}]
}"""


class ResearchAgent(WebSearchMixin):
    AGENT_NAME = "research"
    PROMPT_VERSION = "2.1"

    def get_tools(self) -> list:
        return [_OC_TOOL, _USPTO_TOOL, _WEB_SEARCH_TOOL, _WEB_FETCH_TOOL]

    async def handle_tool_call(self, tool_name: str, tool_input: dict) -> str:
        if tool_name == "opencorporates_search":
            return await opencorporates_search_company(
                tool_input["company_name"]
            )
        if tool_name == "uspto_patent_search":
            return await uspto_search_patents(tool_input["company_name"])
        # Delegate web tools to mixin
        return await super().handle_tool_call(tool_name, tool_input)

    def get_system_prompt(self) -> str:
        return f"""You are a Research Agent performing company due diligence.

Your job is to gather core profile information about a company using
Tier 0/1 primary sources first, then web search to fill gaps.
{self._format_context()}
TOOLS AVAILABLE (in priority order):
- opencorporates_search: Official US company registration — incorporation date,
  registered address, jurisdiction. Tier 1 source. Call this FIRST.
- uspto_patent_search: USPTO PatentsView — US patent count and notable patents.
  PRIMARY_DOCUMENT tier. Call this for any technology or IP-active company.
- web_search: Search the web. Use for any fields not covered by Tier 0/1 tools.
- web_fetch: Fetch a specific URL for deeper extraction.

RESEARCH STRATEGY:
1. Call opencorporates_search for founded_year and headquarters.
   - If found=true: use incorporation_date → founded_year (HIGH confidence),
     registered_address → headquarters (HIGH confidence), with OC source URL.
   - If disabled (no API key) or found=false: get these from web search instead.
2. Call uspto_patent_search for patent data.
   - If found=true: set patent_count.value="N US patents" with HIGH confidence.
   - If patent_count=0 or disabled: set patent_count.value="None found" (MEDIUM).
3. Web search for description, leadership, technology, recent news.
4. Web search for employee count and industry if not yet found.
5. Optionally fetch 1-2 promising URLs for deeper extraction.
6. Synthesize into the structured output.

SOURCE PRECEDENCE:
- OpenCorporates data overrides web search for founded_year and headquarters.
- USPTO data is authoritative for patent_count and notable_patents.
- Do not fabricate URLs. Only include URLs actually returned by tools.

CONFIDENCE SCORING RULES:
- HIGH: Tier 0/1 source, or multiple independent web sources confirm the fact
- MEDIUM: One reliable web source (company website, Crunchbase)
- LOW: Inferred, estimated, or from a single unreliable source
- UNKNOWN: Could not find any information

When you have gathered enough information, respond with ONLY a JSON object
matching this exact schema (no markdown, no backticks, no explanation):

{OUTPUT_SCHEMA}

TOOL CALL BUDGET:
You MUST complete your research within 6 tool calls. After your 5th call,
STOP and synthesize your findings. It is far better to return a complete
JSON response with some LOW-confidence fields than to keep searching.

IMPORTANT:
- patent_count and notable_patents are REQUIRED fields in the output.
  If USPTO returns no patents: set patent_count to value="No US patents found",
  confidence="high", and notable_patents to an empty list.
- Every field must have a confidence level and at least one source URL.
- For recent_developments, focus on the last 6 months.
- Be specific: "2,847 employees (2024)" is better than "thousands of employees".

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COMPUTATION DISCIPLINE:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
If a value you emit contains a number YOU computed (percentage, growth rate,
ratio, or any other arithmetic not directly stated by a source), mark it:

  "derived": true
  "derived_from": ["_claim_id of atomic input 1", "_claim_id of atomic input 2"]
  "reasoning": "explain the computation"

Assign a short "_claim_id" (e.g. "emp_2024") to any DataPoint that will be
referenced by another claim's derived_from. Numbers that appear verbatim in a
source are NOT derived — only mark derived=true for YOUR arithmetic.

If you cannot cite both atomic inputs, omit the derived figure and report only
the atomic values you can source.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""

    def parse_final_output(self, text: str) -> dict:
        parsed = json.loads(self._strip_json(text))
        return CompanyResearch(**parsed).model_dump()
