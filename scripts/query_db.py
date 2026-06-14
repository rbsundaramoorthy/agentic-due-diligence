"""
Query the Agent observability DB after a due diligence run.

Usage:
    export ANTHROPIC_API_KEY=your-key-here
    export BRAVE_API_KEY=your-key-here   # or USE_MOCK_SEARCH=true
    python scripts/query_db.py "Stripe"
"""

import sys
import json
import asyncio

# Add project root to path
sys.path.insert(0, ".")

from src.main import run_due_diligence


def print_separator(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}\n")


def print_traces(db):
    print_separator("TRACES")
    try:
        rows = db.conn.execute("SELECT * FROM runs ORDER BY start_time DESC").fetchall()
    except Exception:
        print("  No traces table found.")
        return
    if not rows:
        print("  No traces recorded.")
        return
    for r in rows:
        r = dict(r)
        print(f"  Trace {r['trace_id']}")
        print(f"    Company:  {r['company_name']}")
        print(f"    Status:   {r['status']}")
        print(f"    Cost:     ${r['total_cost_usd']:.4f}")
        print()

    # Spans summary
    try:
        span_rows = db.conn.execute(
            "SELECT agent, span_type, COUNT(*) as count, "
            "SUM(cost_usd) as cost, SUM(duration_ms) as duration "
            "FROM spans GROUP BY agent, span_type ORDER BY agent"
        ).fetchall()
        if span_rows:
            print("  Spans by agent:")
            for s in span_rows:
                s = dict(s)
                print(f"    {s['agent']:15s} {s['span_type']:10s} {s['count']}x  ${s['cost']:.4f}  {s['duration']:.0f}ms")
            print()
    except Exception:
        pass


def print_runs(db):
    print_separator("AGENT RUNS")
    runs = db.get_runs()
    if not runs:
        print("  No runs recorded.")
        return
    for run in runs:
        tokens = run["total_input_tokens"] + run["total_output_tokens"]
        trace = run.get("trace_id", "N/A")
        print(f"  [{run['agent']}] trace={trace}")
        print(f"    Status:     {run['status']}")
        print(f"    Task:       {run['task'][:80]}...")
        print(f"    Turns:      {run['total_turns']}")
        print(f"    Tool calls: {run['total_tool_calls']}")
        print(f"    Tokens:     {tokens:,} ({run['total_input_tokens']:,} in / {run['total_output_tokens']:,} out)")
        print(f"    Cost:       ${run['total_cost_usd']:.4f}")
        print(f"    Duration:   {run['total_duration_ms']:.0f}ms")
        if run["error"]:
            print(f"    Error:      {run['error']}")
        print()


def print_llm_calls(db):
    print_separator("LLM CALLS")
    calls = db.get_llm_calls()
    if not calls:
        print("  No LLM calls recorded.")
        return
    for call in calls:
        trace = call.get("trace_id", "N/A")
        print(f"  LLM Call [{call['agent']}] turn {call['turn']} trace={trace}")
        print(f"    Model:    {call['model']}")
        print(f"    Tokens:   {call['input_tokens']} in / {call['output_tokens']} out")
        print(f"    Cost:     ${call['cost_usd']:.4f}")
        print(f"    Duration: {call['duration_ms']:.0f}ms")
        if call["stop_reason"]:
            print(f"    Stop:     {call['stop_reason']}")
        if call["error"]:
            print(f"    Error:    {call['error']}")

        # Show request message count
        try:
            msgs = json.loads(call["request_messages"])
            print(f"    Request:  {len(msgs)} messages")
        except (json.JSONDecodeError, TypeError):
            pass

        # Show response preview
        if call["response_content"]:
            try:
                content = json.loads(call["response_content"])
                for block in content:
                    if block.get("type") == "text" and block.get("text"):
                        preview = block["text"][:150]
                        suffix = "..." if len(block["text"]) > 150 else ""
                        print(f"    Response: [text] {preview}{suffix}")
                    elif block.get("type") == "tool_use":
                        print(f"    Response: [tool_use] {block.get('name')}({json.dumps(block.get('input', {}))[:100]})")
            except (json.JSONDecodeError, TypeError):
                preview = call["response_content"][:150]
                print(f"    Response: {preview}...")
        print()


def print_tool_calls(db):
    print_separator("TOOL CALLS")
    calls = db.get_tool_calls()
    if not calls:
        print("  No tool calls recorded.")
        return
    for tc in calls:
        trace = tc.get("trace_id", "N/A")
        print(f"  Tool Call [{tc['agent']}] — {tc['tool_name']} turn {tc['turn']} trace={trace}")
        print(f"    Input:    {tc['tool_input'][:120]}")
        print(f"    Duration: {tc['duration_ms']:.0f}ms")
        if tc["error"]:
            print(f"    Error:    {tc['error']}")
        if tc["tool_result"]:
            preview = tc["tool_result"][:150]
            suffix = "..." if len(tc["tool_result"]) > 150 else ""
            print(f"    Result:   {preview}{suffix}")
        print()


def print_raw_sql_examples(db):
    print_separator("RAW SQL EXAMPLES")

    # Total cost by agent
    rows = db.conn.execute(
        "SELECT agent, SUM(cost_usd) as total_cost, COUNT(*) as calls FROM llm_calls GROUP BY agent"
    ).fetchall()
    print("  Cost by agent:")
    for r in rows:
        print(f"    {r['agent']}: ${r['total_cost']:.4f} ({r['calls']} calls)")

    # Tool usage breakdown
    rows = db.conn.execute(
        "SELECT tool_name, COUNT(*) as count, AVG(duration_ms) as avg_ms FROM tool_calls GROUP BY tool_name"
    ).fetchall()
    print("\n  Tool usage:")
    for r in rows:
        print(f"    {r['tool_name']}: {r['count']}x, avg {r['avg_ms']:.0f}ms")

    # Largest LLM call by tokens
    row = db.conn.execute(
        "SELECT agent, turn, input_tokens + output_tokens as total_tokens FROM llm_calls ORDER BY total_tokens DESC LIMIT 1"
    ).fetchone()
    if row:
        print(f"\n  Largest LLM call: {row['agent']} turn {row['turn']} — {row['total_tokens']:,} tokens")

    # Prompt versions
    try:
        rows = db.conn.execute(
            "SELECT agent, prompt_version, COUNT(*) as calls "
            "FROM spans WHERE prompt_version IS NOT NULL "
            "GROUP BY agent, prompt_version ORDER BY agent"
        ).fetchall()
        if rows:
            print("\n  Prompt versions:")
            for r in rows:
                print(f"    {r['agent']}: v{r['prompt_version']} ({r['calls']} calls)")
    except Exception:
        pass

    print()


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/query_db.py <company_name>")
        print('Example: python scripts/query_db.py "Stripe"')
        sys.exit(1)

    company_name = " ".join(sys.argv[1:])
    print(f"Running due diligence for: {company_name}")
    print("(DB will be queried after the run completes)\n")

    db = asyncio.run(run_due_diligence(company_name))

    print_traces(db)
    print_runs(db)
    print_llm_calls(db)
    print_tool_calls(db)
    print_raw_sql_examples(db)


if __name__ == "__main__":
    main()
