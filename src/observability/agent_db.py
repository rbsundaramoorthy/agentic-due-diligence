"""
SQLite database for logging agent requests, responses, and metrics.

Stores every LLM turn and tool call with full request/response payloads
so you can inspect, replay, and debug agent runs after the fact.

All tables use natural keys based on (trace_id, agent) rather than
surrogate auto-increment IDs. This aligns with the trace-level
observability in the runs/spans tables.
"""

import json
import sqlite3
import time
from typing import Optional


class AgentDB:
    """SQLite store for agent request/response logging."""

    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS agent_runs (
                trace_id TEXT NOT NULL,
                agent TEXT NOT NULL,
                company_name TEXT,
                task TEXT NOT NULL,
                started_at REAL NOT NULL,
                finished_at REAL,
                status TEXT,
                total_turns INTEGER DEFAULT 0,
                total_tool_calls INTEGER DEFAULT 0,
                total_input_tokens INTEGER DEFAULT 0,
                total_output_tokens INTEGER DEFAULT 0,
                total_cost_usd REAL DEFAULT 0.0,
                total_duration_ms REAL DEFAULT 0.0,
                result_json TEXT,
                error TEXT,
                PRIMARY KEY (trace_id, agent)
            );

            CREATE TABLE IF NOT EXISTS llm_calls (
                trace_id TEXT NOT NULL,
                agent TEXT NOT NULL,
                turn INTEGER NOT NULL,
                timestamp REAL NOT NULL,
                model TEXT NOT NULL,
                system_prompt TEXT,
                request_messages TEXT NOT NULL,
                response_content TEXT,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0.0,
                duration_ms REAL DEFAULT 0.0,
                stop_reason TEXT,
                error TEXT,
                PRIMARY KEY (trace_id, agent, turn)
            );

            CREATE TABLE IF NOT EXISTS tool_calls (
                trace_id TEXT NOT NULL,
                agent TEXT NOT NULL,
                turn INTEGER NOT NULL,
                timestamp REAL NOT NULL,
                tool_name TEXT NOT NULL,
                tool_input TEXT NOT NULL,
                tool_result TEXT,
                duration_ms REAL DEFAULT 0.0,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS messages (
                trace_id TEXT NOT NULL,
                agent TEXT NOT NULL,
                sequence_number INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                timestamp REAL NOT NULL,
                tokens INTEGER DEFAULT 0,
                PRIMARY KEY (trace_id, agent, sequence_number)
            );
        """)

    def start_run(self, agent: str, task: str, company_name: Optional[str] = None, trace_id: Optional[str] = None):
        self.conn.execute(
            "INSERT INTO agent_runs (trace_id, agent, company_name, task, started_at) VALUES (?, ?, ?, ?, ?)",
            (trace_id or "", agent, company_name, task, time.time()),
        )
        self.conn.commit()

    def end_run(
        self,
        trace_id: str,
        agent: str,
        status: str,
        total_turns: int,
        total_tool_calls: int,
        total_input_tokens: int,
        total_output_tokens: int,
        total_cost_usd: float,
        result_json: Optional[str] = None,
        error: Optional[str] = None,
    ):
        now = time.time()
        row = self.conn.execute(
            "SELECT started_at FROM agent_runs WHERE trace_id = ? AND agent = ?",
            (trace_id, agent),
        ).fetchone()
        duration_ms = (now - row["started_at"]) * 1000 if row else 0.0
        self.conn.execute(
            """UPDATE agent_runs SET
                finished_at = ?, status = ?, total_turns = ?,
                total_tool_calls = ?, total_input_tokens = ?,
                total_output_tokens = ?, total_cost_usd = ?,
                total_duration_ms = ?, result_json = ?, error = ?
            WHERE trace_id = ? AND agent = ?""",
            (
                now, status, total_turns, total_tool_calls,
                total_input_tokens, total_output_tokens, total_cost_usd,
                duration_ms, result_json, error, trace_id, agent,
            ),
        )
        self.conn.commit()

    def log_llm_call(
        self,
        agent: str,
        turn: int,
        model: str,
        system_prompt: str,
        request_messages: list,
        response_content: Optional[str] = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        duration_ms: float = 0.0,
        stop_reason: Optional[str] = None,
        error: Optional[str] = None,
        trace_id: Optional[str] = None,
    ):
        self.conn.execute(
            """INSERT INTO llm_calls
                (trace_id, agent, turn, timestamp, model, system_prompt, request_messages,
                 response_content, input_tokens, output_tokens, cost_usd,
                 duration_ms, stop_reason, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trace_id or "", agent, turn, time.time(), model, system_prompt,
                json.dumps(request_messages, default=str),
                response_content, input_tokens, output_tokens, cost_usd,
                duration_ms, stop_reason, error,
            ),
        )
        self.conn.commit()

    def log_tool_call(
        self,
        agent: str,
        turn: int,
        tool_name: str,
        tool_input: dict,
        tool_result: Optional[str] = None,
        duration_ms: float = 0.0,
        error: Optional[str] = None,
        trace_id: Optional[str] = None,
    ):
        self.conn.execute(
            """INSERT INTO tool_calls
                (trace_id, agent, turn, timestamp, tool_name, tool_input,
                 tool_result, duration_ms, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trace_id or "", agent, turn, time.time(), tool_name,
                json.dumps(tool_input), tool_result, duration_ms, error,
            ),
        )
        self.conn.commit()

    def log_message(
        self,
        trace_id: str,
        agent: str,
        sequence_number: int,
        role: str,
        content: str,
        tokens: int = 0,
    ):
        truncated = content[:2000] if content else ""
        self.conn.execute(
            """INSERT INTO messages
                (trace_id, agent, sequence_number, role, content, timestamp, tokens)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (trace_id, agent, sequence_number, role, truncated, time.time(), tokens),
        )
        self.conn.commit()

    # ── Query helpers ────────────────────────────────────────────

    def get_runs(self, agent: Optional[str] = None) -> list:
        if agent:
            rows = self.conn.execute(
                "SELECT * FROM agent_runs WHERE agent = ? ORDER BY started_at", (agent,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM agent_runs ORDER BY started_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_llm_calls(self, agent: Optional[str] = None) -> list:
        if agent:
            rows = self.conn.execute(
                "SELECT * FROM llm_calls WHERE agent = ? ORDER BY turn", (agent,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM llm_calls ORDER BY timestamp"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_tool_calls(self, agent: Optional[str] = None) -> list:
        if agent:
            rows = self.conn.execute(
                "SELECT * FROM tool_calls WHERE agent = ? ORDER BY timestamp", (agent,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM tool_calls ORDER BY timestamp"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_messages(self, trace_id: Optional[str] = None, agent: Optional[str] = None) -> list:
        if trace_id and agent:
            rows = self.conn.execute(
                "SELECT * FROM messages WHERE trace_id = ? AND agent = ? ORDER BY sequence_number",
                (trace_id, agent),
            ).fetchall()
        elif trace_id:
            rows = self.conn.execute(
                "SELECT * FROM messages WHERE trace_id = ? ORDER BY agent, sequence_number",
                (trace_id,),
            ).fetchall()
        elif agent:
            rows = self.conn.execute(
                "SELECT * FROM messages WHERE agent = ? ORDER BY trace_id, sequence_number",
                (agent,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM messages ORDER BY trace_id, agent, sequence_number"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_run_detail(self, trace_id: str, agent: str) -> dict:
        run = dict(self.conn.execute(
            "SELECT * FROM agent_runs WHERE trace_id = ? AND agent = ?",
            (trace_id, agent),
        ).fetchone())
        run["llm_calls"] = [
            dict(r) for r in self.conn.execute(
                "SELECT * FROM llm_calls WHERE trace_id = ? AND agent = ? ORDER BY turn",
                (trace_id, agent),
            ).fetchall()
        ]
        run["tool_calls"] = [
            dict(r) for r in self.conn.execute(
                "SELECT * FROM tool_calls WHERE trace_id = ? AND agent = ? ORDER BY timestamp",
                (trace_id, agent),
            ).fetchall()
        ]
        run["messages"] = [
            dict(r) for r in self.conn.execute(
                "SELECT * FROM messages WHERE trace_id = ? AND agent = ? ORDER BY sequence_number",
                (trace_id, agent),
            ).fetchall()
        ]
        return run
