"""
Streamlit dashboard for querying the Agent observability DB.

Usage:
    streamlit run scripts/dashboard.py
"""

import json
import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

DB_PATH = Path("outputs/agent_log.db")

st.set_page_config(page_title="Agent DB Explorer", layout="wide")
st.title("Agent DB Explorer")


@st.cache_resource
def get_connection():
    if not DB_PATH.exists():
        return None
    return sqlite3.connect(str(DB_PATH), check_same_thread=False)


conn = get_connection()

if conn is None:
    st.error(
        f"Database not found at `{DB_PATH}`. "
        "Run the pipeline first:\n\n"
        "```bash\n"
        'python -m src.main "Stripe"\n'
        "```"
    )
    st.stop()

# Ensure eval_results table exists (created on first eval, but dashboard may load before that)
conn.execute("""
    CREATE TABLE IF NOT EXISTS eval_results (
        trace_id TEXT,
        company_name TEXT NOT NULL,
        agent TEXT NOT NULL,
        field_name TEXT NOT NULL,
        expected TEXT NOT NULL,
        actual TEXT NOT NULL,
        confidence TEXT,
        match INTEGER NOT NULL,
        match_type TEXT NOT NULL,
        PRIMARY KEY (trace_id, agent, field_name)
    )
""")

# ── Sidebar: table picker + raw SQL ──────────────────────────────

st.sidebar.header("Navigation")
page = st.sidebar.radio(
    "View",
    ["Overview", "Traces", "Agent Runs", "Conversation Replay", "LLM Calls", "Tool Calls", "Evals", "Raw SQL"],
)

# ── Helper ───────────────────────────────────────────────────────


def run_query(sql: str, params: tuple = ()) -> pd.DataFrame:
    try:
        df = pd.read_sql_query(sql, conn, params=params)
        df.index.name = "seq_id"
        return df
    except Exception as e:
        st.error(f"Query error: {e}")
        return pd.DataFrame()


# ── Overview ─────────────────────────────────────────────────────

if page == "Overview":
    st.header("Overview")

    col1, col2, col3, col4, col5 = st.columns(5)

    traces = run_query("SELECT COUNT(*) as count FROM runs")
    agent_runs = run_query("SELECT COUNT(*) as count FROM agent_runs")
    llm = run_query("SELECT COUNT(*) as count, SUM(input_tokens) as inp, SUM(output_tokens) as out, SUM(cost_usd) as cost FROM llm_calls")
    tools = run_query("SELECT COUNT(*) as count FROM tool_calls")
    spans = run_query("SELECT COUNT(*) as count FROM spans")

    col1.metric("Traces", int(traces["count"].iloc[0]) if not traces.empty else 0)
    col2.metric("Agent Runs", int(agent_runs["count"].iloc[0]) if not agent_runs.empty else 0)
    col3.metric("LLM Calls", int(llm["count"].iloc[0]) if not llm.empty else 0)
    col4.metric("Tool Calls", int(tools["count"].iloc[0]) if not tools.empty else 0)

    total_cost = float(llm["cost"].iloc[0]) if not llm.empty and llm["cost"].iloc[0] else 0
    col5.metric("Total Cost", f"${total_cost:.4f}")

    st.subheader("Cost by Agent")
    cost_df = run_query(
        "SELECT agent, COUNT(*) as calls, SUM(input_tokens) as input_tokens, "
        "SUM(output_tokens) as output_tokens, SUM(cost_usd) as total_cost "
        "FROM llm_calls GROUP BY agent ORDER BY total_cost DESC"
    )
    if not cost_df.empty:
        st.dataframe(cost_df, use_container_width=True)
        st.bar_chart(cost_df.set_index("agent")["total_cost"])

    st.subheader("Cost by Trace")
    trace_cost_df = run_query(
        "SELECT trace_id, company_name, status, total_cost_usd, "
        "ROUND(end_time - start_time, 1) as duration_s "
        "FROM runs ORDER BY start_time DESC"
    )
    if not trace_cost_df.empty:
        st.dataframe(trace_cost_df, use_container_width=True)

    st.subheader("Tool Usage")
    tool_df = run_query(
        "SELECT tool_name, COUNT(*) as count, "
        "ROUND(AVG(duration_ms), 0) as avg_duration_ms "
        "FROM tool_calls GROUP BY tool_name ORDER BY count DESC"
    )
    if not tool_df.empty:
        st.dataframe(tool_df, use_container_width=True)

    st.subheader("Prompt Versions in Use")
    pv_df = run_query(
        "SELECT agent, prompt_version, COUNT(*) as llm_calls, "
        "ROUND(SUM(cost_usd), 4) as total_cost "
        "FROM spans WHERE span_type = 'llm_call' AND prompt_version IS NOT NULL "
        "GROUP BY agent, prompt_version ORDER BY agent"
    )
    if not pv_df.empty:
        st.dataframe(pv_df, use_container_width=True)

# ── Traces ────────────────────────────────────────────────────────

elif page == "Traces":
    st.header("Traces (End-to-End Runs)")

    traces_df = run_query("SELECT * FROM runs ORDER BY start_time DESC")
    if traces_df.empty:
        st.info("No traces found. Run the pipeline first.")
    else:
        st.dataframe(traces_df, use_container_width=True)

        selected_trace = st.selectbox("Inspect trace", traces_df["trace_id"].tolist())
        if selected_trace:
            st.subheader(f"Trace: {selected_trace}")

            # Summary metrics for this trace
            trace_row = traces_df[traces_df["trace_id"] == selected_trace].iloc[0]
            tc1, tc2, tc3 = st.columns(3)
            tc1.metric("Company", trace_row.get("company_name", "N/A"))
            tc2.metric("Status", trace_row.get("status", "N/A"))
            tc3.metric("Cost", f"${trace_row.get('total_cost_usd', 0):.4f}")

            # Spans in this trace
            st.subheader("Spans")
            spans_df = run_query(
                "SELECT span_id, agent, span_type, name, model, prompt_version, "
                "input_tokens, output_tokens, cost_usd, duration_ms, tool_name, error "
                "FROM spans WHERE trace_id = ? ORDER BY start_time",
                (selected_trace,),
            )
            if not spans_df.empty:
                st.caption(f"{len(spans_df)} spans")
                st.dataframe(spans_df, use_container_width=True)

                # Cost breakdown by agent within this trace
                span_cost = spans_df.groupby("agent")["cost_usd"].sum().reset_index()
                if len(span_cost) > 1:
                    st.bar_chart(span_cost.set_index("agent")["cost_usd"])

            # Agent runs linked to this trace
            agent_runs_df = run_query(
                "SELECT agent, status, total_turns, total_tool_calls, "
                "total_input_tokens, total_output_tokens, total_cost_usd, total_duration_ms "
                "FROM agent_runs WHERE trace_id = ? ORDER BY agent",
                (selected_trace,),
            )
            if not agent_runs_df.empty:
                st.subheader("Agent Runs")
                st.dataframe(agent_runs_df, use_container_width=True)

            # LLM calls linked to this trace
            llm_df = run_query(
                "SELECT agent, turn, model, input_tokens, output_tokens, "
                "cost_usd, duration_ms, stop_reason, error "
                "FROM llm_calls WHERE trace_id = ? ORDER BY agent, turn",
                (selected_trace,),
            )
            if not llm_df.empty:
                st.subheader("LLM Calls")
                st.dataframe(llm_df, use_container_width=True)

            # Tool calls linked to this trace
            tool_df = run_query(
                "SELECT agent, turn, tool_name, duration_ms, error "
                "FROM tool_calls WHERE trace_id = ? ORDER BY agent, turn",
                (selected_trace,),
            )
            if not tool_df.empty:
                st.subheader("Tool Calls")
                st.dataframe(tool_df, use_container_width=True)

# ── Agent Runs ───────────────────────────────────────────────────

elif page == "Agent Runs":
    st.header("Agent Runs")

    # Filters
    filter_col1, filter_col2 = st.columns(2)

    with filter_col1:
        agents = run_query("SELECT DISTINCT agent FROM agent_runs ORDER BY agent")
        agent_filter = st.selectbox(
            "Filter by agent", ["All"] + agents["agent"].tolist() if not agents.empty else ["All"]
        )

    with filter_col2:
        trace_ids = run_query("SELECT DISTINCT trace_id FROM agent_runs WHERE trace_id IS NOT NULL ORDER BY trace_id")
        trace_filter = st.selectbox(
            "Filter by trace", ["All"] + trace_ids["trace_id"].tolist() if not trace_ids.empty else ["All"]
        )

    conditions, params = [], []
    if agent_filter != "All":
        conditions.append("agent = ?"); params.append(agent_filter)
    if trace_filter != "All":
        conditions.append("trace_id = ?"); params.append(trace_filter)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    df = run_query(
        f"SELECT trace_id, agent, company_name, status, total_turns, "
        f"total_tool_calls, total_input_tokens, total_output_tokens, "
        f"total_cost_usd, total_duration_ms, error "
        f"FROM agent_runs {where} ORDER BY started_at DESC",
        tuple(params),
    )

    if df.empty:
        st.info("No runs found.")
    else:
        st.dataframe(df, use_container_width=True)

# ── Conversation Replay ──────────────────────────────────────────

elif page == "Conversation Replay":
    st.header("Conversation Replay")

    # Filters
    filter_col1, filter_col2 = st.columns(2)

    with filter_col1:
        agents = run_query("SELECT DISTINCT agent FROM agent_runs ORDER BY agent")
        agent_filter = st.selectbox(
            "Filter by agent", ["All"] + agents["agent"].tolist() if not agents.empty else ["All"]
        )

    with filter_col2:
        trace_ids = run_query("SELECT DISTINCT trace_id FROM agent_runs WHERE trace_id IS NOT NULL ORDER BY trace_id")
        trace_filter = st.selectbox(
            "Filter by trace", ["All"] + trace_ids["trace_id"].tolist() if not trace_ids.empty else ["All"]
        )

    conditions, params = [], []
    if agent_filter != "All":
        conditions.append("agent = ?"); params.append(agent_filter)
    if trace_filter != "All":
        conditions.append("trace_id = ?"); params.append(trace_filter)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    messages = run_query(
        f"SELECT trace_id, agent, sequence_number, role, content, timestamp, tokens "
        f"FROM messages {where} ORDER BY trace_id, agent, sequence_number",
        tuple(params),
    )

    if messages.empty:
        st.info("No messages found. Adjust filters or run the pipeline first.")
    else:
        # Group by (trace_id, agent) for section headers
        grouped = messages.groupby(["trace_id", "agent"])
        for (trace_id, agent), group in grouped:
            st.subheader(f"{agent} — [{trace_id}]")
            st.caption(f"{len(group)} messages")

            for _, msg in group.iterrows():
                role = msg["role"]
                content = msg["content"] or ""
                tokens = int(msg["tokens"]) if msg["tokens"] else 0

                if role == "user":
                    with st.chat_message("user"):
                        st.markdown(content)
                elif role == "assistant":
                    with st.chat_message("assistant"):
                        try:
                            blocks = json.loads(content)
                            for block in blocks:
                                if block.get("type") == "text" and block.get("text"):
                                    st.markdown(block["text"][:2000])
                                elif block.get("type") == "tool_use":
                                    st.code(
                                        f"Tool: {block.get('name')}\n"
                                        f"Input: {json.dumps(block.get('input', {}), indent=2)[:500]}",
                                        language="json",
                                    )
                        except (json.JSONDecodeError, TypeError):
                            st.markdown(content[:2000])
                        if tokens:
                            st.caption(f"{tokens} tokens")
                elif role == "tool_result":
                    with st.chat_message("assistant", avatar="🔧"):
                        st.code(content[:2000], language="text")

# ── LLM Calls ───────────────────────────────────────────────────

elif page == "LLM Calls":
    st.header("LLM Calls")

    # Filters
    filter_col1, filter_col2 = st.columns(2)

    with filter_col1:
        agents = run_query("SELECT DISTINCT agent FROM llm_calls ORDER BY agent")
        agent_filter = st.selectbox(
            "Filter by agent", ["All"] + agents["agent"].tolist() if not agents.empty else ["All"]
        )

    with filter_col2:
        trace_ids = run_query("SELECT DISTINCT trace_id FROM llm_calls WHERE trace_id IS NOT NULL ORDER BY trace_id")
        trace_filter = st.selectbox(
            "Filter by trace", ["All"] + trace_ids["trace_id"].tolist() if not trace_ids.empty else ["All"]
        )

    conditions, params = [], []
    if agent_filter != "All":
        conditions.append("agent = ?"); params.append(agent_filter)
    if trace_filter != "All":
        conditions.append("trace_id = ?"); params.append(trace_filter)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    df = run_query(
        f"SELECT trace_id, agent, turn, model, input_tokens, output_tokens, "
        f"cost_usd, duration_ms, stop_reason, error "
        f"FROM llm_calls {where} ORDER BY timestamp",
        tuple(params),
    )

    if df.empty:
        st.info("No LLM calls found.")
    else:
        st.dataframe(df, use_container_width=True)

        # Token distribution chart
        if len(df) > 1:
            st.subheader("Tokens per Turn")
            chart_df = df[["turn", "input_tokens", "output_tokens"]].copy()
            chart_df = chart_df.set_index("turn")
            st.bar_chart(chart_df)

        # Detail view
        call_keys = [(r["trace_id"], r["agent"], r["turn"]) for _, r in df.iterrows()]
        call_labels = [f"{t} / {a} / turn {tn}" for t, a, tn in call_keys]
        selected_label = st.selectbox("Inspect LLM call", call_labels)
        if selected_label:
            idx = call_labels.index(selected_label)
            sel_trace, sel_agent, sel_turn = call_keys[idx]

            full = run_query(
                "SELECT * FROM llm_calls WHERE trace_id = ? AND agent = ? AND turn = ?",
                (sel_trace, sel_agent, sel_turn),
            )
            row = full.iloc[0]

            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("Input Tokens", row["input_tokens"])
                st.metric("Cost", f"${row['cost_usd']:.4f}")
            with col2:
                st.metric("Output Tokens", row["output_tokens"])
                st.metric("Duration", f"{row['duration_ms']:.0f}ms")
            with col3:
                st.metric("Trace ID", row["trace_id"] or "N/A")
                st.metric("Model", row["model"])

            with st.expander("System Prompt"):
                st.code(row["system_prompt"] or "N/A", language="text")

            with st.expander("Request Messages"):
                try:
                    st.json(json.loads(row["request_messages"]))
                except (json.JSONDecodeError, TypeError):
                    st.code(str(row["request_messages"]))

            with st.expander("Response Content"):
                try:
                    st.json(json.loads(row["response_content"]))
                except (json.JSONDecodeError, TypeError):
                    st.code(str(row["response_content"]))

            if row["error"]:
                st.error(f"Error: {row['error']}")

# ── Tool Calls ───────────────────────────────────────────────────

elif page == "Tool Calls":
    st.header("Tool Calls")

    # Filters
    filter_col1, filter_col2 = st.columns(2)

    with filter_col1:
        agents = run_query("SELECT DISTINCT agent FROM tool_calls ORDER BY agent")
        agent_filter = st.selectbox(
            "Filter by agent", ["All"] + agents["agent"].tolist() if not agents.empty else ["All"]
        )

    with filter_col2:
        trace_ids = run_query("SELECT DISTINCT trace_id FROM tool_calls WHERE trace_id IS NOT NULL ORDER BY trace_id")
        trace_filter = st.selectbox(
            "Filter by trace", ["All"] + trace_ids["trace_id"].tolist() if not trace_ids.empty else ["All"]
        )

    conditions, params = [], []
    if agent_filter != "All":
        conditions.append("agent = ?"); params.append(agent_filter)
    if trace_filter != "All":
        conditions.append("trace_id = ?"); params.append(trace_filter)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    df = run_query(
        f"SELECT trace_id, agent, turn, tool_name, tool_input, duration_ms, error "
        f"FROM tool_calls {where} ORDER BY timestamp",
        tuple(params),
    )

    if df.empty:
        st.info("No tool calls found.")
    else:
        st.dataframe(df, use_container_width=True)

        # Detail view
        selected_idx = st.selectbox("Inspect tool call (row index)", range(len(df)))
        if selected_idx is not None:
            row = df.iloc[selected_idx]

            col1, col2 = st.columns(2)
            col1.metric("Duration", f"{row['duration_ms']:.0f}ms")
            col2.metric("Trace ID", row["trace_id"] or "N/A")

            full = run_query(
                "SELECT * FROM tool_calls WHERE trace_id = ? AND agent = ? "
                "AND turn = ? AND tool_name = ? LIMIT 1",
                (row["trace_id"], row["agent"], row["turn"], row["tool_name"]),
            )
            if not full.empty:
                full_row = full.iloc[0]

                with st.expander("Tool Input"):
                    try:
                        st.json(json.loads(full_row["tool_input"]))
                    except (json.JSONDecodeError, TypeError):
                        st.code(str(full_row["tool_input"]))

                with st.expander("Tool Result"):
                    try:
                        st.json(json.loads(full_row["tool_result"]))
                    except (json.JSONDecodeError, TypeError):
                        st.code(str(full_row["tool_result"]))

                if full_row["error"]:
                    st.error(f"Error: {full_row['error']}")

# ── Evals ────────────────────────────────────────────────────────

elif page == "Evals":
    st.header("Eval Results")

    eval_df = run_query(
        "SELECT * FROM eval_results ORDER BY trace_id, agent, field_name"
    )
    if eval_df.empty:
        st.info("No eval results found. Run an evaluation first.")
    else:
        # Filters
        filter_col1, filter_col2 = st.columns(2)

        with filter_col1:
            agents = eval_df["agent"].unique().tolist()
            agent_filter = st.selectbox("Filter by agent", ["All"] + sorted(agents))

        with filter_col2:
            traces = eval_df["trace_id"].unique().tolist()
            trace_filter = st.selectbox("Filter by trace", ["All"] + sorted(traces))

        filtered = eval_df.copy()
        if agent_filter != "All":
            filtered = filtered[filtered["agent"] == agent_filter]
        if trace_filter != "All":
            filtered = filtered[filtered["trace_id"] == trace_filter]

        # Summary metrics
        total = len(filtered)
        matched = int(filtered["match"].sum())
        missing = len(filtered[filtered["match_type"] == "missing"])
        accuracy = matched / total if total else 0

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total Fields", total)
        col2.metric("Matched", matched)
        col3.metric("Missing", missing)
        col4.metric("Accuracy", f"{accuracy:.0%}")

        # Field results table
        st.dataframe(filtered, use_container_width=True)

        # Confidence calibration
        if total > 0:
            st.subheader("Confidence Calibration")
            cal_df = run_query(
                "SELECT confidence, COUNT(*) as total, SUM(match) as correct, "
                "ROUND(CAST(SUM(match) AS FLOAT) / COUNT(*) * 100, 1) as accuracy_pct "
                "FROM eval_results GROUP BY confidence ORDER BY "
                "CASE confidence WHEN 'high' THEN 1 WHEN 'medium' THEN 2 "
                "WHEN 'low' THEN 3 ELSE 4 END"
            )
            if not cal_df.empty:
                st.dataframe(cal_df, use_container_width=True)

        # Per-agent breakdown
        if len(filtered["agent"].unique()) > 1:
            st.subheader("Accuracy by Agent")
            agent_acc = run_query(
                "SELECT agent, COUNT(*) as total, SUM(match) as correct, "
                "ROUND(CAST(SUM(match) AS FLOAT) / COUNT(*) * 100, 1) as accuracy_pct "
                "FROM eval_results GROUP BY agent"
            )
            if not agent_acc.empty:
                st.dataframe(agent_acc, use_container_width=True)

        # Prompt version → accuracy correlation
        st.subheader("Prompt Version → Accuracy")
        st.caption(
            "Joins spans (prompt_version per trace/agent) with eval_results. "
            "Aggregates each side to (trace_id, agent) before joining to avoid fan-out."
        )
        pv_acc_df = run_query(
            "WITH agent_pv AS ("
            "  SELECT trace_id, agent, prompt_version"
            "  FROM spans"
            "  WHERE span_type = 'llm_call' AND prompt_version IS NOT NULL"
            "  GROUP BY trace_id, agent, prompt_version"
            "), agent_acc AS ("
            "  SELECT trace_id, agent, COUNT(*) AS fields, SUM(match) AS correct"
            "  FROM eval_results"
            "  GROUP BY trace_id, agent"
            ")"
            "SELECT a.agent, a.prompt_version, COUNT(*) AS eval_runs,"
            "  SUM(b.fields) AS total_fields, SUM(b.correct) AS total_correct,"
            "  ROUND(CAST(SUM(b.correct) AS FLOAT) / NULLIF(SUM(b.fields), 0) * 100, 1) AS accuracy_pct "
            "FROM agent_pv a "
            "JOIN agent_acc b ON b.trace_id = a.trace_id AND b.agent = a.agent "
            "GROUP BY a.agent, a.prompt_version "
            "ORDER BY a.agent, a.prompt_version"
        )
        if pv_acc_df.empty:
            st.info("No data yet — need eval runs across multiple prompt versions.")
        else:
            st.dataframe(pv_acc_df, use_container_width=True)

# ── Raw SQL ──────────────────────────────────────────────────────

elif page == "Raw SQL":
    st.header("Raw SQL Query")

    st.markdown("**Tables:** `runs`, `spans`, `agent_runs`, `llm_calls`, `tool_calls`, `messages`, `eval_results` — join on `trace_id` / `agent`")

    default_query = "SELECT agent, COUNT(*) as calls, SUM(cost_usd) as total_cost FROM llm_calls GROUP BY agent"
    query = st.text_area("SQL Query", value=default_query, height=100)

    if st.button("Run Query"):
        df = run_query(query)
        if not df.empty:
            st.dataframe(df, use_container_width=True)
            st.caption(f"{len(df)} rows returned")
        else:
            st.info("No results.")

    st.subheader("Quick Queries")
    quick_queries = {
        "All traces": "SELECT * FROM runs ORDER BY start_time DESC",
        "All agent runs": "SELECT trace_id, agent, company_name, status, total_turns, total_tool_calls, total_cost_usd, total_duration_ms FROM agent_runs ORDER BY started_at DESC",
        "LLM calls with errors": "SELECT trace_id, agent, turn, model, error FROM llm_calls WHERE error IS NOT NULL",
        "Most expensive calls": "SELECT trace_id, agent, turn, input_tokens, output_tokens, cost_usd FROM llm_calls ORDER BY cost_usd DESC LIMIT 10",
        "Tool call breakdown": "SELECT tool_name, COUNT(*) as count, ROUND(AVG(duration_ms)) as avg_ms, ROUND(SUM(duration_ms)) as total_ms FROM tool_calls GROUP BY tool_name",
        "Tokens per agent": "SELECT agent, SUM(input_tokens) as total_in, SUM(output_tokens) as total_out, SUM(input_tokens + output_tokens) as total FROM llm_calls GROUP BY agent",
        "Trace → Agent Runs": "SELECT r.trace_id, r.company_name, r.status as trace_status, ar.agent, ar.status as agent_status, ar.total_cost_usd FROM runs r LEFT JOIN agent_runs ar ON ar.trace_id = r.trace_id ORDER BY r.start_time DESC",
        "Prompt versions": "SELECT agent, prompt_version, COUNT(*) as calls, ROUND(SUM(cost_usd), 4) as cost FROM spans WHERE prompt_version IS NOT NULL GROUP BY agent, prompt_version",
        "Eval accuracy by agent": "SELECT agent, COUNT(*) as fields, SUM(match) as correct, ROUND(CAST(SUM(match) AS FLOAT) / COUNT(*) * 100, 1) as accuracy_pct FROM eval_results GROUP BY agent",
        "Prompt version → accuracy": "WITH pv AS (SELECT trace_id, agent, prompt_version FROM spans WHERE span_type='llm_call' AND prompt_version IS NOT NULL GROUP BY trace_id, agent, prompt_version), acc AS (SELECT trace_id, agent, COUNT(*) as fields, SUM(match) as correct FROM eval_results GROUP BY trace_id, agent) SELECT pv.agent, pv.prompt_version, COUNT(*) as eval_runs, SUM(acc.fields) as total_fields, SUM(acc.correct) as total_correct, ROUND(CAST(SUM(acc.correct) AS FLOAT)/NULLIF(SUM(acc.fields),0)*100,1) as accuracy_pct FROM pv JOIN acc ON acc.trace_id=pv.trace_id AND acc.agent=pv.agent GROUP BY pv.agent, pv.prompt_version ORDER BY pv.agent, pv.prompt_version",
        "Eval + cost (trace)": "SELECT e.trace_id, e.company_name, COUNT(*) as fields, SUM(e.match) as correct, ROUND(CAST(SUM(e.match) AS FLOAT) / COUNT(*) * 100, 1) as accuracy_pct, ROUND(r.total_cost_usd, 4) as cost FROM eval_results e LEFT JOIN runs r ON e.trace_id = r.trace_id GROUP BY e.trace_id",
    }

    for label, sql in quick_queries.items():
        if st.button(label):
            df = run_query(sql)
            if not df.empty:
                st.dataframe(df, use_container_width=True)
