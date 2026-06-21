"""
Base agent class with state machine.

Every agent in the system inherits from this. It provides:
- A state machine (PLANNING → EXECUTING → REFLECTING → COMPLETE/RETRY/FAILED)
- Integration with the tracer for observability
- Retry logic with configurable limits
- A standard interface that the Orchestrator can call

This mirrors the Configuration-as-Code framework from TIAA, where every
pipeline stage had explicit states, transitions, and validation checkpoints.
"""

import json
import time
from abc import ABC, abstractmethod
from datetime import date
from enum import Enum
from typing import Optional, List

import anthropic
from rich.console import Console

from src.observability.tracer import AgentTracer
from src.observability.agent_db import AgentDB
from src.tools.web_search import web_search, web_fetch

console = Console()

# Shared tool definitions used by all web-searching agents
_WEB_SEARCH_TOOL = {
    "name": "web_search",
    "description": (
        "Search the web for information. Returns results with title, URL, and snippet. "
        "Use short, specific queries (3-6 words) for best results."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query (keep it short and specific)"},
            "max_results": {"type": "integer", "description": "Max results to return (default 5)", "default": 5},
        },
        "required": ["query"],
    },
}

_WEB_FETCH_TOOL = {
    "name": "web_fetch",
    "description": (
        "Fetch the full text content of a web page. Use this when a search result "
        "snippet looks promising but you need more detail. Returns truncated text (~8000 chars)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to fetch"},
        },
        "required": ["url"],
    },
}


def strip_json(text: str) -> str:
    """Strip markdown fences and surrounding prose from an LLM response.

    Handles: ```json ... ```, plain ``` ... ```, and preamble text before {.
    Raises ValueError if no JSON object is found.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned[cleaned.index("\n") + 1:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    if not cleaned.startswith("{"):
        brace_idx = cleaned.find("{")
        if brace_idx == -1:
            raise ValueError("No JSON object found in response")
        cleaned = cleaned[brace_idx:]

    if not cleaned.endswith("}"):
        last_brace = cleaned.rfind("}")
        if last_brace == -1:
            raise ValueError("No closing brace found in response")
        cleaned = cleaned[:last_brace + 1]

    return cleaned


class AgentState(str, Enum):
    PLANNING = "planning"
    EXECUTING = "executing"
    REFLECTING = "reflecting"
    COMPLETE = "complete"
    FAILED = "failed"
    RETRY = "retry"


class BaseAgent(ABC):
    """Base class for all specialist agents.

    Subclasses implement:
        - get_tools() → list of tool definitions for the LLM
        - get_system_prompt() → system prompt specific to this agent
        - handle_tool_call(name, input) → execute the tool and return result
        - parse_final_output(messages) → extract structured data from conversation

    The base class handles:
        - The agent loop (plan → execute → reflect → complete/retry)
        - Tool calling via the Anthropic API
        - Tracing every LLM call and tool invocation
        - Retry logic with max attempts
    """

    AGENT_NAME: str = "base"
    MODEL: str = "claude-sonnet-4-6"
    PROMPT_VERSION: str = "1.0"
    MAX_RETRIES: int = 3
    MAX_TURNS: int = 10  # Max tool-use turns before forcing completion
    DEFAULT_MAX_TOKENS: int = 4096

    def __init__(
        self,
        tracer: AgentTracer,
        client: anthropic.AsyncAnthropic,
        db: Optional[AgentDB] = None,
        company_context: Optional[dict] = None,
    ):
        self.tracer = tracer
        self.client = client
        self.db = db
        self.company_context = company_context
        self.state = AgentState.PLANNING
        self.messages: List[dict] = []
        self.retries = 0
        self.soft_budget_seconds: Optional[float] = None

    def _format_context(self) -> str:
        """Render current date and optional company_context as a prompt block."""
        today = date.today()
        current_year = today.year
        prior_year = current_year - 1
        parts = [
            f"\nCURRENT DATE: {today.isoformat()} — always prefer {prior_year} or {current_year} data over older figures.",
        ]
        if self.company_context:
            c = self.company_context
            ctx = []
            if c.get("sector"):
                ctx.append(f"- Sector: {c['sector']}")
            if c.get("company_type"):
                ctx.append(f"- Company type: {c['company_type']}")
            if c.get("business_model"):
                ctx.append(f"- Business model: {c['business_model']}")
            if c.get("primary_region"):
                ctx.append(f"- Primary region: {c['primary_region']}")
            if c.get("is_likely_public") is not None:
                ctx.append(f"- Likely SEC-reporting public company: {c['is_likely_public']}")
            if c.get("is_government_contractor") is not None:
                ctx.append(f"- Likely US federal contractor: {c['is_government_contractor']}")
            if c.get("legal_name"):
                ctx.append(f"- SEC-registered legal name: {c['legal_name']}")
            if c.get("ticker"):
                ctx.append(f"- Stock ticker: {c['ticker']}")
            if c.get("key_context"):
                ctx.append(f"- Key context: {c['key_context']}")
            if ctx:
                parts.append(
                    "COMPANY CONTEXT (use this to target your research and searches):\n"
                    + "\n".join(ctx)
                )
        return "\n".join(parts) + "\n"

    def _strip_json(self, text: str) -> str:
        return strip_json(text)

    def _transition(self, new_state: AgentState):
        old = self.state
        self.state = new_state
        console.print(
            f"  [dim][{self.AGENT_NAME}][/dim] {old.value} → "
            f"[bold]{new_state.value}[/bold]"
        )

    async def run(self, task: str) -> dict:
        """Main entry point. Runs the agent loop and returns structured output.

        The loop:
        1. Send task + tools to the LLM
        2. If LLM returns tool_use → execute tool, feed result back (EXECUTING)
        3. If LLM returns text (no more tool calls) → parse output (REFLECTING)
        4. If output is sufficient → COMPLETE
        5. If output is insufficient and retries remain → RETRY with refined task
        6. If retries exhausted → FAILED with partial results
        """
        self._transition(AgentState.PLANNING)

        self.messages = [{"role": "user", "content": task}]
        tools = self.get_tools()
        system_prompt = self.get_system_prompt()

        # Start DB run tracking
        trace_id = self.tracer.run_id
        if self.db:
            self.db.start_run(self.AGENT_NAME, task, trace_id=trace_id)

        total_input_tokens = 0
        total_output_tokens = 0
        total_cost = 0.0
        total_tool_calls = 0
        message_seq = 0
        run_start = time.monotonic()
        _budget_forced = False
        # Sticky: once a max_tokens truncation forces budget exhaustion, it must
        # persist across turns. Otherwise the per-turn recompute below clobbers it
        # and the model re-truncates every retry (4096-token storm until timeout).
        _max_tokens_truncated = False

        # Log initial user task as first message
        if self.db:
            self.db.log_message(
                trace_id=trace_id, agent=self.AGENT_NAME,
                sequence_number=message_seq, role="user", content=task,
            )
            message_seq += 1

        for turn in range(self.MAX_TURNS):
            # Check soft budget before the LLM call — if exceeded, omit tools so
            # the model is forced to emit text (can't call tools if none are offered).
            # Must be checked here, not after, to avoid API protocol violations:
            # if the previous turn returned tool_use, we must send tool_results
            # before removing tools.
            # Two distinct budget conditions, deliberately NOT conflated:
            #   • soft-budget cutoff = run was halted mid-work by the wall-clock
            #     timer; tools removed and the model told to fill unknowns. This is
            #     a genuinely PARTIAL outcome → drives _budget_forced (the status).
            #   • max_tokens stickiness = an earlier turn truncated and we bumped to
            #     a higher token budget; if the run then recovers to a clean terminal
            #     emit it is COMPLETE, not partial → must NOT set _budget_forced.
            # Both feed _budget_exhausted (tool removal + 8192 bump); only the soft
            # cutoff feeds _budget_forced. Status is then derived from the terminal
            # outcome, not from the mere presence of a max_tokens bump in history.
            _soft_budget_hit = (
                self.soft_budget_seconds is not None
                and time.monotonic() - run_start >= self.soft_budget_seconds
            )
            _budget_exhausted = _max_tokens_truncated or _soft_budget_hit
            if _soft_budget_hit:
                _budget_forced = True

            effective_system = system_prompt
            if _budget_exhausted:
                effective_system = (
                    system_prompt
                    + "\n\n⚠ RESEARCH BUDGET REACHED: Stop calling tools. "
                    "Emit your final structured JSON output now using everything "
                    "gathered so far. For any field you did not reach, use "
                    'value="unknown", confidence="unknown", sources=[], '
                    'reasoning="Research budget reached before investigation."'
                )

            # Call the LLM
            span = self.tracer.start_span(
                name=f"{self.AGENT_NAME}_llm_turn_{turn}",
                agent=self.AGENT_NAME,
                span_type="llm_call",
            )

            try:
                call_kwargs: dict = dict(
                    model=self.MODEL,
                    max_tokens=8192 if _budget_exhausted else self.DEFAULT_MAX_TOKENS,
                    system=effective_system,
                    messages=self.messages,
                )
                if not _budget_exhausted:
                    call_kwargs["tools"] = tools
                response = await self.client.messages.create(**call_kwargs)
                self.tracer.end_span(
                    span,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    model=self.MODEL,
                    prompt_version=self.PROMPT_VERSION,
                )
                total_input_tokens += response.usage.input_tokens
                total_output_tokens += response.usage.output_tokens
                total_cost += span.cost_usd

                # Log LLM call to DB
                if self.db:
                    response_json = json.dumps(
                        [{"type": b.type, "text": getattr(b, "text", None),
                          "name": getattr(b, "name", None),
                          "input": getattr(b, "input", None)}
                         for b in response.content],
                        default=str,
                    )
                    self.db.log_llm_call(
                        agent=self.AGENT_NAME,
                        turn=turn,
                        model=self.MODEL,
                        system_prompt=system_prompt,
                        request_messages=self.messages,
                        response_content=response_json,
                        input_tokens=response.usage.input_tokens,
                        output_tokens=response.usage.output_tokens,
                        cost_usd=span.cost_usd,
                        duration_ms=span.duration_ms,
                        stop_reason=getattr(response, "stop_reason", None),
                        trace_id=trace_id,
                    )
                    # Log assistant message for conversation replay
                    self.db.log_message(
                        trace_id=trace_id, agent=self.AGENT_NAME,
                        sequence_number=message_seq, role="assistant",
                        content=response_json,
                        tokens=response.usage.output_tokens,
                    )
                    message_seq += 1
            except Exception as e:
                self.tracer.end_span(span, error=str(e))
                self.tracer.log_error(self.AGENT_NAME, str(e))
                if self.db:
                    self.db.log_llm_call(
                        agent=self.AGENT_NAME, turn=turn, model=self.MODEL,
                        system_prompt=system_prompt, request_messages=self.messages,
                        error=str(e), trace_id=trace_id,
                    )
                    self.db.end_run(
                        trace_id=trace_id, agent=self.AGENT_NAME,
                        status="failed", total_turns=turn + 1,
                        total_tool_calls=total_tool_calls,
                        total_input_tokens=total_input_tokens,
                        total_output_tokens=total_output_tokens,
                        total_cost_usd=total_cost, error=str(e),
                    )
                self._transition(AgentState.FAILED)
                return {
                    "status": "failed",
                    "data": None,
                    "gaps": ["LLM call failed"],
                    "error_summary": str(e),
                }

            # Process response blocks
            assistant_content = response.content
            self.messages.append({"role": "assistant", "content": assistant_content})

            # Check if there are tool calls to handle
            tool_use_blocks = [
                b for b in assistant_content if b.type == "tool_use"
            ]

            if tool_use_blocks:
                self._transition(AgentState.EXECUTING)
                tool_results = []

                for tool_block in tool_use_blocks:
                    # Trace the tool call
                    tool_span = self.tracer.start_span(
                        name=f"{self.AGENT_NAME}_tool_{tool_block.name}",
                        agent=self.AGENT_NAME,
                        span_type="tool_call",
                        tool_name=tool_block.name,
                    )

                    try:
                        result = await self.handle_tool_call(
                            tool_block.name, tool_block.input
                        )
                        self.tracer.end_span(tool_span)
                    except Exception as e:
                        result = json.dumps({"error": str(e)})
                        self.tracer.end_span(tool_span, error=str(e))

                    total_tool_calls += 1

                    # Log tool call to DB
                    if self.db:
                        self.db.log_tool_call(
                            agent=self.AGENT_NAME,
                            turn=turn,
                            tool_name=tool_block.name,
                            tool_input=tool_block.input,
                            tool_result=result[:10000] if result else None,
                            duration_ms=tool_span.duration_ms,
                            error=tool_span.error,
                            trace_id=trace_id,
                        )
                        # Log tool result for conversation replay
                        self.db.log_message(
                            trace_id=trace_id, agent=self.AGENT_NAME,
                            sequence_number=message_seq, role="tool_result",
                            content=f"[{tool_block.name}] {result[:1900] if result else ''}",
                        )
                        message_seq += 1

                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_block.id,
                            "content": result,
                        }
                    )

                self.messages.append({"role": "user", "content": tool_results})
                # Continue the loop — LLM will process tool results

            else:
                # No tool calls — LLM has finished. Extract the text response.
                self._transition(AgentState.REFLECTING)
                text_blocks = [
                    b.text for b in assistant_content if b.type == "text"
                ]
                final_text = "\n".join(text_blocks)

                # Try to parse structured output
                try:
                    parsed = self.parse_final_output(final_text)
                    self._transition(AgentState.COMPLETE)
                    _final_status = "partial" if _budget_forced else "complete"
                    if self.db:
                        self.db.end_run(
                            trace_id=trace_id, agent=self.AGENT_NAME,
                            status=_final_status, total_turns=turn + 1,
                            total_tool_calls=total_tool_calls,
                            total_input_tokens=total_input_tokens,
                            total_output_tokens=total_output_tokens,
                            total_cost_usd=total_cost,
                            result_json=json.dumps(parsed, default=str),
                        )
                    return {
                        "status": _final_status,
                        "data": parsed,
                        "gaps": ["Research time budget reached; some fields may have limited coverage"] if _budget_forced else [],
                        "error_summary": None,
                    }
                except Exception as e:
                    # Parsing failed — retry if possible.
                    # If the model was cut off by max_tokens, force _budget_exhausted
                    # so the retry call omits tools and the model emits JSON directly
                    # rather than calling more tools (which grows context and repeats
                    # the truncation).
                    if getattr(response, "stop_reason", None) == "max_tokens":
                        _budget_exhausted = True
                        _max_tokens_truncated = True
                    if self.retries < self.MAX_RETRIES:
                        self.retries += 1
                        self._transition(AgentState.RETRY)
                        retry_msg = (
                            f"Your response could not be parsed: {e}. "
                            "Please respond ONLY with the JSON object "
                            "matching the required schema. No markdown, "
                            "no backticks, just raw JSON."
                        )
                        self.messages.append(
                            {"role": "user", "content": retry_msg}
                        )
                        if self.db:
                            self.db.log_message(
                                trace_id=trace_id, agent=self.AGENT_NAME,
                                sequence_number=message_seq, role="user",
                                content=retry_msg,
                            )
                            message_seq += 1
                        continue
                    else:
                        self._transition(AgentState.FAILED)
                        if self.db:
                            self.db.end_run(
                                trace_id=trace_id, agent=self.AGENT_NAME,
                                status="partial", total_turns=turn + 1,
                                total_tool_calls=total_tool_calls,
                                total_input_tokens=total_input_tokens,
                                total_output_tokens=total_output_tokens,
                                total_cost_usd=total_cost,
                                result_json=json.dumps({"raw_text": final_text}),
                                error=str(e),
                            )
                        return {
                            "status": "partial",
                            "data": {"raw_text": final_text},
                            "gaps": ["Could not parse structured output"],
                            "error_summary": str(e),
                        }

        # Exhausted MAX_TURNS
        self._transition(AgentState.FAILED)
        if self.db:
            self.db.end_run(
                trace_id=trace_id, agent=self.AGENT_NAME,
                status="partial", total_turns=self.MAX_TURNS,
                total_tool_calls=total_tool_calls,
                total_input_tokens=total_input_tokens,
                total_output_tokens=total_output_tokens,
                total_cost_usd=total_cost,
                error=f"Agent did not complete within {self.MAX_TURNS} turns",
            )
        return {
            "status": "partial",
            "data": None,
            "gaps": ["Exhausted maximum tool-use turns"],
            "error_summary": f"Agent did not complete within {self.MAX_TURNS} turns",
        }

    @abstractmethod
    def get_tools(self) -> list:
        """Return the list of tool definitions for this agent."""
        ...

    @abstractmethod
    def get_system_prompt(self) -> str:
        """Return the system prompt for this agent."""
        ...

    @abstractmethod
    async def handle_tool_call(self, tool_name: str, tool_input: dict) -> str:
        """Execute a tool and return the result as a string."""
        ...

    @abstractmethod
    def parse_final_output(self, text: str) -> dict:
        """Parse the LLM's final text response into structured data."""
        ...


class WebSearchMixin(BaseAgent):
    """Provides standard web_search + web_fetch tools.

    All four specialist agents (Research, Financial, Risk, Social Media) do web
    research with the same two tools and identical handling logic. This mixin
    eliminates that duplication — subclass this instead of BaseAgent directly.
    """

    MAX_SEARCH_RESULTS: int = 5   # cap per search call regardless of agent request
    MAX_FETCHES: int = 4          # per-run cap on web_fetch calls

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._fetch_count: int = 0

    def get_tools(self) -> list:
        return [_WEB_SEARCH_TOOL, _WEB_FETCH_TOOL]

    async def handle_tool_call(self, tool_name: str, tool_input: dict) -> str:
        if tool_name == "web_search":
            max_results = min(tool_input.get("max_results", 5), self.MAX_SEARCH_RESULTS)
            return await web_search(query=tool_input["query"], max_results=max_results)
        elif tool_name == "web_fetch":
            if self._fetch_count >= self.MAX_FETCHES:
                return json.dumps({
                    "error": "web_fetch budget exhausted",
                    "note": "No further fetches available. Use gathered data to complete analysis.",
                })
            self._fetch_count += 1
            try:
                return await web_fetch(url=tool_input["url"])
            except Exception as e:
                return json.dumps({"error": f"Failed to fetch URL: {e}"})
        return json.dumps({"error": f"Unknown tool: {tool_name}"})
