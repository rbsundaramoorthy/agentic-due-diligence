# Multi-Agent Due Diligence Analyst

A multi-agent system that researches a company and produces a structured, traceable
due-diligence report. Five specialist agents run in parallel, each using LLM tool
calling to gather evidence from public sources. Their outputs are assembled into a
canonical JSON document with per-claim confidence levels, source tier annotations,
and explicit gap records. Markdown, PDF, and HTML reports are rendered from that
document. Every LLM call, tool invocation, and cost is logged to SQLite.

**Positioning:** This is a provenance-first research tool, not a polished-memo
generator. It is intended for compliance, audit, and engineering teams who need
traceable outputs and structured data — not narrative polish.

---

## Quick Start

```bash
# 1. Clone and install
git clone https://github.com/rbsundaramoorthy/agentic-due-diligence.git
cd agentic-due-diligence
pip install -e .

# 2. Set required keys
export ANTHROPIC_API_KEY=your-key
export BRAVE_API_KEY=your-key        # Brave Search API — free tier: 2,000 queries/month

# 3. Run
python -m src.main "Acme Corp"
# Writes: outputs/report_acme_corp.json  (canonical)
#         outputs/report_acme_corp.md
#         outputs/report_acme_corp.pdf
#         outputs/report_acme_corp.html
```

**Test without a search API key:**

```bash
export ANTHROPIC_API_KEY=your-key
export USE_MOCK_SEARCH=true
python -m src.main "Acme Corp"

# JSON output only (skip PDF/HTML):
python -m src.main "Acme Corp" --json-only
```

---

## How It Works

```
                      ┌──────────────────────┐
                      │  Classifier (Haiku)   │
                      │  sector, public?,     │
                      │  government vendor?   │
                      └──────────┬───────────┘
                                 │ company_context → all agents
                        asyncio.gather (parallel)
   ┌──────────┬──────────────────┼──────────────┬────────────┐
   │          │                  │              │            │
Research  Financial            Risk          Social       EDGAR
 Agent     Agent              Agent          Media        Agent
(Sonnet)  (Sonnet)           (Sonnet)       Agent       (Sonnet)
                                            (Sonnet)
   └──────────┴──────────────────┴──────────────┴────────────┘
                                 │
                         assemble_report()
                   (EDGAR merge, tier inference,
                    section confidences, gap records)
                                 │ ReportDocument
           ┌─────────────────────┼───────────────────┐
           │                     │                   │
      Markdown              PDF + HTML            SQLite
     (report.md)        (report.pdf/.html)     (observability)
```

### Pre-classification

A lightweight Haiku call (no tools, ≤512 tokens) runs first. It sets flags
including `is_likely_public`, `is_government_contractor`, `legal_name` (the
SEC-registered name when it differs from the brand), and `ticker`. These shape
each agent's behavior without requiring tools.

### Phase 1: Five Parallel Agents

| Agent | Tools | Produces |
|-------|-------|---------|
| **Research** | `opencorporates_search`, `uspto_patent_search`, `web_search`, `web_fetch` | Overview, leadership, products, patent count, entity registration data |
| **Financial** | `web_search`, `web_fetch` | Funding rounds, investors, valuation, business model (defers revenue/profitability to EDGAR for US public companies) |
| **Risk** | `samgov_contract_search`, `courtlistener_case_search`, `web_search`, `web_fetch` | Regulatory, legal, cyber, operational, reputational risks; court dockets; federal contracts |
| **Social Media** | `web_search`, `web_fetch` | Twitter/X, LinkedIn, Reddit, Glassdoor sentiment |
| **EDGAR** | `edgar_find_company`, `edgar_get_financials`, `edgar_get_filing_text` | Audited financials and risk factors from SEC filings |

Each web-search agent has a prompt-enforced tool-call budget of 4–6 calls. The
EDGAR agent is deterministic: three tool calls in order (find → financials →
filing text), with MAX_TURNS=5 to accommodate retries. All agents produce
structured JSON parsed by Pydantic, with up to 3 retries on parse failure.

**EDGAR details:**
- For established US public companies it reads 10-K annual filings.
- For recent IPO filers with no 10-K yet it falls back to S-1 and 424B
  prospectuses, searched in that priority order.
- CIK resolution uses a three-pass cascade: ticker lookup → SEC-registered legal
  name → brand name. This handles companies where the brand name (e.g., a trade
  name) does not match the registered filer name.
- Risk factor text extraction can fail if a filing uses non-standard section
  headings. The `extraction_status` field (`extracted` | `section_not_found` |
  `fetch_failed`) is always surfaced and translated into a visible gap record
  when extraction fails.
- For brand-new filers where XBRL financials are not yet aggregated,
  `xbrl_available=false` is returned and financials are gapped. This is
  expected, not an error.

**Financial agent deferral:** For US public companies, the Financial agent
returns revenue and profitability as "unknown". The EDGAR agent provides audited
values and the assembler merges them. This keeps agent prompts simple.

**Risk agent attribution discipline:** The Risk agent distinguishes named
defendants from non-party references in court records. In rem and seizure
proceedings (docket prefixes `sz`, case captions "United States v. SEIZURE OF
...") are routed to `reputational_risks` at LOW severity, not attributed as
criminal charges against the company. Statutory violations are only attributed
to a company when it is the named criminal defendant.

### Phase 2: Synthesis

A synthesis agent (no tools) reads all five agent outputs and produces
cross-agent insights: executive summary, key strengths and concerns, red flags,
data conflicts, and an overall recommendation (`strong_proceed` through
`do_not_proceed`).

Rules enforced in the prompt:
- All numbers must appear verbatim in an upstream claim or be omitted; the
  agent may not compute, estimate, or average figures.
- EDGAR evidence takes precedence over classifier priors for determining
  public/private status.
- Data conflicts between agents are flagged in a `data_conflicts` field rather
  than silently reconciled.

### Assembly

`assemble_report()` in `src/synthesis/assembler.py` converts the six raw agent
dicts into a single `ReportDocument`:

- Annotates every claim with a stable 12-char hex `claim_id`
- Wraps each bare source URL in a `SourceRef` with an inferred `SourceTier`
- Merges EDGAR financials into the financial section
- Prepends SEC risk factors to the risk section's `regulatory_risks`
- Creates visible `GapRecord` entries for fields with no reliable data
- Computes `tier_coverage` (fraction of claims at each tier), per-section
  confidence percentages, and an overall weighted confidence score
  (Financial 40%, Risk 40%, Social Media 20%)

---

## Source Tier System

Every source URL is classified at assembly time via registry-first lookup:
known authoritative sources are matched by domain before falling back to a
general pattern set.

| Tier | Label | Examples |
|------|-------|---------|
| 0 | `primary_document` | SEC EDGAR, USPTO, SAM.gov, CourtListener, `.gov` domains |
| 1 | `reputable_secondary` | Reuters, Bloomberg, FT, WSJ, AP, TechCrunch, CNN, Forbes |
| 2 | `aggregator` | Crunchbase, PitchBook, Statista, Owler, Similarweb |
| 3 | `community` | Reddit, Twitter/X, LinkedIn, Glassdoor, GitHub |
| — | `unknown` | Everything else |

**Confidence capping:** claims backed solely by aggregator, community, or
unknown sources are capped at MEDIUM confidence. Material financial fields
(revenue, valuation) backed only by those tiers are capped at LOW. The overall
data quality rating is capped if tier-0/1 coverage falls below 25% or unknown
coverage exceeds 40%.

This is tier-based confidence bounding, not empirical confidence calibration.

---

## Two-Layer Schema

**Agent output layer** (what agents produce — sources are bare URL strings):

```python
class DataPoint(BaseModel):
    value: str
    confidence: ConfidenceLevel     # HIGH | MEDIUM | LOW | UNKNOWN
    sources: List[str]              # bare URL strings
    reasoning: Optional[str]
    severity: Optional[SeverityLevel]   # CRITICAL | HIGH | MEDIUM | LOW (risk items only)
    derived: bool                   # True when computed from other claims
    derived_from: List[str]         # claim_ids of source claims when derived=True
    synthesized_from: List[str]     # upstream claim_ids (synthesis agent only)
```

**Canonical report layer** (what renderers and downstream systems consume):

```python
class Claim(BaseModel):
    claim_id: str           # stable 12-char hex
    field_name: str
    value: str
    confidence: ConfidenceLevel
    severity: Optional[SeverityLevel]
    sources: List[SourceRef]    # tier-annotated, not bare strings
    agent: str
    reasoning: Optional[str]
    synthesized_from: List[str]
    derived: bool
    derived_from: List[str]

class SourceRef(BaseModel):
    url: str
    tier: SourceTier
    retrieved_at: Optional[datetime]    # always null; reserved for future use
```

Severity and confidence are independent: a risk can be HIGH severity with LOW
confidence (unconfirmed but potentially significant).

The schema is versioned (`SCHEMA_VERSION = "1.0.6"`) and published as
`schema/report.schema.json`. Drift is caught by `tests/test_report_schema.py`.

---

## Output Files

| File | Description |
|------|-------------|
| `outputs/report_{slug}.json` | Canonical `ReportDocument` |
| `outputs/report_{slug}.md` | Markdown: tables, confidence badges, severity sorting, tier coverage footer, gap section |
| `outputs/report_{slug}.pdf` | Styled PDF (ReportLab; no system dependencies) |
| `outputs/report_{slug}.html` | Standalone HTML companion |
| `outputs/agent_log.db` | SQLite observability database |

---

## Observability

All tables share `trace_id` as the universal correlation key.

**Layer 1 — Lightweight metrics** (`src/observability/tracer.py`):

| Table | Contents |
|-------|---------|
| `runs` | Trace ID, company, timestamps, total cost, status |
| `spans` | Per-LLM-call and per-tool-call: tokens, cost, duration, model, prompt version |

**Layer 2 — Full payload** (`src/observability/agent_db.py`):

| Table | Contents |
|-------|---------|
| `agent_runs` | Task, status, turns, token/cost totals, full result JSON |
| `llm_calls` | System prompt, request messages, response content, stop reason |
| `tool_calls` | Tool name, input JSON, result (up to 10K chars) |
| `messages` | Conversation replay (user/assistant/tool_result, truncated to 2K chars each) |

**Inspect a run:**

```bash
python scripts/query_db.py "Acme Corp"      # print DB contents for the latest run
streamlit run scripts/dashboard.py          # 8-page Streamlit dashboard
```

The dashboard pages: Overview, Traces, Agent Runs, Conversation Replay, LLM
Calls, Tool Calls, Evals, Raw SQL.

---

## Evaluation

Ground truth fixtures (`evals/ground_truth/`) contain stable facts for a set of
well-known companies. The evaluator compares agent outputs against these using
exact match, fuzzy match (substring and word overlap), and list overlap. Results
are persisted to the `eval_results` table and printed as a Rich terminal
scorecard.

---

## CLI Reference

```bash
# Run due diligence
python -m src.main "Acme Corp"

# JSON only (skip PDF/HTML render)
python -m src.main "Acme Corp" --json-only

# Print full DB dump after run
python -m src.main "Acme Corp" --dump-db

# Inspect an existing run
python scripts/query_db.py "Acme Corp"

# Run tests
./venv/bin/python -m pytest tests/

# Regenerate JSON schema after model changes
python -c "import json; from src.schemas.models import ReportDocument; \
           print(json.dumps(ReportDocument.model_json_schema(), indent=2))" \
> schema/report.schema.json
```

---

## Environment

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes | Claude API key |
| `BRAVE_API_KEY` | For real search | Brave Search API (free tier: 2,000 queries/month) |
| `USE_MOCK_SEARCH` | No | Set `true` to bypass Brave; useful for development |
| `EDGAR_USER_AGENT` | Recommended | Identifies your app to SEC per their policy (e.g. `MyApp/1.0 you@example.com`) |
| `SAM_GOV_API_KEY` | No | SAM.gov federal contractor data; Risk agent degrades gracefully without it |
| `OPENCORPORATES_API_KEY` | No | Required for OpenCorporates entity lookups; Research agent falls back to web search if absent |
| `COURTLISTENER_API_KEY` | No | Higher rate limits on CourtListener; unauthenticated mode works |

- Python 3.13, virtualenv in `./venv`
- SQLite: `outputs/agent_log.db` (auto-created on first run)

---

## Key Design Decisions

- **JSON as source of truth.** Markdown, PDF, and HTML are renders of the canonical
  `ReportDocument`. They are not edited independently.

- **Two-layer schema.** The agent output layer (`DataPoint`, `CompanyResearch`, etc.)
  stays stable across agent changes. The canonical layer (`Claim`, `ReportDocument`)
  is what renderers and downstream systems consume.

- **Registry-first tier inference.** `_infer_tier()` checks the source registry
  before domain pattern sets. Adding a new authoritative source requires only a
  registry entry, not assembler changes.

- **EDGAR deferral.** For US public companies, the Financial agent returns revenue
  and profitability as "unknown". The EDGAR agent provides audited values. The
  assembler merges them at assembly time. Agents stay simple; prompts stay stable.

- **Assembler as merge point.** Source-of-truth overlays — EDGAR into financial,
  SEC risk factors into regulatory risks — happen at assembly time, not in agent
  prompts.

- **Gaps, not fabrication.** Fields with no reliable data produce visible
  `GapRecord` entries. Agents are prompted to gap rather than estimate; the EDGAR
  agent always surfaces `extraction_status` so fetch failures are never silent.

- **Three-layer provenance.** Every `Claim` carries one of: `sources` (direct
  retrieval), `derived_from` (agent-computed from cited upstream claim IDs), or
  `synthesized_from` (synthesis agent citations of upstream claim IDs).

- **SourceCache.** SQLite TTL cache for Tier 0 API calls. Immutable filings cached
  indefinitely; company XBRL facts cached 24 hours. Reduces latency on repeated
  runs and respects SEC rate limits.

- **Opt-in observability.** Agents work without a database (`db=None`); full
  payload logging is additive.

- **Structured output with retries.** All agents produce JSON parsed by Pydantic.
  Up to 3 retries on parse failure before the run is marked FAILED.

---

## Project Structure

```
src/
├── main.py                    # Entry point and orchestrator
├── agents/
│   ├── base.py                # BaseAgent state machine + WebSearchMixin
│   ├── classifier.py          # Pre-classification (no tools, Haiku)
│   ├── research.py
│   ├── financial.py
│   ├── risk.py
│   ├── social_media.py
│   ├── synthesis.py
│   └── edgar.py
├── sources/
│   ├── registry.py            # Authoritative source tier registry
│   ├── cache.py               # SQLite TTL cache
│   ├── edgar.py               # EDGAR tool functions
│   ├── opencorporates.py      # US entity registration
│   ├── uspto.py               # USPTO PatentsView
│   ├── samgov.py              # SAM.gov federal contractor data
│   └── courtlistener.py       # CourtListener docket search
├── observability/
│   ├── tracer.py              # Run/span metrics
│   └── agent_db.py            # Full payload logging
├── schemas/
│   └── models.py              # DataPoint, Claim, ReportDocument schemas
└── synthesis/
    ├── assembler.py           # assemble_report() — canonical JSON builder
    ├── report_generator.py    # Markdown renderer
    └── pdf_report.py          # PDF + HTML renderer

schema/report.schema.json      # JSON Schema generated from Pydantic models
evals/                         # Ground truth fixtures and evaluator
scripts/
├── dashboard.py               # Streamlit observability dashboard
└── query_db.py                # CLI DB inspector
tests/                         # 518 unit and integration tests
```

---

## Disclaimer

This is a personal research project. It uses only publicly available information.
Output is generated by LLM agents and may contain errors, omissions, or stale
data. It is not financial, legal, or investment advice. Do not rely on it as the
sole basis for any business or investment decision.

---

## Roadmap

Capabilities not yet built:

- **Passage-level provenance.** `SourceRef.retrieved_at` is always null today.
  A future version would attach the specific text excerpt supporting each claim,
  not just the source URL.

- **Empirical confidence calibration.** The system applies tier-based confidence
  caps. Calibrating stated confidence against observed accuracy requires
  accumulated ground truth data.

- **Domain profiles.** Configurable profiles (`vendor_security`, `ma_target`,
  etc.) that weight agents and sources differently for specific use cases.

- **Conflict resolution stage.** The synthesis agent flags data conflicts between
  agents; it does not resolve them. A dedicated resolution step is deferred.

- **Reproducibility from content snapshots.** Prompt versions are tracked per
  agent. Source content is not yet hashed or snapshotted for strict run
  reproducibility.

- **Plugin architecture.** Adding a new source or agent requires code changes.
  A pluggable interface is planned but not built yet.
