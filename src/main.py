"""
Multi-Agent Due Diligence Analyst — Main Entry Point

Usage:
    # With Brave Search API key (real web search):
    export ANTHROPIC_API_KEY=your-key-here
    export BRAVE_API_KEY=your-key-here
    python -m src.main "Stripe"

    # With mock search (test the agent loop without search API):
    export ANTHROPIC_API_KEY=your-key-here
    export USE_MOCK_SEARCH=true
    python -m src.main "Stripe"
"""

import os
import sys
import asyncio

import anthropic
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown

from src.agents.research import ResearchAgent
from src.agents.financial import FinancialAgent
from src.agents.risk import RiskAgent
from src.agents.social_media import SocialMediaAgent
from src.agents.synthesis import SynthesisAgent, build_synthesis_task
from src.agents.edgar import EdgarAgent
from src.agents.classifier import classify_company
from src.observability.tracer import AgentTracer
from src.observability.agent_db import AgentDB
from src.sources.cache import SourceCache
from src.synthesis.assembler import assemble_report, annotate_claim_ids
from src.synthesis.report_generator import render_report_from_doc
from src.synthesis.pdf_report import render_pdf_report_from_doc
from evals.eval_runner import GroundTruthEvaluator, persist_eval_results, print_scorecard

console = Console()

_AGENT_TIMEOUT = 300  # seconds — p99 observed max is ~174s; 300s catches genuine hangs
_SOFT_BUDGET = int(_AGENT_TIMEOUT * 0.70)  # 210s — soft budget; agent forced to reflect early


async def _run_with_timeout(agent, task: str) -> dict:
    console.print(f"  → {agent.AGENT_NAME} starting...")
    try:
        result = await asyncio.wait_for(agent.run(task), timeout=_AGENT_TIMEOUT)
        status = result.get("status", "?")
        console.print(f"  ✓ {agent.AGENT_NAME} {status}")
        return result
    except asyncio.TimeoutError:
        console.print(f"  [bold red]⚠ {agent.AGENT_NAME} timed out after {_AGENT_TIMEOUT}s[/bold red]")
        return {"status": "partial", "data": None, "gaps": ["Agent timed out — hard budget exceeded"],
                "error_summary": f"Timed out after {_AGENT_TIMEOUT}s"}


async def run_due_diligence(company_name: str, json_only: bool = False) -> AgentDB:
    """Run due diligence on a company using all agents.

    Returns the AgentDB instance for post-run querying.
    """
    console.print()
    console.print(
        Panel(
            f"[bold blue]Due Diligence Research: {company_name}[/bold blue]",
            subtitle="Research + Financial + Risk + Social Media Agents",
        )
    )
    console.print()

    # Initialize shared components
    # 120s per-request HTTP timeout: above the p99 observed LLM response time (~82s)
    # but well below the 300s hard agent timeout, so hung API calls surface quickly.
    client = anthropic.AsyncAnthropic(timeout=120.0)
    tracer = AgentTracer(company_name)
    os.makedirs("outputs", exist_ok=True)
    agent_db = AgentDB(db_path="outputs/agent_log.db")
    source_cache = SourceCache(db_path="outputs/agent_log.db")

    # ── Pre-classification ────────────────────────────────────────
    console.print("[dim]Pre-classifying company...[/dim]")
    company_context = await classify_company(company_name, client, tracer)
    if company_context:
        sector = company_context.get("sector", "")
        ctype = company_context.get("company_type", "")
        bmodel = company_context.get("business_model", "")
        region = company_context.get("primary_region", "")
        console.print(
            f"  [dim]↳ {sector} | {ctype} | {bmodel} | {region}[/dim]"
        )
    console.print()

    # ── Phase 1: All Agents (parallel) ────────────────────────────
    console.print("[bold]Phase 1:[/bold] Running Research, Financial, Risk, Social Media & EDGAR Agents...")
    console.print()

    research_agent = ResearchAgent(tracer=tracer, client=client, db=agent_db, company_context=company_context)
    financial_agent = FinancialAgent(tracer=tracer, client=client, db=agent_db, company_context=company_context)
    risk_agent = RiskAgent(tracer=tracer, client=client, db=agent_db, company_context=company_context)
    social_media_agent = SocialMediaAgent(tracer=tracer, client=client, db=agent_db, company_context=company_context)
    edgar_agent = EdgarAgent(tracer=tracer, client=client, db=agent_db, company_context=company_context, cache=source_cache)

    # Soft budget: force reflection at 70% of the hard timeout, leaving 90s for
    # the model to emit its final JSON. EDGAR is deterministic so it's excluded.
    for _a in (research_agent, financial_agent, risk_agent, social_media_agent):
        _a.soft_budget_seconds = _SOFT_BUDGET

    research_result, financial_result, risk_result, social_media_result, edgar_result = await asyncio.gather(
        _run_with_timeout(research_agent,
            f"Research the company '{company_name}'. Gather comprehensive "
            f"information about what they do, when they were founded, where "
            f"they're headquartered, how many employees they have, who leads "
            f"the company, what products they offer, what technology they use, "
            f"and any recent news or developments."
        ),
        _run_with_timeout(financial_agent,
            f"Research the financials of '{company_name}'. Gather information "
            f"about revenue, revenue growth, profitability, funding rounds, "
            f"valuation, key investors, business/revenue model, key customers, "
            f"financial risks, and any recent financial events or announcements."
        ),
        _run_with_timeout(risk_agent,
            f"Assess the risk profile of '{company_name}'. Search for regulatory "
            f"actions, lawsuits, pending litigation, cybersecurity incidents, "
            f"data breaches, operational risks, reputational controversies, "
            f"and ESG concerns. Rank risks from most to least severe."
        ),
        _run_with_timeout(social_media_agent,
            f"Assess the social media presence and public sentiment for "
            f"'{company_name}'. Search for Twitter/X presence, LinkedIn activity, "
            f"Reddit discussions, Glassdoor reviews, customer complaints, "
            f"notable mentions, and positive signals. Rank by significance."
        ),
        _run_with_timeout(edgar_agent,
            f"Look up '{company_name}' in SEC EDGAR and extract financial facts and "
            f"risk factors from the most recent SEC filing — the annual report (10-K) "
            f"for established public companies, or the registration statement / prospectus "
            f"(S-1 or 424B) for companies that recently went public."
        ),
    )

    research_data = research_result.get("data")
    financial_data = financial_result.get("data")
    risk_data = risk_result.get("data")
    social_media_data = social_media_result.get("data")
    edgar_data = edgar_result.get("data")

    # Pre-assign claim_ids to every upstream DataPoint so the synthesis agent
    # can cite them in synthesized_from, and the assembler can use the same IDs.
    research_data = annotate_claim_ids(research_data)
    financial_data = annotate_claim_ids(financial_data)
    risk_data = annotate_claim_ids(risk_data)
    social_media_data = annotate_claim_ids(social_media_data)
    # EDGAR: also annotate so sec_risk_factors and revenue/profitability DataPoints
    # carry stable _claim_ids that synthesis can cite and the assembler will honour.
    edgar_data = annotate_claim_ids(edgar_data)

    # ── Phase 2: Synthesis ───────────────────────────────────────
    console.print()
    console.print("[bold]Phase 2:[/bold] Running Synthesis Agent...")
    console.print()

    synthesis_agent = SynthesisAgent(tracer=tracer, client=client, db=agent_db)
    synthesis_result = await _run_with_timeout(
        synthesis_agent,
        build_synthesis_task(
            company_name, research_data, financial_data, risk_data, social_media_data,
            edgar_data=edgar_data,
        ),
    )
    synthesis_data = synthesis_result.get("data")

    # ── Phase 3: Generate Report ─────────────────────────────────
    console.print()
    console.print("[bold]Phase 3:[/bold] Generating report...")

    trace_summary = tracer.summary()
    company_slug = company_name.lower().replace(" ", "_").replace(".", "")

    # Assemble canonical ReportDocument — single source of truth
    doc = assemble_report(
        research_data=research_data,
        financial_data=financial_data,
        risk_data=risk_data,
        social_media_data=social_media_data,
        synthesis_data=synthesis_data,
        trace_summary=trace_summary,
        edgar_data=edgar_data,
    )

    # Write canonical JSON
    json_path = os.path.join("outputs", f"report_{company_slug}.json")
    with open(json_path, "w") as f:
        f.write(doc.model_dump_json(indent=2))

    # Render markdown from canonical doc
    report = render_report_from_doc(doc, output_dir="outputs")

    # ── Generate PDF/HTML (skipped with --json-only) ─────────────
    html_path, pdf_path = None, None
    if not json_only:
        try:
            html_path, pdf_path = render_pdf_report_from_doc(doc, output_dir="outputs")
        except Exception as e:
            console.print(f"[yellow]PDF generation failed: {e}[/yellow]")

    # ── Print Results ────────────────────────────────────────────
    console.print()
    console.print(Panel(Markdown(report), title="Due Diligence Report"))

    # Persist trace (run + spans) to SQLite and print summary
    tracer.persist("outputs/agent_log.db", report_json_path=json_path)
    tracer.print_summary()

    # Print status for each agent
    for label, result in [("Research", research_result), ("Financial", financial_result), ("Risk", risk_result), ("Social Media", social_media_result), ("EDGAR", edgar_result), ("Synthesis", synthesis_result)]:
        status = result.get("status", "unknown")
        gaps = result.get("gaps", [])

        if status == "complete":
            console.print(f"[bold green]✓ {label} complete[/bold green]")
        elif status == "partial":
            console.print(f"[bold yellow]⚠ {label} partially complete[/bold yellow]")
            for gap in gaps:
                console.print(f"  - {gap}")
        else:
            console.print(f"[bold red]✗ {label} failed: {status}[/bold red]")
            error = result.get("error_summary")
            if error:
                console.print(f"  Error: {error}")

    # Print Research Agent DB log summary
    runs = agent_db.get_runs("research")
    if runs:
        console.print()
        console.print("[bold blue]Research Agent DB Log:[/bold blue]")
        for run in runs:
            console.print(
                f"  [{run['agent']}] {run['status']} | "
                f"{run['total_turns']} turns, {run['total_tool_calls']} tool calls | "
                f"{run['total_input_tokens']+run['total_output_tokens']:,} tokens | "
                f"${run['total_cost_usd']:.4f} | {run['total_duration_ms']:.0f}ms"
            )
        llm_calls = agent_db.get_llm_calls("research")
        console.print(f"  LLM calls logged: {len(llm_calls)}")
        tool_calls = agent_db.get_tool_calls("research")
        console.print(f"  Tool calls logged: {len(tool_calls)}")

    # ── Phase 3: Ground Truth Eval (if fixture exists) ─────────
    evaluator = GroundTruthEvaluator()
    if company_slug in evaluator.available_companies():
        console.print()
        console.print("[bold]Phase 4:[/bold] Running ground truth evaluation...")
        eval_results = evaluator.evaluate(
            company_slug,
            research_data=research_data,
            financial_data=financial_data,
            trace_id=tracer.run_id,
        )
        persist_eval_results(eval_results, "outputs/agent_log.db")
        print_scorecard(eval_results)

    console.print()
    console.print(f"[bold]Reports saved:[/bold]")
    console.print(f"  JSON:     {json_path}")
    console.print(f"  Markdown: outputs/report_{company_slug}.md")
    if html_path:
        console.print(f"  HTML:     {html_path}")
    if pdf_path:
        console.print(f"  PDF:      {pdf_path}")
    console.print()

    return agent_db


def _dump_db(db: AgentDB):
    """Print full DB contents for debugging."""
    import json as _json
    console.print()
    console.rule("[bold blue]Agent DB Dump[/bold blue]")

    for run in db.get_runs():
        console.print(f"\n[bold][{run['agent']}][/bold] — {run['status']}")
        console.print(f"  Task: {run['task']}")
        console.print(
            f"  Turns: {run['total_turns']} | Tool calls: {run['total_tool_calls']} | "
            f"Tokens: {run['total_input_tokens']+run['total_output_tokens']:,} | "
            f"Cost: ${run['total_cost_usd']:.4f} | Duration: {run['total_duration_ms']:.0f}ms"
        )

    for call in db.get_llm_calls():
        console.print(f"\n[bold cyan]LLM Call[/bold cyan] — {call['agent']} turn {call['turn']} [{call['trace_id']}]")
        console.print(f"  Model: {call['model']}")
        console.print(f"  Tokens: {call['input_tokens']} in / {call['output_tokens']} out | Cost: ${call['cost_usd']:.4f} | Duration: {call['duration_ms']:.0f}ms")
        if call['error']:
            console.print(f"  [red]Error: {call['error']}[/red]")
        if call['response_content']:
            preview = call['response_content'][:200]
            console.print(f"  Response: {preview}{'...' if len(call['response_content']) > 200 else ''}")

    for tc in db.get_tool_calls():
        console.print(f"\n[bold green]Tool Call[/bold green] — {tc['agent']} turn {tc['turn']} — {tc['tool_name']}")
        console.print(f"  Input: {tc['tool_input']}")
        if tc['tool_result']:
            preview = tc['tool_result'][:200]
            console.print(f"  Result: {preview}{'...' if len(tc['tool_result']) > 200 else ''}")
        console.print(f"  Duration: {tc['duration_ms']:.0f}ms")
        if tc['error']:
            console.print(f"  [red]Error: {tc['error']}[/red]")

    console.rule()


def main():
    if len(sys.argv) < 2:
        console.print("[bold red]Usage:[/bold red] python -m src.main <company_name>")
        console.print()
        console.print("Examples:")
        console.print('  python -m src.main "Stripe"')
        console.print('  python -m src.main "Anthropic"')
        console.print('  python -m src.main "Stripe" --dump-db')
        sys.exit(1)

    dump = "--dump-db" in sys.argv
    json_only = "--json-only" in sys.argv
    args = [a for a in sys.argv[1:] if a not in ("--dump-db", "--json-only")]
    company_name = " ".join(args)
    try:
        db = asyncio.run(run_due_diligence(company_name, json_only=json_only))
    except KeyboardInterrupt:
        console.print("\n[yellow]Run interrupted by user (Ctrl+C). Partial results may have been written to outputs/.[/yellow]")
        sys.exit(0)

    if dump:
        _dump_db(db)


if __name__ == "__main__":
    main()
