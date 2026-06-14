# Architecture Backlog

Items identified during the 2026-05-17 architecture review. None of these are implemented — each requires explicit approval before work begins.

---

## 1. MODEL_COSTS silently returns $0.00 for unknown models

**File:** `src/observability/tracer.py` — `MODEL_COSTS` dict and `TraceSpan.cost_usd` property

**Problem:** When a model ID is not in `MODEL_COSTS`, `cost_usd` returns `0.0` without any warning. If a new Claude model is used before pricing is updated, every run will show $0.00 cost — silently wrong rather than visibly broken.

**Proposed fix:** Log a warning (or raise at startup) when `TraceSpan.cost_usd` encounters an unknown model ID. Separately, add a last-checked date comment to `MODEL_COSTS` so it's clear when the rates were last verified.

**Effort:** Small (< 1 hour)

---

## 2. DB path is hardcoded

**File:** `src/main.py` — `AgentDB(db_path="outputs/agent_log.db")`

**Problem:** The SQLite DB path is hardcoded. Running two instances simultaneously, using a test DB, or changing the output location all require code edits.

**Proposed fix:** Accept `--db-path` as a CLI flag in `main()`, defaulting to `"outputs/agent_log.db"`. Pass it through to `AgentDB` and `tracer.persist()`.

**Effort:** Small (< 1 hour)

---

## 3. AgentDB connection is never explicitly closed

**File:** `src/observability/agent_db.py` — `AgentDB.__init__` opens `self.conn` and never closes it

**Problem:** The connection is left open and relies on garbage collection to release the file handle. In long-lived processes or tests that create many `AgentDB` instances, this leaks file descriptors. `AgentTracer.persist()` opens and closes its own connection correctly — `AgentDB` is inconsistent.

**Proposed fix:** Implement `__enter__`/`__exit__` (context manager) and an explicit `close()` method. Update `main.py` to use `with AgentDB(...) as agent_db:`.

**Effort:** Small (< 1 hour)

---

## 4. OUTPUT_SCHEMA strings can drift from Pydantic models

**Files:** `src/agents/research.py`, `financial.py`, `risk.py`, `social_media.py`, `synthesis.py` — each has a hardcoded `OUTPUT_SCHEMA` string that mirrors a Pydantic model

**Problem:** The schema string is what the LLM is instructed to produce; the Pydantic model is what validates the output. They are defined separately and can drift. Adding a field to `CompanyResearch` without updating the `OUTPUT_SCHEMA` string in `research.py` means the LLM will never populate that field, but no test will catch it.

**Proposed fix:** Generate the schema string from the Pydantic model using `model.model_json_schema()` and a lightweight renderer, or add a test that asserts all Pydantic model field names appear in their corresponding `OUTPUT_SCHEMA` string.

**Effort:** Medium (2–4 hours for the generative approach; < 1 hour for the test-only approach)
