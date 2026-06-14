"""
Observability and tracing for the agent system.

Built on the same principles as the TIAA observability platform:
surface business-level signals (cost, quality) alongside
system-level signals (latency, errors, token usage).

Every LLM call, tool invocation, and external API call gets a span.
At the end of a run, you can see exactly what happened, how long it took,
and how much it cost — broken down by agent.

Terminology:
- **Trace (run)**: A single end-to-end due diligence run, identified by run_id.
- **Span**: An individual LLM call or tool call within that trace.
"""

import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import List, Optional, Dict

from rich.console import Console
from rich.table import Table


# Cost per million tokens (Claude Sonnet 4 pricing as of Mar 2026)
# Update these when pricing changes
MODEL_COSTS = {
    "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
}


def _short_uuid() -> str:
    """Generate a 12-char hex UUID (48 bits of entropy)."""
    return uuid.uuid4().hex[:12]


@dataclass
class TraceSpan:
    """A single unit of work in the agent system."""
    name: str
    agent: str
    span_type: str               # "llm_call", "tool_call", "external_api"
    run_id: str = ""
    span_id: str = ""
    start_time: float = 0.0
    end_time: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    prompt_version: Optional[str] = None
    tool_name: Optional[str] = None
    external_api: Optional[str] = None
    error: Optional[str] = None
    metadata: Dict = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        if self.end_time == 0:
            return 0
        return (self.end_time - self.start_time) * 1000

    @property
    def cost_usd(self) -> float:
        if not self.model or self.model not in MODEL_COSTS:
            return 0.0
        rates = MODEL_COSTS[self.model]
        input_cost = (self.input_tokens / 1_000_000) * rates["input"]
        output_cost = (self.output_tokens / 1_000_000) * rates["output"]
        return input_cost + output_cost


class AgentTracer:
    """Traces all operations across the agent system.

    Each AgentTracer instance represents a single trace (run) — one
    end-to-end due diligence execution. Every span created through
    this tracer carries the same run_id.

    Usage:
        tracer = AgentTracer("Stripe")
        span = tracer.start_span("extract_company_info", "research", "llm_call")
        # ... do work ...
        tracer.end_span(span, input_tokens=500, output_tokens=200, model="claude-sonnet-4-20250514")
        tracer.persist("outputs/agent_log.db")
        tracer.print_summary()
    """

    def __init__(self, company_name: str = ""):
        self.run_id: str = _short_uuid()
        self.company_name: str = company_name
        self.start_time: float = time.time()
        self.end_time: Optional[float] = None
        self.spans: List[TraceSpan] = []
        self._active_spans: Dict[str, TraceSpan] = {}

    def start_span(
        self, name: str, agent: str, span_type: str, **metadata
    ) -> TraceSpan:
        span = TraceSpan(
            name=name,
            agent=agent,
            span_type=span_type,
            run_id=self.run_id,
            span_id=_short_uuid(),
            start_time=time.time(),
            metadata=metadata,
        )
        self.spans.append(span)
        self._active_spans[name] = span
        return span

    def end_span(self, span: TraceSpan, **kwargs):
        span.end_time = time.time()
        for k, v in kwargs.items():
            if hasattr(span, k):
                setattr(span, k, v)
        self._active_spans.pop(span.name, None)

    def log_error(self, agent: str, error: str):
        """Log an error without a span (e.g., unexpected exception)."""
        span = TraceSpan(
            name="error",
            agent=agent,
            span_type="error",
            run_id=self.run_id,
            span_id=_short_uuid(),
            start_time=time.time(),
            end_time=time.time(),
            error=error,
        )
        self.spans.append(span)

    def summary(self) -> dict:
        llm_spans = [s for s in self.spans if s.span_type == "llm_call"]
        tool_spans = [s for s in self.spans if s.span_type == "tool_call"]
        error_spans = [s for s in self.spans if s.error]

        end = self.end_time or time.time()
        return {
            "trace_id": self.run_id,
            "company_name": self.company_name,
            "total_duration_ms": (end - self.start_time) * 1000,
            "total_input_tokens": sum(s.input_tokens for s in llm_spans),
            "total_output_tokens": sum(s.output_tokens for s in llm_spans),
            "total_cost_usd": sum(s.cost_usd for s in llm_spans),
            "total_llm_calls": len(llm_spans),
            "total_tool_calls": len(tool_spans),
            "errors": [
                {"agent": s.agent, "error": s.error} for s in error_spans
            ],
            "by_agent": self._by_agent(),
        }

    def _by_agent(self) -> dict:
        agents: Dict[str, dict] = {}
        for s in self.spans:
            if s.agent not in agents:
                agents[s.agent] = {
                    "llm_calls": 0,
                    "tool_calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_usd": 0.0,
                    "duration_ms": 0.0,
                    "errors": 0,
                }
            a = agents[s.agent]
            a["duration_ms"] += s.duration_ms
            if s.span_type == "llm_call":
                a["llm_calls"] += 1
                a["input_tokens"] += s.input_tokens
                a["output_tokens"] += s.output_tokens
                a["cost_usd"] += s.cost_usd
            elif s.span_type == "tool_call":
                a["tool_calls"] += 1
            if s.error:
                a["errors"] += 1
        return agents

    # ── SQLite persistence ───────────────────────────────────────

    def persist(self, db_path: str, report_json_path: Optional[str] = None):
        """Write this trace (run + spans) to SQLite.

        Creates tables if they don't exist. Each call writes one row
        to `runs` and one row per span to `spans`.
        report_json_path is stored so the dashboard can link to the canonical JSON.
        """
        self.end_time = time.time()
        s = self.summary()

        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                trace_id TEXT PRIMARY KEY,
                company_name TEXT,
                start_time REAL NOT NULL,
                end_time REAL,
                total_cost_usd REAL DEFAULT 0.0,
                status TEXT,
                report_json_path TEXT
            );

            CREATE TABLE IF NOT EXISTS spans (
                span_id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL,
                name TEXT NOT NULL,
                agent TEXT NOT NULL,
                span_type TEXT NOT NULL,
                start_time REAL NOT NULL,
                end_time REAL,
                duration_ms REAL DEFAULT 0.0,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                model TEXT,
                prompt_version TEXT,
                tool_name TEXT,
                cost_usd REAL DEFAULT 0.0,
                error TEXT,
                FOREIGN KEY (trace_id) REFERENCES runs(trace_id)
            );
        """)

        # Migrate existing DBs that pre-date the report_json_path column
        try:
            conn.execute("ALTER TABLE runs ADD COLUMN report_json_path TEXT")
        except Exception:
            pass  # column already exists

        # Determine status from spans
        has_errors = any(sp.error for sp in self.spans)
        status = "complete" if not has_errors else "partial"

        conn.execute(
            """INSERT OR REPLACE INTO runs
                (trace_id, company_name, start_time, end_time, total_cost_usd, status, report_json_path)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (self.run_id, self.company_name, self.start_time,
             self.end_time, s["total_cost_usd"], status, report_json_path),
        )

        for sp in self.spans:
            conn.execute(
                """INSERT OR REPLACE INTO spans
                    (span_id, trace_id, name, agent, span_type, start_time,
                     end_time, duration_ms, input_tokens, output_tokens,
                     model, prompt_version, tool_name, cost_usd, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (sp.span_id, sp.run_id, sp.name, sp.agent, sp.span_type,
                 sp.start_time, sp.end_time, sp.duration_ms,
                 sp.input_tokens, sp.output_tokens, sp.model,
                 sp.prompt_version, sp.tool_name, sp.cost_usd, sp.error),
            )

        conn.commit()
        conn.close()

    # ── Pretty printing ──────────────────────────────────────────

    def print_summary(self):
        """Pretty-print a trace summary to the terminal using Rich."""
        console = Console()
        s = self.summary()

        console.print()
        console.rule("[bold blue]Trace Summary[/bold blue]")

        # Overall stats
        overview = Table(show_header=False, box=None, padding=(0, 2))
        overview.add_column(style="bold")
        overview.add_column()
        overview.add_row("Run ID", self.run_id)
        overview.add_row("Company", self.company_name or "(not set)")
        overview.add_row("Total Duration", f"{s['total_duration_ms']:.0f} ms")
        overview.add_row("LLM Calls", str(s["total_llm_calls"]))
        overview.add_row("Tool Calls", str(s["total_tool_calls"]))
        overview.add_row(
            "Tokens (in/out)",
            f"{s['total_input_tokens']:,} / {s['total_output_tokens']:,}",
        )
        overview.add_row("Total Cost", f"${s['total_cost_usd']:.4f}")
        overview.add_row("Errors", str(len(s["errors"])))
        console.print(overview)

        # Per-agent breakdown
        if s["by_agent"]:
            console.print()
            agent_table = Table(title="By Agent")
            agent_table.add_column("Agent", style="cyan")
            agent_table.add_column("LLM Calls", justify="right")
            agent_table.add_column("Tool Calls", justify="right")
            agent_table.add_column("Tokens", justify="right")
            agent_table.add_column("Cost", justify="right")
            agent_table.add_column("Duration", justify="right")
            agent_table.add_column("Errors", justify="right")

            for agent_name, data in s["by_agent"].items():
                tokens = f"{data['input_tokens']:,} / {data['output_tokens']:,}"
                agent_table.add_row(
                    agent_name,
                    str(data["llm_calls"]),
                    str(data["tool_calls"]),
                    tokens,
                    f"${data['cost_usd']:.4f}",
                    f"{data['duration_ms']:.0f} ms",
                    str(data["errors"]),
                )
            console.print(agent_table)

        # Errors
        if s["errors"]:
            console.print()
            console.print("[bold red]Errors:[/bold red]")
            for err in s["errors"]:
                console.print(f"  [{err['agent']}] {err['error']}")

        console.rule()
