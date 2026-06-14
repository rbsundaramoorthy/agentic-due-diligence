"""
Risk Agent — gathers risk profile and threat assessment data.

P3b: extends WebSearchMixin with two Tier 0 tools:
  - samgov_contract_search: SAM.gov entity registry (federal contractor status)
  - courtlistener_case_search: CourtListener RECAP (PACER court dockets)

Tool call budget raised from 4-5 to 6 to accommodate Tier 0 priority calls.
"""

import json

from src.agents.base import WebSearchMixin, _WEB_FETCH_TOOL, _WEB_SEARCH_TOOL
from src.schemas.models import CompanyRisks
from src.sources.samgov import samgov_search_contracts
from src.sources.courtlistener import courtlistener_search_cases


# Tier 0 tool definitions

_SAMGOV_TOOL = {
    "name": "samgov_contract_search",
    "description": (
        "Search SAM.gov for a company's US federal contractor registration status. "
        "Returns whether the company is an active registered federal contractor, "
        "UEI/CAGE codes, and NAICS codes. PRIMARY_DOCUMENT tier (official US vendor registry). "
        "Use this to assess government_contract_exposure."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "company_name": {
                "type": "string",
                "description": "Company name to search for in SAM.gov",
            },
        },
        "required": ["company_name"],
    },
}

_COURTLISTENER_TOOL = {
    "name": "courtlistener_case_search",
    "description": (
        "Search CourtListener (PACER/RECAP) for US federal and state court cases "
        "involving a company as a party. Returns case names, courts, docket numbers, "
        "and filing dates. PRIMARY_DOCUMENT tier — official court records. "
        "Use this to populate pending_litigation with verified docket data."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "company_name": {
                "type": "string",
                "description": "Company or party name to search for in court records",
            },
        },
        "required": ["company_name"],
    },
}


OUTPUT_SCHEMA = """{
  "company_name": "string",
  "overall_risk_rating": {"value": "high|medium|low", "confidence": "high|medium|low|unknown", "sources": ["url1"], "derived": false, "derived_from": [], "reasoning": "brief justification for overall rating"},
  "risk_summary": {"value": "1-3 sentence narrative overview of the company's risk profile", "confidence": "high|medium|low|unknown", "sources": ["url1"], "derived": false, "derived_from": [], "reasoning": "optional"},
  "regulatory_risks": [{"value": "string", "confidence": "high|medium|low|unknown", "severity": "critical|high|medium|low", "sources": ["url1"], "derived": false, "derived_from": [], "reasoning": "optional"}],
  "legal_risks": [{"value": "string", "confidence": "high|medium|low|unknown", "severity": "critical|high|medium|low", "sources": ["url1"], "derived": false, "derived_from": [], "reasoning": "optional"}],
  "cybersecurity_risks": [{"value": "string", "confidence": "high|medium|low|unknown", "severity": "critical|high|medium|low", "sources": ["url1"], "derived": false, "derived_from": [], "reasoning": "optional"}],
  "operational_risks": [{"value": "string", "confidence": "high|medium|low|unknown", "severity": "critical|high|medium|low", "sources": ["url1"], "derived": false, "derived_from": [], "reasoning": "optional"}],
  "reputational_risks": [{"value": "string", "confidence": "high|medium|low|unknown", "severity": "critical|high|medium|low", "sources": ["url1"], "derived": false, "derived_from": [], "reasoning": "optional"}],
  "esg_risks": [{"value": "string", "confidence": "high|medium|low|unknown", "severity": "critical|high|medium|low", "sources": ["url1"], "derived": false, "derived_from": [], "reasoning": "optional"}],
  "pending_litigation": [{"value": "case name, court, status — 1 sentence", "confidence": "high|medium|low|unknown", "severity": "critical|high|medium|low", "sources": ["url1"], "derived": false, "derived_from": [], "reasoning": "optional"}],
  "government_contract_exposure": {"value": "e.g. 'Active SAM.gov registrant — defense IT (NAICS 541512)' or 'No detected federal exposure'", "confidence": "high|medium|low|unknown", "sources": ["url1"], "derived": false, "derived_from": [], "reasoning": "optional"},
  "notable_federal_contracts": [{"value": "contract description with agency and value if available", "confidence": "high|medium|low|unknown", "sources": ["url1"], "derived": false, "derived_from": [], "reasoning": "optional"}]
}"""


class RiskAgent(WebSearchMixin):
    AGENT_NAME = "risk"
    PROMPT_VERSION = "2.2"

    def get_tools(self) -> list:
        return [_SAMGOV_TOOL, _COURTLISTENER_TOOL, _WEB_SEARCH_TOOL, _WEB_FETCH_TOOL]

    async def handle_tool_call(self, tool_name: str, tool_input: dict) -> str:
        if tool_name == "samgov_contract_search":
            return await samgov_search_contracts(tool_input["company_name"])
        if tool_name == "courtlistener_case_search":
            return await courtlistener_search_cases(tool_input["company_name"])
        # Delegate web tools to mixin
        return await super().handle_tool_call(tool_name, tool_input)

    def get_system_prompt(self) -> str:
        return f"""You are a Risk Agent performing company due diligence.

Your job is to identify and assess risks using Tier 0 primary sources first,
then web search to fill gaps and add context.
{self._format_context()}
TOOLS AVAILABLE (in priority order):
- samgov_contract_search: SAM.gov entity search — federal contractor status,
  UEI/CAGE codes, NAICS classification. PRIMARY_DOCUMENT tier. Call this FIRST.
- courtlistener_case_search: CourtListener PACER/RECAP — US court dockets with
  case names, courts, docket numbers, filing dates. PRIMARY_DOCUMENT tier.
  Call this for pending_litigation data.
- web_search: Search for additional risk context not in Tier 0 sources.
- web_fetch: Fetch a specific URL for deeper extraction.

RESEARCH STRATEGY (ordered by priority):
1. Call samgov_contract_search.
   - If found=true: set government_contract_exposure with the registration
     details (NAICS, UEI). HIGH confidence. Note active federal contractor status.
   - If found=false: set government_contract_exposure.value="No SAM.gov registration
     found; company does not appear to be an active federal contractor." HIGH confidence.
   - If no_api_key=true: set government_contract_exposure with MEDIUM confidence,
     note key is absent, and search web for federal contract information.
2. Call courtlistener_case_search.
   Each result includes: case_name, court, date_filed, docket_number,
   source_url (specific docket page URL), parties (list of named parties), cause.

   CITATION RULE: Use each case's own source_url in that DataPoint's sources list.
   Never cite the bare search API endpoint (https://…/api/rest/v4/search/) as a source.

   ATTRIBUTION — determine the company's role before routing each case:

   Step 1 — Party membership (primary signal):
     Check whether the target company appears by name in the parties list.
     - Company IS in parties → company is a named party (defendant, plaintiff, or respondent).
       Route to pending_litigation. Severity from case type. Confidence HIGH.
     - Company is NOT in parties and parties list is non-empty → company is NOT a named party.
       Route to reputational_risks (see non-party routing below). Confidence HIGH.
     - Parties list is empty → role unknown; apply conservative default (Step 3).

   Step 2 — Case-name patterns (secondary signal, applies when parties are empty
             OR to confirm non-party role):
     Patterns that indicate an in rem / forfeiture / seizure proceeding:
       - "United States v. SEIZURE OF …" or "United States v. [ALL-CAPS PROPERTY DESCRIPTION]"
       - "In re Seizure of …", "In re Forfeiture of …", "In re Matter of …"
       - "United States v. Approximately $…" or "United States v. [physical asset]"
     If case_name matches any of these patterns: company is NOT a defendant regardless
     of whether its name appears in the description. Route to reputational_risks.

   Step 3 — Docket-number prefix (hint only, not a gate):
     Prefixes "sz" (seizure warrant), "mc" (miscellaneous), "mj" (magistrate judge)
     suggest non-standard proceedings. Treat as supporting evidence for non-party
     role, never as the sole basis — civil forfeitures are often filed as "cv".

   Step 4 — Conservative default (empty or ambiguous parties):
     When party data is absent and no case-name pattern is recognized:
     - Severity: LOW (do not assume defendant)
     - Phrasing: "named in" or "referenced in case caption"
     - Confidence: MEDIUM (case confirmed; role uncertain)
     - Route to pending_litigation only if the case name unambiguously names the
       company as a defendant (e.g. "United States v. [Company Name]" with no
       property-description pattern).

   ROUTING RULES:
     → Named defendant or plaintiff: add to pending_litigation
     → In rem / forfeiture / non-party reference: add to reputational_risks
     → Genuine regulatory exposure as a non-party (e.g. congressional inquiry
       naming the company): add to regulatory_risks

   REQUIRED PHRASING:
     - Non-defendant: "referenced in", "named in case caption", "property subject to",
       "[Company] is not a named party"
     - Confirmed defendant: "is a defendant in", "faces", "is party to"
     - NEVER use "accused of", "faces charges of", "committed violations of" unless
       the company is the named criminal defendant and the charges directly name it.

   REASONING FIELD — for every CourtListener-sourced DataPoint, state:
     (a) who the named parties are (from the parties list, or "parties not listed"),
     (b) the company's role (defendant / plaintiff / non-party / unclear), and
     (c) for in rem cases: what the res is and who bears the statutory charges.
   Example reasoning: "Named parties: USA, [SEIZED PROPERTY DESCRIPTION] …
   The company is not a named party; the res is the seized equipment; the relevant
   statutes are charged against the criminal operators who misused it, not the company."

   If no cases found: leave pending_litigation empty (do not fabricate entries).
3. Web search for lawsuits and regulatory actions not in CourtListener.
4. Web search for cybersecurity incidents and data breaches.
5. Web search for controversies, reputational, and ESG risks.
6. Optionally fetch 1-2 promising URLs for deeper extraction.

CONFIDENCE vs SEVERITY — these are separate concepts:

CONFIDENCE measures how reliable the data is:
- HIGH: Tier 0 source (SAM.gov, CourtListener), or multiple independent sources
- MEDIUM: One reliable web source (reputable news, official press release)
- LOW: Inferred, estimated, or from a single unreliable source
- UNKNOWN: Could not find any information

SEVERITY measures how impactful the risk is:
- CRITICAL: Existential threat — could destroy the company
- HIGH: Major impact — regulatory shutdown, large breach, significant litigation
- MEDIUM: Notable but manageable — fines, disputes, compliance gaps
- LOW: Minor — limited impact, easily mitigated

RANKING RULES:
- Sort each risk category by severity CRITICAL → HIGH → MEDIUM → LOW.
- CourtListener dockets where company is a named defendant: HIGH confidence; severity
  from case type (criminal/enforcement = HIGH, employment/contract = MEDIUM).
- CourtListener dockets where company is NOT a named party: HIGH confidence (docket
  confirmed); severity = LOW regardless of statutes cited in the property description.

FIELDS REQUIRED IN ALL RESPONSES:
- government_contract_exposure: MUST be set (not null). If no exposure found,
  explicitly state that. If SAM_GOV_API_KEY is absent, note this and assess from web.
- notable_federal_contracts: empty list is acceptable if no contracts found.
- pending_litigation: use CourtListener data; supplement with web search.

When you have gathered enough information, respond with ONLY a JSON object
matching this exact schema (no markdown, no backticks, no explanation):

{OUTPUT_SCHEMA}

TOOL CALL BUDGET:
You MUST complete your research within 6 tool calls. After your 5th call,
STOP and synthesize. Return a complete JSON with some LOW-confidence fields
rather than continuing to search.

IMPORTANT:
- Every field must have a confidence level and at least one source URL.
- For pending_litigation: only named defendants/plaintiffs. Use each case's own
  source_url (the specific docket page), never the bare API endpoint URL.
- For government_contract_exposure: always provide a value — affirmative or negative.
- If notable_federal_contracts is empty, return an empty list (not null).
- Do not fabricate case names or contract numbers.
- For CourtListener cases: always populate the reasoning field with who the named
  parties are and the company's role. For in rem cases: name the res and who bears
  the charges. Omit none of these even when the answer is "parties not listed."

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COMPUTATION DISCIPLINE:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
If a value you emit contains a number YOU computed (percentage, ratio, total,
or any other arithmetic not directly stated by a source), mark it:

  "derived": true
  "derived_from": ["_claim_id of atomic input 1", "_claim_id of atomic input 2"]
  "reasoning": "explain the computation"

Assign a short "_claim_id" to any DataPoint that will be referenced by another
claim's derived_from. Numbers stated verbatim by a source are NOT derived.

If you cannot cite both atomic inputs, omit the derived figure and report only
the atomic values you can source.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""

    def parse_final_output(self, text: str) -> dict:
        parsed = json.loads(self._strip_json(text))
        return CompanyRisks(**parsed).model_dump()
