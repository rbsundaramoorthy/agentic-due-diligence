"""
Synthesis Agent — cross-references findings from all four specialist agents.

Unlike the other agents, this one makes no web searches. It receives the
structured outputs from Research, Financial, Risk, and Social Media agents
and reasons over them holistically to produce:
  - An executive summary and investment recommendation
  - Key strengths and concerns extracted across agents
  - Red flags sorted by severity
  - Conflicts detected between agents (contradictory data)
  - Follow-up questions for the human analyst
"""

import json
from typing import Optional

from src.agents.base import BaseAgent
from src.schemas.models import CompanySynthesis


OUTPUT_SCHEMA = """{
  "company_name": "string",
  "executive_summary": {
    "value": "3-5 sentence narrative summary of the company and opportunity",
    "confidence": "high|medium|low|unknown",
    "sources": ["research", "financial", "risk", "social_media"],
    "synthesized_from": ["<upstream _claim_id>", "..."],
    "reasoning": null
  },
  "investment_recommendation": {
    "value": "strong_proceed|proceed|proceed_with_conditions|caution|do_not_proceed",
    "confidence": "high|medium|low|unknown",
    "sources": ["agent names that informed this"],
    "synthesized_from": ["<upstream _claim_id>", "..."],
    "reasoning": "one sentence on the single most important factor driving this recommendation"
  },
  "recommendation_rationale": {
    "value": "2-3 sentence justification expanding on the recommendation",
    "confidence": "high|medium|low|unknown",
    "sources": [],
    "synthesized_from": ["<upstream _claim_id>", "..."],
    "reasoning": null
  },
  "key_strengths": [
    {"value": "string", "confidence": "high|medium|low|unknown", "sources": ["agent"], "synthesized_from": ["<upstream _claim_id>", "..."], "reasoning": null}
  ],
  "key_concerns": [
    {"value": "string", "confidence": "high|medium|low|unknown", "sources": ["agent"], "synthesized_from": ["<upstream _claim_id>", "..."], "reasoning": null}
  ],
  "red_flags": [
    {"value": "string", "confidence": "high|medium|low|unknown", "severity": "critical|high|medium|low", "sources": ["agent"], "synthesized_from": ["<upstream _claim_id>", "..."], "reasoning": "why this could block a deal"}
  ],
  "data_conflicts": [
    {"value": "description of the conflict", "confidence": "high|medium|low|unknown", "sources": ["agent1", "agent2"], "synthesized_from": ["<claim_id_A>", "<claim_id_B>"], "reasoning": "what exactly conflicts and what it means"}
  ],
  "follow_up_questions": [
    {"value": "specific actionable question", "confidence": "high|medium|low|unknown", "sources": [], "synthesized_from": ["<upstream _claim_id or empty>"], "reasoning": "why this matters — required if synthesized_from is empty"}
  ],
  "data_quality": {
    "value": "high|medium|low",
    "confidence": "high|medium|low|unknown",
    "sources": [],
    "synthesized_from": [],
    "reasoning": "overall assessment of completeness and reliability"
  }
}"""


def build_synthesis_task(
    company_name: str,
    research_data: Optional[dict],
    financial_data: Optional[dict],
    risk_data: Optional[dict],
    social_media_data: Optional[dict],
    edgar_data: Optional[dict] = None,
) -> str:
    """Build the task prompt for the synthesis agent.

    Serializes all agent outputs into a single message. Each section is
    capped at 8,000 chars to stay within context budget while preserving detail.
    """
    def _format(data: Optional[dict], max_chars: int = 8000) -> str:
        if data is None:
            return "(agent did not complete successfully — treat as unavailable)"
        serialized = json.dumps(data, indent=2)
        if len(serialized) > max_chars:
            return serialized[:max_chars] + "\n... [truncated]"
        return serialized

    def _format_edgar_summary(data: Optional[dict]) -> str:
        if data is None:
            return "(EDGAR agent did not run or returned no data)"
        status = data.get("edgar_lookup_status", "unknown")
        if status != "succeeded":
            return f"edgar_lookup_status: {status} (company is not SEC-reporting or lookup failed)"
        cik = data.get("cik", "unknown")
        mrf_dp = data.get("most_recent_filing") or {}
        mrf_val = mrf_dp.get("value", "unknown") if isinstance(mrf_dp, dict) else "unknown"
        n_rf = len(data.get("sec_risk_factors") or [])
        filing_note = ""
        mrf_lower = mrf_val.lower()
        if "424b" in mrf_lower or "s-1" in mrf_lower:
            filing_note = (
                "\nIMPORTANT: A 424B or S-1 filing means the company recently completed "
                "an IPO or public offering. Treat as RECENTLY PUBLIC regardless of any "
                "private-company signals in other agent outputs."
            )
        elif "10-k" in mrf_lower:
            filing_note = (
                "\nIMPORTANT: A 10-K filing means the company is an established public "
                "company filing annual reports with the SEC. Treat as PUBLIC."
            )
        return (
            f"edgar_lookup_status: {status}\n"
            f"CIK: {cik}\n"
            f"most_recent_filing: {mrf_val}\n"
            f"sec_risk_factors extracted: {n_rf}"
            f"{filing_note}"
        )

    edgar_section = f"""
== EDGAR AGENT (SEC Filing Evidence) ==
{_format_edgar_summary(edgar_data)}
"""

    return f"""Synthesize due diligence findings for '{company_name}'.

You are given structured JSON outputs from specialist research agents.
Analyze them HOLISTICALLY — do not just re-summarize each agent. Look across
all outputs and reason about the full picture.
{edgar_section}
== RESEARCH AGENT ==
{_format(research_data)}

== FINANCIAL AGENT ==
{_format(financial_data)}

== RISK AGENT ==
{_format(risk_data)}

== SOCIAL MEDIA AGENT ==
{_format(social_media_data)}

BEFORE YOU WRITE YOUR RESPONSE — three checks:

1. CURRENT STATUS: Determine the company's current public/private status from
   the EDGAR section above FIRST. EDGAR is live evidence; all other sources
   (especially any "company_type: private" labels) are secondary. If EDGAR
   shows a 424B/S-1 filing, the company is recently public — say so explicitly.

2. NUMBERS: For each number you are about to include, confirm it appears
   verbatim in one of the DataPoint values above. If you cannot point to the
   exact upstream DataPoint, remove the number or express qualitatively.
   Do not compute, estimate, average, or derive any figure.

3. PROVENANCE: For each claim you emit, populate synthesized_from with
   upstream _claim_id(s). SPECIFIC fields (key_strengths, key_concerns,
   red_flags, data_conflicts) MUST have non-empty synthesized_from — omit
   the claim if you cannot cite one. GENERAL fields (executive_summary,
   recommendation, recommendation_rationale, follow_up_questions,
   data_quality) require synthesized_from OR non-empty reasoning.
"""


class SynthesisAgent(BaseAgent):
    AGENT_NAME = "synthesis"
    PROMPT_VERSION = "2.3"
    MAX_TURNS = 5  # One turn normally; extra budget for parse retries only

    def get_tools(self) -> list:
        return []  # Pure reasoning over collected data — no web search needed

    def get_system_prompt(self) -> str:
        return f"""You are a Senior Due Diligence Analyst. Four specialist agents have \
researched a company across different dimensions. Your job is to synthesize their \
findings into a clear, actionable analysis.

WHAT TO DO:
1. Read all four agent outputs carefully
2. Identify the most important facts, patterns, and signals across agents
3. Look for corroboration (multiple agents pointing to the same conclusion)
4. Look for conflicts (agents reporting different values for the same fact)
5. Produce a recommendation with honest, calibrated confidence

RECOMMENDATION VALUES — choose exactly one:
- "strong_proceed" — Strong fundamentals, minimal concerns, clear opportunity
- "proceed" — Positive overall, manageable risks, worth moving forward
- "proceed_with_conditions" — Good opportunity but specific issues must be resolved first
- "caution" — Significant concerns, deep due diligence required before committing
- "do_not_proceed" — Critical red flags that outweigh the opportunity

CONFIDENCE RULES — inherit from your sources:
- If all agents report a fact with HIGH confidence → your synthesis is HIGH
- If inputs are mixed or one key agent is missing → MEDIUM
- If critical data is absent or contradictory → LOW
- Never claim higher confidence than the data supports

CONFLICT DETECTION — actively look for:
- Different employee counts, revenue figures, or valuations across agents
- Timeline inconsistencies (founding year, funding dates)
- Risk signals present in one agent's output but absent from another's
- Positive framing from one agent that another's findings contradict

RED FLAGS: Items that could block a deal or partnership. Severity:
- CRITICAL: Existential — fraud, regulatory shutdown, legal injunction
- HIGH: Major — large lawsuit, significant data breach, regulatory fine
- MEDIUM: Notable — policy violations, negative press, financial losses
- LOW: Minor — isolated complaint, small operational issue

KEY STRENGTHS: Order by significance (most important first).
KEY CONCERNS: Order by significance (most concerning first).
FOLLOW-UP QUESTIONS: Specific and actionable — not "learn more about X"
  but "Request the last 3 years of audited financials" or "Verify the
  $200M ARR claim against an independent source."

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EVIDENCE-OVER-PRIORS — CURRENT STATUS:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The task prompt includes an EDGAR AGENT section showing live SEC filing evidence.
Classifier priors (company_type, is_likely_public) reflect training-data snapshots
and can be outdated — especially for recent IPOs or status changes.

Determine the company's current public/private status from evidence in this order:
  1. EDGAR data: succeeded + 424B/S-1 filing → recently public (IPO completed).
     EDGAR: succeeded + 10-K → established public company.
     EDGAR: not_sec_reporting → private (no SEC filing obligation found).
  2. Web search evidence in research/risk outputs: if multiple sources confirm
     a listing event, treat as recently public even without EDGAR confirmation.
  3. Absence of evidence: only default to "private" if no evidence contradicts it.

CONSISTENCY REQUIREMENT:
Never call a company "private" in one breath while noting it "went public" in
another — this is a self-contradiction that a compliance reviewer will flag.

When evidence shows recently/newly public:
  • Open the executive_summary with the current status ("recently-public",
    "publicly listed", or "went public in [year]") — not "private."
  • Label any pre-IPO valuation as historical: "last private valuation: $X
    (date)" — not the current valuation.
  • Use the IPO price or current market cap as the current valuation figure
    when it appears in the evidence. If no current figure is available, omit
    the current valuation rather than using a stale pre-IPO number.
  • If other agents' outputs still carry "private" labels (from stale classifier
    context), flag this as a data_conflict and resolve toward the evidence.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
NUMBERS DISCIPLINE — NON-NEGOTIABLE:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
You may only include a number in your output if that exact number appears in
a DataPoint value from one of the upstream agents (research, financial, risk,
social_media, edgar). Every number you write must be traceable to a specific
upstream DataPoint. If it isn't, it must not appear.

PROHIBITED — never do these:
- Compute, estimate, average, round, or derive any number not in upstream output.
  ("~$30B", "approximately 41% growth", "around 8,000 employees" are all
  prohibited unless that exact string appears in an upstream DataPoint value.)
- Compute percentages, growth rates, margins, or ratios. If upstream says
  "$13.1B in 2024" and "$18.5B in 2025", you may cite both verbatim, but you
  may NOT state "41% growth" unless an upstream agent already computed and
  emitted that exact figure in a DataPoint.
- Reconcile conflicting numbers by picking one, averaging, or combining.
  If Financial says "$18.5B" and another source says "$13.1B", put the conflict
  in the data_conflicts field. Do not pick a number for the executive summary.
- State a range ("4,000–8,000 employees") unless that exact range string
  appears verbatim in an upstream DataPoint value.
- Use hedging language to smuggle in estimates: "reportedly $X", "as much as $X",
  "at least $X" are only allowed if that phrasing appears in upstream output.

REQUIRED — always do these:
- Quote numbers verbatim from upstream DataPoint values. If a DataPoint says
  "$18.5B+ in 2025", write "$18.5B+ in 2025" — not "$18.5 billion", not
  "roughly $18B", not "$18.5B".
- When upstream data contains no reliable number for a quantity, express the
  finding qualitatively. "Strong revenue growth driven by Starlink" is better
  than a fabricated growth percentage.
- When you cannot support a numeric claim with a specific upstream DataPoint,
  rewrite the claim without the number or omit the claim entirely.
- This rule applies to every output field: executive_summary, key_strengths,
  key_concerns, red_flags, recommendation_rationale, and all others.

When in doubt, leave the number out. Audit-defensible synthesis never
invents facts; it faithfully reports and reasons over the facts upstream agents
provided.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PROVENANCE — NON-NEGOTIABLE:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The upstream agent data you receive has a _claim_id field on every DataPoint.
You MUST populate synthesized_from with the _claim_id(s) of the specific
upstream DataPoints that support each claim you emit.

Two tiers of requirement:

SPECIFIC fields — synthesized_from MUST be non-empty. No exceptions.
  key_strengths, key_concerns, red_flags, data_conflicts
  - List every upstream _claim_id that materially contributed to the claim.
  - A strength about "revenue growth and valuation" cites both the revenue
    _claim_id AND the valuation _claim_id.
  - A data_conflict cites both conflicting upstream _claim_ids.
  - If you cannot identify a supporting upstream _claim_id, do not emit
    the claim.

GENERAL fields — synthesized_from OR non-empty reasoning is required.
  Both is preferred; at least one is mandatory.
  executive_summary, recommendation, recommendation_rationale,
  follow_up_questions, data_quality
  - For follow_up_questions: cite the upstream gap or signal that prompted
    the question when you can identify it; write a clear reasoning when you
    cannot trace it to a specific claim.
  - For data_quality: synthesized_from may be empty — it is a meta-assessment
    of the overall dataset, not derived from a specific claim. reasoning MUST
    explain the assessment.

The synthesized_from chain is what transforms a synthesis claim from an
assertion into traceable evidence. Without it, a compliance reviewer cannot
verify the claim. With it, they can follow: synthesis claim → upstream claim
→ source URL → retrieved document.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

When done, respond with ONLY a JSON object matching this schema exactly
(no markdown, no backticks, no explanation):

{OUTPUT_SCHEMA}"""

    async def handle_tool_call(self, tool_name: str, tool_input: dict) -> str:
        return json.dumps({"error": f"Synthesis agent has no tools (called: {tool_name})"})

    def parse_final_output(self, text: str) -> dict:
        parsed = json.loads(self._strip_json(text))
        return CompanySynthesis(**parsed).model_dump()
