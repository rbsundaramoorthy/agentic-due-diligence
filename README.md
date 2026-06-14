# Multi-Agent Due Diligence Analyst

A structured, auditable multi-agent system for automated company due diligence with per-claim confidence, source provenance, and full observability. Five specialist agents research a company in parallel using LLM agents with tool calling and structured outputs — including an EDGAR agent that retrieves audited financials and risk factors directly from SEC 10-K filings for US public companies. Findings are assembled into a canonical `ReportDocument` (JSON) with tier-annotated sources and gap records; markdown, PDF, and HTML reports are rendered from that single source of truth. Every LLM call, tool invocation, and token cost is logged to a SQLite database for inspection and replay.

## Quick Start

```bash
# 1. Clone and install
git clone https://github.com/sunlightware/agentic-due-diligence.git
cd agentic-due-diligence
pip install -e .

# 2. Set your API keys
export ANTHROPIC_API_KEY=your-key-here
export BRAVE_API_KEY=your-key-here  # Free: https://brave.com/search/api/

# 3. Run it
python -m src.main "Stripe"
# Writes: outputs/report_stripe.json (canonical), .md, .pdf, .html
```

### Test without a search API key

```bash
export ANTHROPIC_API_KEY=your-key-here
export USE_MOCK_SEARCH=true
python -m src.main "Stripe"

# JSON output only (no PDF/HTML render)
python -m src.main "Stripe" --json-only
```

## Architecture

```
                          ┌──────────────────┐
                          │   Orchestrator    │
                          │   (main.py)       │
                          └────────┬─────────┘
                                   │ asyncio.gather (parallel)
       ┌───────────┬───────────────┼───────────────┬───────────────┐
       │           │               │               │               │
  ┌────▼────┐ ┌────▼────┐  ┌──────▼──────┐ ┌─────▼─────┐ ┌──────▼──────┐
  │Research │ │Financial│  │    Risk     │ │  Social   │ │   EDGAR     │
  │ Agent   │ │ Agent   │  │   Agent     │ │   Media   │ │   Agent     │
  └────┬────┘ └────┬────┘  └──────┬──────┘ └─────┬─────┘ └──────┬──────┘
       │           │               │               │               │
       └───────────┴───────────────┴───────────────┴───────────────┘
                                   │ assemble_report() — EDGAR merge + tier coverage
                    ┌──────────────┼───────────────┐
                    │              │               │
           ┌────────▼──────┐ ┌────▼──────┐ ┌─────▼──────────┐
           │  Report       │ │  Tracer   │ │  Ground Truth  │
           │  Generator    │ │ + AgentDB │ │  Evaluator     │
           └───────────────┘ └───────────┘ └────────────────┘
```

### Agent Loop (Base Agent State Machine)

Every agent follows the same state machine implemented in `BaseAgent`:

```
PLANNING → EXECUTING → REFLECTING → COMPLETE
              ↑            │
              └── RETRY ◄──┘ (parse failure, up to 3 retries)
                             → FAILED (retries exhausted or MAX_TURNS hit)
```

1. **PLANNING** — The agent receives a task and sends it to the LLM with tools and a system prompt
2. **EXECUTING** — The LLM returns `tool_use` blocks; the agent executes each tool and feeds results back
3. **REFLECTING** — The LLM returns text (no more tool calls); the agent parses structured JSON output via Pydantic
4. **COMPLETE** — Parsing succeeds; structured data is returned
5. **RETRY** — Parsing fails; a correction prompt is appended and the loop continues
6. **FAILED** — Retries exhausted or MAX_TURNS (15) reached; partial results returned

### Agents

| Agent | Responsibility | Schema |
|-------|---------------|--------|
| **Research** | Company overview, products, leadership, technology, recent news | `CompanyResearch` |
| **Financial** | Revenue, funding, valuation, investors, revenue model | `CompanyFinancials` |
| **Risk** | Regulatory, legal, cyber, operational, reputational, ESG risks | `CompanyRisks` |
| **Social Media** | Twitter/X, LinkedIn, Reddit, Glassdoor, sentiment analysis | `CompanySocialMedia` |
| **EDGAR** | SEC EDGAR: audited 10-K financials + risk factor disclosures for US public companies | `CompanyEdgarFinancials` |

The four web-search agents use `claude-sonnet-4-20250514` with shared `web_search` and `web_fetch` tools. The EDGAR agent uses three EDGAR-specific tools (no web search) with MAX_TURNS=5. All agents produce structured JSON parsed into Pydantic models. Each data point carries a confidence level (HIGH/MEDIUM/LOW) and source URLs.

The Financial agent defers revenue/profitability to the EDGAR agent for US public companies (`is_likely_public=True`), returning those fields as "unknown". The assembler merges the EDGAR values at assembly time, keeping agent prompts stable.

### Tools

| Tool | Agent | Description |
|------|-------|-------------|
| `web_search` | All web agents | Brave Search API (or mock via `USE_MOCK_SEARCH=true`) — returns `{title, url, snippet}` results |
| `web_fetch` | All web agents | HTTP fetch with HTML stripping, truncated to 8K chars for context budget |
| `edgar_find_company` | EDGAR | EFTS search for CIK; disambiguation by most-recent filing date + exact name match |
| `edgar_get_financials` | EDGAR | Companyfacts API: revenue (with bank fallback chain) + net income from XBRL facts |
| `edgar_get_filing_text` | EDGAR | 10-K filing text for risk_factors / business / mda sections |

### Structured Output (Two-Layer Schema)

**Agent output layer** — what agents produce (sources are bare URL strings):

```python
class DataPoint(BaseModel):
    value: str
    confidence: ConfidenceLevel    # HIGH, MEDIUM, LOW, UNKNOWN
    sources: List[str]             # URLs (bare strings at agent layer)
    reasoning: Optional[str]
    severity: Optional[SeverityLevel]  # CRITICAL, HIGH, MEDIUM, LOW (risk items only)
```

When `assemble_report()` builds the canonical report, each bare URL in `sources` is wrapped into a `SourceRef` with an inferred `tier` — no agent prompt changes needed.

**Canonical report layer** — what renderers and downstream systems consume:

```python
class Claim(BaseModel):
    claim_id: str                  # 12-char hex UUID, stable per claim
    field_name: str                # e.g. "description", "key_products[0]"
    value: str
    confidence: ConfidenceLevel
    severity: Optional[SeverityLevel]
    sources: List[SourceRef]       # tier-annotated, not bare strings
    agent: str
    reasoning: Optional[str]

class SourceRef(BaseModel):
    url: str
    tier: SourceTier               # primary_document | reputable_secondary | aggregator | community | unknown
    retrieved_at: Optional[datetime]  # always null in P1; reserved for P2 passage-level evidence
```

Severity and confidence are orthogonal: a risk can be HIGH severity with LOW confidence (unconfirmed but potentially catastrophic).

### Canonical JSON Report (Primary Output)

Every run produces a `ReportDocument` written to `outputs/report_{slug}.json`. This is the single source of truth — markdown, PDF, and HTML are renders of it, not edited independently. The schema is versioned (`SCHEMA_VERSION = "1.0.1"`) and published as `schema/report.schema.json`.

Key fields in `ReportDocument`:
- **`Claim`** objects with `claim_id`, `field_name`, `value`, `confidence`, `severity`, `agent`, and `sources: List[SourceRef]`
- **`SourceRef`** carries `url`, `tier` (one of `primary_document | reputable_secondary | aggregator | community | unknown`), and `retrieved_at` (always null in P1; reserved for P2 passage-level evidence)
- **`GapRecord`** entries for fields with no reliable data
- **`RunMetadata`** with per-agent cost/token/timing breakdown, plus `tier_coverage`, `tier_attempts`, `edgar_lookup_status`, and `edgar_cik`

Source tiers are inferred at assembly time via **registry-first lookup**: `_infer_tier()` checks the source registry (`src/sources/registry.py`) before falling back to hardcoded domain sets for general web search results. Known Tier 0/1 sources (SEC EDGAR, USPTO, SAM.gov, OpenCorporates, CourtListener) are always correctly classified regardless of subdomain variation. Adding a new authoritative source requires only a registry entry — no assembler changes.

### Report Rendering

All renders are produced from the canonical `ReportDocument`:
- **Markdown** (`report_{slug}.md`) — tables for all sections, confidence badges, severity sorting (CRITICAL first), weighted overall confidence (Financial 40% + Risk 40% + Social Media 20%), information gaps, per-agent methodology footer
- **PDF** (`report_{slug}.pdf`) — styled ReportLab PDF with color-coded confidence/severity badges
- **HTML** (`report_{slug}.html`) — standalone browser-viewable companion to the PDF

### Observability — Two-Layer Architecture

All tables use `trace_id` as the universal correlation key.

**Layer 1: Lightweight Metrics** (`src/observability/tracer.py`)

Per-pipeline-run tracing with cost tracking:

| Table | Primary Key | Contents |
|-------|-------------|----------|
| `runs` | `trace_id` | Company, timestamps, total cost, status |
| `spans` | `span_id` (FK→runs) | Per-LLM-call and per-tool-call metrics: tokens, cost, duration, model, prompt version |

**Layer 2: Full Payload Debug** (`src/observability/agent_db.py`)

Every request/response stored for replay and debugging:

| Table | Primary Key | Contents |
|-------|-------------|----------|
| `agent_runs` | `(trace_id, agent)` | Task, status, total turns/tokens/cost, result JSON |
| `llm_calls` | `(trace_id, agent, turn)` | Full system prompt, request messages, response content, stop reason |
| `tool_calls` | `(trace_id, agent, turn)` | Tool name, input JSON, result (up to 10K chars) |
| `messages` | `(trace_id, agent, sequence_number)` | Conversation replay: user/assistant/tool_result messages (truncated to 2K chars) |

**Layer 3: Evaluation Results** (`evals/eval_runner.py`)

| Table | Primary Key | Contents |
|-------|-------------|----------|
| `eval_results` | `(trace_id, agent, field_name)` | Expected vs actual, confidence, match type |

### Evaluation System

Ground truth fixtures (`evals/ground_truth/{stripe,anthropic,shopify}.json`) contain stable facts (founded year, headquarters, leadership, products, investors). The evaluator compares agent outputs using:

- **Exact match** — normalized string equality
- **Fuzzy match** — substring containment or word overlap
- **List overlap** — how many expected items appear in the combined output

Results include accuracy, confidence calibration (are HIGH-confidence fields actually correct more often?), and per-agent breakdown. Output via Rich terminal scorecard and persisted to `eval_results` table.

## Project Structure

```
src/
├── main.py                     # Entry point; orchestrates agents, assembles ReportDocument, writes outputs
├── agents/
│   ├── base.py                 # BaseAgent (state machine), WebSearchMixin, strip_json
│   ├── classifier.py           # Lightweight pre-classification (no tools); sets is_likely_public
│   ├── research.py             # Research Agent
│   ├── financial.py            # Financial Agent (defers revenue/profitability to EDGAR for US public cos)
│   ├── risk.py                 # Risk Agent
│   ├── social_media.py         # Social Media Agent
│   ├── synthesis.py            # Synthesis Agent (cross-agent aggregation)
│   └── edgar.py                # EDGAR Agent — SEC 10-K financials + risk factors (US public companies)
├── tools/
│   └── web_search.py           # web_search (Brave API / mock) + web_fetch
├── sources/
│   ├── registry.py             # Authoritative source registry (tier, rate_limit, API key metadata)
│   ├── cache.py                # SourceCache — SQLite TTL cache for Tier 0 API responses
│   └── edgar.py                # EDGAR tool functions: find_company, get_financials, get_filing_text
├── observability/
│   ├── tracer.py               # AgentTracer — run/span metrics + report_json_path
│   └── agent_db.py             # AgentDB — full payload logging to SQLite
├── schemas/
│   └── models.py               # Two-layer schema: DataPoint (agent) + Claim/ReportDocument (canonical)
└── synthesis/
    ├── assembler.py            # assemble_report() — agent dicts → ReportDocument (registry-first tier, EDGAR merge, tier coverage)
    ├── report_generator.py     # render_report_from_doc() — markdown; render_report() is a deprecated shim
    └── pdf_report.py           # render_pdf_report_from_doc() — PDF + HTML; render_pdf_report() is a shim

schema/
└── report.schema.json          # JSON Schema generated from ReportDocument.model_json_schema()

evals/
├── eval_runner.py              # GroundTruthEvaluator, persistence, scorecard
└── ground_truth/               # Stable fact fixtures: stripe, anthropic, shopify, nvda, googl, ...

scripts/
├── dashboard.py                # Streamlit dashboard (8 pages)
└── query_db.py                 # CLI script to run pipeline + print DB contents

tests/
├── fixtures/
│   ├── sample_report.json      # Checked-in Stripe ReportDocument fixture for contract tests
│   └── edgar/                  # EDGAR fixture JSON: search_aapl, search_apple_disambiguation, companyfacts_aapl, companyfacts_jpm
├── test_agents.py              # Agent state machine, schemas, severity sorting, AgentDB
├── test_schemas_and_tracer.py  # Schemas + AgentTracer
├── test_evals.py               # Eval system — matching, EvalResults, persistence
├── test_synthesis.py           # Synthesis agent
├── test_assembler.py           # assemble_report, _infer_tier (registry-first), _merge_edgar, tier_coverage, build_render_dicts, perf
├── test_report_schema.py       # Schema drift test + ReportDocument round-trip
├── test_canonical_json_contract.py  # Contract tests against sample_report.json
├── test_source_cache.py        # SourceCache: miss, put/get, TTL, hit_count, key stability, clear
└── test_edgar_tools.py         # EDGAR tools: find_company disambiguation, get_financials fallback chain (AAPL + JPM)
```

## Streamlit Dashboard

Launch with `streamlit run scripts/dashboard.py`. Pages:

| Page | Description |
|------|-------------|
| Overview | High-level run summary |
| Traces | Drill into spans, agent runs, LLM calls, tool calls per trace |
| Agent Runs | Filter by agent and trace |
| Conversation Replay | Chat-style UI showing user/assistant/tool_result messages |
| LLM Calls | Full request/response payloads per turn |
| Tool Calls | Tool inputs and results |
| Evals | Accuracy metrics, field results, confidence calibration |
| Raw SQL | Ad-hoc queries against the observability DB |

## CLI Usage

```bash
# Run due diligence on a company (writes JSON + markdown + PDF + HTML)
python -m src.main "Stripe"

# JSON output only — skips PDF/HTML render
python -m src.main "Stripe" --json-only

# Run with full DB dump printed to terminal
python -m src.main "Stripe" --dump-db

# Query DB after a run
python scripts/query_db.py "Stripe"

# Launch the Streamlit dashboard
streamlit run scripts/dashboard.py

# Run tests
./venv/bin/python -m pytest tests/

# Regenerate JSON schema after model changes (then re-run tests to verify drift test passes)
python -c "import json; from src.schemas.models import ReportDocument; \
           print(json.dumps(ReportDocument.model_json_schema(), indent=2))" \
> schema/report.schema.json
```

## Environment

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes | Claude API key |
| `BRAVE_API_KEY` | For real search | Brave Search API key (free tier: 2,000 queries/month) |
| `USE_MOCK_SEARCH` | No | Set to `true` to use mock search results |
| `EDGAR_USER_AGENT` | Recommended | Identifies your app to SEC per their policy (e.g. `MyApp/1.0 you@example.com`) |
| `SAM_GOV_API_KEY` | No (P3b) | Federal contract award data via SAM.gov; degrades gracefully if absent |
| `OPENCORPORATES_API_KEY` | No (P3b) | Higher rate limits on OpenCorporates; required once P3b tools are live |
| `COURTLISTENER_API_KEY` | No | Higher rate limits on CourtListener; unauthenticated mode works |

- Python 3.13, virtualenv in `./venv`
- SQLite DB: `outputs/agent_log.db` (auto-created on first run)
- Reports written to `outputs/report_{company}.md`

## Key Design Decisions

- **JSON as source of truth** — Every run produces a canonical `ReportDocument` written to `outputs/report_{slug}.json`; markdown, PDF, and HTML are renders, not independent documents
- **Two-layer schema** — Agent output layer (`DataPoint`, `CompanyResearch`, etc.) is stable; canonical layer (`Claim`, `ReportDocument`) is what renderers and downstream systems consume
- **Registry-first tier inference** — `_infer_tier()` checks the source registry (`src/sources/registry.py`) before falling back to hardcoded domain sets; adding a new authoritative source requires only a registry entry
- **EDGAR deferral** — For US public companies, the Financial agent skips revenue/profitability (returns "unknown") and the EDGAR agent provides audited 10-K values; the assembler merges them — agents stay simple, prompts stay stable
- **Assembler as merge point** — All source-of-truth overlays (EDGAR → financial, SEC risk factors → regulatory_risks) happen at assembly time; no agent needs to know about other agents
- **SourceCache** — SQLite-backed TTL cache for Tier 0 API calls; immutable filings cached forever, companyfacts cached 24h; reduces EDGAR latency on repeated runs and respects rate limits
- **Shim pattern for renderers** — Old dict-based render functions delegate to new doc-based functions; marked `TODO(P2)` for eventual removal
- **Parallel execution** — All 5 specialist agents (including EDGAR) run concurrently via `asyncio.gather`, sharing one `AgentTracer`, `AgentDB`, and `SourceCache`
- **Confidence-scored data** — Every fact is a `DataPoint` (agent layer) or `Claim` (canonical layer) with confidence + sources, not raw strings
- **Opt-in observability** — Agents work without a DB (`db=None` default); full logging is additive
- **Tool call budget** — MAX_TURNS=15 with prompt-based budget (4–5 calls) for web agents; EDGAR uses MAX_TURNS=5 (deterministic 3-tool workflow)
- **Structured output with retries** — Agents produce JSON parsed by Pydantic; up to 3 retries on parse failure
- **Natural composite keys** — `(trace_id, agent)` for runs, `(trace_id, agent, turn)` for LLM calls — no surrogate IDs
- **Severity vs confidence** — Orthogonal dimensions: severity = risk impact, confidence = data reliability
- **Schema versioning** — `SCHEMA_VERSION = "1.0.1"` in models.py; MAJOR for breaking changes, MINOR for additive fields without defaults, PATCH for additive fields with defaults; drift test in `tests/test_report_schema.py`

## Direction

This project is positioned as a structured, auditable, programmable due-diligence pipeline — not a competitor to polished-memo products like Perplexity Comet or Deep Research. The strategic bet is that provenance, calibration, and customization matter more to compliance, engineering, and downstream-pipeline users than narrative polish.

Active direction (see [`docs/STRATEGY.md`](docs/STRATEGY.md) for the full brief, priorities, success metrics, and non-goals):

- **Per-claim evidence with passage-level provenance**, not just source URL lists.
- **Tiered source registry.** Primary documents (SEC EDGAR, OpenCorporates, USPTO, SAM.gov, court records) → agent-grade search (Brave, Tavily) → specialized commercial APIs. Premium licensed feeds (Bloomberg, Factiva, Refinitiv) are explicitly out of scope.
- **Tier-weighted, empirically calibrated confidence** with source diversity as an input.
- **Domain profiles** (`vendor_security`, `ma_target`, etc.) as configurations of the same pipeline, not forks.
- **Conflict resolution as a pipeline stage**, not just a flag in the report.
- **Reproducibility** from versioned prompts, hashed source snapshots, and a replayable run manifest.
- **Plugin architecture** for agents and tools, so new sources and domain logic drop in without core changes.

**Out of scope:** narrative memo synthesis, polished-deck output, premium licensed feed integration, scenario analysis (bull/base/bear).

## Sample Output

See `outputs/` for generated reports after running the tool.
