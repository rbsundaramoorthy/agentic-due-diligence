# Project Brief: `agentic-due-diligence` — Strengthen on Its Own Merits

**Repository:** https://github.com/rbsundaramoorthy/agentic-due-diligence
**Constraints:** None at this time. Architecture, language, framework, and deployment choices are open.

## Purpose of this document

This is a strategic brief for Claude Code working on the `agentic-due-diligence` project. It describes what this project **is**, what it deliberately **is not**, and the prioritized engineering work that strengthens it as a structured, auditable, customizable research pipeline — **not** as a polished-memo product.

When you (Claude Code) work on tasks here, anchor every decision in this brief. If a proposed change pulls the product toward "narrative investment memo with charts," that's the wrong direction and should be flagged.

**Before making structural changes, read the existing repo carefully.** The orchestration layer has earned trust and should not be rewritten on assumptions. Read `README.md`, the agent definitions, and at least one full run's output before proposing refactors.

---

## 1. What this project is today

A multi-agent due-diligence pipeline that produces structured research reports with per-claim provenance.

**Current architecture (observed from latest run):**
- Agents: `classifier`, `research`, `financial`, `risk`, `social_media`, `synthesis`
- ~29 LLM calls, ~23 tool invocations per run
- ~$0.45 and ~9 minutes per report
- Output: PDF report with tabular findings, per-claim confidence labels (HIGH/MED/LOW), explicit information gaps, data-conflict flagging, and a methodology footer
- Confidence scoring is **declared per claim** — this is a real differentiator

**What works well already:**
- Agent orchestration is real and visible in output
- Tool-invocation accounting exists
- Confidence scoring exists per claim
- Information gaps are surfaced explicitly
- Cost and token usage are reported transparently

**What is weak today:**
- Synthesis stage is shallow (1 LLM call, ~10k tokens) — aggregation, not reasoning
- Output is PDF-first; structured data is implicit
- Citations are at the source-list level, not per-claim
- Confidence labels are uncalibrated (we don't actually know if MED claims are ~70% accurate)
- Conflicts are flagged but not resolved
- No domain customization — same agents/tools regardless of use case
- No reproducibility guarantees

---

## 2. Strategic positioning

**This is not a competitor to polished-memo products** (Perplexity Comet, Anthropic Deep Research, OpenAI Deep Research, Gemini Deep Research). Those products win on narrative, visualization, and editorial polish. We will lose that fight on every dimension that matters to their buyers.

**This is a structured, auditable, programmatic research pipeline.** It produces a data product with provenance — the PDF is one render of it, not the source of truth.

### Target users

In priority order:
1. **Compliance, audit, legal, regulated-industry teams** who cannot ship LLM output without traceable provenance
2. **Engineering teams building agent systems** who need this as a component, not a deliverable
3. **Downstream pipelines** (RAG corpora, CRMs, deal-flow systems, BI tools) that consume structured input
4. **Risk and analyst teams** who write the memo themselves and want a research assistant, not a competitor

### What we sell that competitors can't

- **Provenance:** every claim is linked to the agent, tool call, source, and timestamp that produced it
- **Auditability:** runs are reproducible; prompts and tools are versioned
- **Customization:** domain-specific agent and tool configurations (vendor security, M&A target, legal diligence, etc.)
- **Programmability:** JSON-first output that downstream systems can consume
- **Calibration:** empirically validated confidence scores

### The three-layer provenance model

This is the architectural vocabulary that makes the pipeline auditable. Every `Claim` in the canonical report falls into exactly one of these patterns:

```
sources        URLs an agent retrieved directly. Tier-classified (Tier 0–3).
               The claim value is what those sources say.

derived_from   claim_ids of atomic input claims this claim was computed from.
               Set derived=True when the agent performed arithmetic (growth
               rates, margins, ratios, sums). The value contains the agent's
               computation; the inputs are traceable.

synthesized_from  claim_ids of upstream claims the synthesis agent drew from.
               Cross-agent, synthesis layer only. The value is a cross-agent
               inference or judgment, not new retrieval.
```

A compliance reviewer can follow any claim backward:
- `sources` claim → source URL → retrieved document
- `derived` claim → atomic input claims → their source URLs
- `synthesized` claim → upstream claims → their source URLs

No other due-diligence pipeline makes this chain explicit in the JSON. This is the main intellectual differentiator.

**P4 calibration implication:** source-grounded claims and derived claims have different failure modes. A source-grounded claim fails because the source was wrong or misread. A derived claim fails because the arithmetic was wrong. Calibrating them separately will produce more useful error attribution than treating all claims identically.

---

## 3. Improvement roadmap

Work in priority order. Each item lists rationale, what "done" looks like, and where to start.

### Priority 1 — Make structured output the source of truth

**Rationale:** Today the PDF is the artifact. That's backward. Downstream systems need JSON; humans get the PDF as a render.

**Done when:**
- Every run produces a canonical JSON document conforming to a published schema
- The schema covers: claims, sources, agents, tool calls, confidence, timestamps, conflicts, gaps, methodology metadata
- **Each source object carries a `tier` field** (`primary_document`, `reputable_secondary`, `aggregator`, `community`) — this enables Priority 3's source-aware acquisition and Priority 4's tier-weighted calibration
- The PDF (and any other format) is generated from the JSON, never edited independently
- The JSON validates against a schema (JSON Schema or Pydantic)

**Start here:**
- Define the schema in `schema/report.schema.json` and a corresponding Pydantic model
- Refactor the synthesis stage to emit JSON, then render PDF from it
- Snapshot existing reports as JSON to validate the schema covers real cases

### Priority 2 — Per-claim inline citations

**Rationale:** A "Sources" column at the bottom of a section is not provenance. Compliance buyers need to point at any claim and trace it to the exact passage that produced it.

**Done when:**
- Every claim object in the JSON carries one or more `evidence` objects with: source URL, retrieved passage (or hash), retrieval timestamp, the agent and tool call that retrieved it
- The PDF renders inline citation markers next to each claim
- The viewer (CLI or web) can expand a claim to show its full evidence chain

**Start here:**
- Extend research-stage tool wrappers to capture passage-level evidence, not just URLs
- Update the claim schema to require ≥1 evidence object
- Add a "show evidence" command/view

### ✅ Priority 3 — Tiered source registry and primary-source agents [SHIPPED — P3a: v0.3.0, P3b: v0.4.0]

**Rationale:** Source diversity and calibration matter more than access to any one prestigious source. Compliance, engineering, and downstream-pipeline buyers care whether a claim is *correct and traceable*, not whether it came from a brand-name outlet. A SEC 10-K beats a Bloomberg article on the same company's revenue. The NY State Comptroller's actual letter beats a Reuters summary of it. This priority operationalizes that principle.

**The tiered source model:**

- **Tier 0 — Primary documents (free, high-signal).** SEC EDGAR (10-K/10-Q/8-K/S-1), Companies House (UK), SEDAR (Canada), SAM.gov / USAspending.gov, PACER / CourtListener, OFAC and sanctions lists, USPTO / EPO Espacenet, BLS / FRED, arXiv / Semantic Scholar. These should be wired in first — most defensible claims trace back here.
- **Tier 1 — Cheap commercial search and extraction APIs.** Brave Search API or Tavily for agent-optimized search; Firecrawl or Exa for full-content extraction. This is where the first real spend goes (~$100–200/month gets meaningful volume). Replaces or supplements whatever general web search is wired in today.
- **Tier 2 — Specialized commercial APIs ($50–500/month).** Financial Modeling Prep, Alpha Vantage, Polygon, Finnhub for financials; OpenCorporates for entity/ownership graphs; NewsAPI / GDELT for news aggregation. Add per domain profile (Priority 5), not globally.
- **Tier 3 — Premium licensed feeds ($5k–$50k+/year).** Refinitiv, Factiva, LexisNexis, Bloomberg Terminal, S&P Capital IQ, Pitchbook. **Do not pursue these yet.** They are a Path A signal — buyers who require them are buyers who want a memo, not a pipeline. Revisit only after Priorities 1–6 are shipped and a paying customer explicitly requires Tier 3.

**Done when:**
- A source registry exists that maps every source to its tier, freshness expectations, and rate limits
- Each Tier 0 source has a dedicated agent or tool wrapper (start with: SEC EDGAR, OpenCorporates, USPTO, SAM.gov)
- General web search is served by Brave Search API or Tavily, not a generic SERP
- Content extraction beyond snippets is served by Firecrawl or Exa
- Every claim's `evidence` carries the tier of its source (per Priority 1 schema)
- A Tier-1 (paid) search/extraction account exists and is in production use

**Start here:**
1. **Add Tier 0 source agents in this order:** SEC EDGAR → OpenCorporates → USPTO → SAM.gov. Each is its own agent/tool with rate limiting and caching.
2. **Swap general web search to Brave Search API or Tavily** — agent-optimized retrieval with relevance filtering, not just SERP JSON.
3. **Add Firecrawl or Exa for full-content extraction** — snippets aren't enough for claim-grade evidence.
4. **Wire `source.tier` into the schema and into the evidence captured at retrieval time** — not inferred later.
5. **Build a source-quality eval set** that distinguishes Tier 0 / Tier 1 / Tier 2 claims so Priority 4 (calibration) has tier-labeled ground truth.

### Priority 4 — Empirically calibrate confidence scores

**Rationale:** HIGH/MED/LOW labels are decorative unless they predict accuracy. Calibration is what turns confidence labels from theater into a feature. Source tier (from Priority 3) is one of the strongest predictive features of claim correctness.

**Done when:**
- An eval harness exists that scores a corpus of claims against ground truth
- We can report: "HIGH claims are correct X% of the time, MED claims Y%, LOW claims Z%"
- **Confidence is computed with explicit tier weighting** (e.g., a single Tier 0 source can support HIGH; a single Tier 2 aggregator cannot)
- **Source diversity is a calibration input** — a claim backed by three independent sources of different tiers scores higher than a claim backed by one aggregator
- The labels are tuned (or the prompts that produce them are tuned) until those numbers are sensible and stable
- Calibration is re-run on every significant prompt or model change

**Start here:**
- Build a small (50–200 claim) labeled eval set across at least two domains, with tier annotations
- Wire up an eval runner that compares pipeline output against the labels
- Define the tier-weighting formula explicitly (and version it; see Priority 7)
- Publish a calibration report; iterate on the prompts and weights that produce confidence labels

### Priority 5 — Domain customization via configuration

**Rationale:** A vendor-security diligence run and an M&A target diligence run should be configurations of the same pipeline, not separate codebases. This is where the moat compounds. It's also where Tier 2 paid feeds (Priority 3) belong — wired into specific profiles that justify the cost, not as a global dependency.

**Done when:**
- A run is parameterized by a `profile` (e.g., `vendor_security`, `ma_target`, `legal_diligence`)
- Profiles declare: which agents run, which tools each agent can call, which sources to prefer, which sections appear in the output, which schemas extend the base
- **Profiles can opt into Tier 2 paid feeds** (e.g., Polygon or Financial Modeling Prep for `ma_target`; OpenCorporates Pro for `vendor_security`) without affecting other profiles
- Adding a new domain is a config change plus optional new agents/tools — never a fork

**Start here:**
- Refactor the current pipeline as the `general_company` profile
- Define the profile schema
- Build one second profile end-to-end (suggest: `vendor_security` — narrower scope, clearer market)
- Decide which Tier 2 feed, if any, the second profile should depend on — and document the cost/value trade-off in the profile definition

### Priority 6 — Conflict resolution as a pipeline stage

**Rationale:** Today conflicts are flagged and dropped on the reader. A reconciliation agent should attempt resolution and preserve the reasoning.

**Done when:**
- Detected conflicts trigger a `reconciliation` agent
- The agent attempts to resolve by re-querying sources, comparing methodologies, or producing a synthesized claim with its reasoning preserved
- Unresolvable conflicts are explicitly marked as such, with the reasoning attached — not silently flagged
- Resolved claims carry both the resolution and the original conflicting claims as evidence

**Start here:**
- Build a `reconciliation` agent with access to research tools
- Add a `conflict_resolution` field to claim objects in the schema
- Test on known conflicts (e.g., the $5B GAAP loss vs. $8B EBITDA profit case from the SpaceX run)

### Priority 7 — Reproducibility and versioning

**Rationale:** Compliance buyers need to defend a finding six months later. That requires the ability to re-run a report with the same inputs and get the same (or auditably different) result.

**Done when:**
- Every run records: prompt versions, agent versions, tool versions, model versions, seeds, source snapshots (or hashes of fetched content)
- Re-running a report from its run manifest produces an output that is either byte-identical or differs only in places explicitly marked as non-deterministic (e.g., new web content)
- A diff tool shows what changed between two runs of the same report

**Start here:**
- Version every prompt; store in a `prompts/` directory with semver
- Cache or hash all fetched external content per run
- Add a `run_manifest.json` to every output
- Build a `replay` command

### Priority 8 — Plug-in architecture for agents and tools

**Rationale:** The moat compounds when users (or you) can add domain-specific agents (SOC2 auditor, patent searcher, SEDAR fetcher, Tier 0 source agents for new jurisdictions) without modifying core code. The Tier 0 source agents from Priority 3 are the first natural plugins.

**Done when:**
- Agents and tools are registered via a discoverable plugin interface
- A third-party package can ship an agent/tool that drops in
- Profiles can reference plugin agents/tools by name
- The Tier 0 source agents (EDGAR, OpenCorporates, USPTO, SAM.gov) are themselves shipped via this plugin interface, not hardcoded

**Start here:**
- Define the agent and tool plugin protocols (Python protocols / ABCs)
- Refactor existing agents/tools — including the Tier 0 source agents from Priority 3 — to use them
- Document how to ship a plugin

---

## 4. Non-goals (do not do these)

These look attractive but pull the product toward Path A. Flag and discuss before implementing any of them.

- **Narrative prose generation as a primary feature.** The output should be navigable structured findings, not flowing memo prose. A short executive summary is fine; chapter-length analysis is not.
- **Charts and data visualizations as a core deliverable.** A chart endpoint that consumes our JSON is fine. Charts as the headline feature is not. We are not a BI tool.
- **Bull/base/bear scenario synthesis.** Leave scenario analysis to the human analyst who consumes our output. We provide the facts and confidence; they provide the judgment.
- **Source aggregator dependency.** Don't lean on any single aggregator (e.g., Sacra, Crunchbase) as a primary source. Source diversity is part of quality.
- **Premium licensed feeds (Tier 3) for credibility.** Bloomberg Terminal, Factiva, Refinitiv, Pitchbook, S&P Capital IQ are not on the roadmap. Their buyers want memos; ours want pipelines. Source quality is achieved through Tier 0 primary documents plus Tier 1 agent-grade search, not through brand-name licensing.
- **"Make it look like a McKinsey deck."** Polish for its own sake is the wrong axis to compete on.

---

## 5. Success metrics

We're doing the right things if, over the next two quarters, we can credibly claim:

- 100% of claims in every report carry per-claim evidence with passage-level provenance
- Every source carries a tier label; reports surface tier coverage (% Tier 0, % Tier 1, etc.)
- Median claim is backed by ≥2 independent sources spanning ≥2 tiers
- Tier 0 primary-source agents (EDGAR, OpenCorporates, USPTO, SAM.gov at minimum) are wired in and produce a measurable share of claims
- Confidence labels are empirically calibrated, with calibration broken out by source tier, and published per release
- At least two domain profiles ship end-to-end with measurably different agent/tool configurations
- Runs are reproducible from their manifests
- Output JSON is consumed by at least one downstream system (internal or external)
- At least one third-party (or internal team) has shipped a plugin agent or tool

We're doing the wrong things if we find ourselves measuring:

- Length of the prose narrative
- Number of charts in the PDF
- How "polished" the output looks compared to Perplexity / Deep Research outputs
- Number of brand-name premium feeds we subscribe to

---

## 6. Working notes for Claude Code

- **Investigate before refactoring.** Read the `agentic-due-diligence` codebase carefully before proposing structural changes. The orchestration side — agents, tool invocation, confidence labeling, methodology accounting — has earned trust and should be preserved or extended, not rewritten.
- **Schema-first.** When in doubt, define the data shape first, then the agents that produce it, then the renderers that consume it.
- **Keep the methodology footer.** The transparency about LLM calls, tokens, cost, and tool invocations is part of the product, not debug output.
- **Don't delete confidence labels in favor of "better" alternatives until calibration data exists.** The labels are valuable even uncalibrated; replace them only when you have something measurably better.
- **When a feature could be a config rather than code, make it a config.** Profiles are where the leverage lives.
- **Tier-aware thinking, always.** When adding any source, tag its tier explicitly. When weighing claims, weight by tier. When a question can be answered from a Tier 0 primary document, prefer that over a Tier 2 aggregator even if the aggregator is faster.
- **Three-layer provenance vocabulary.** `sources` = direct retrieval; `derived_from` = same-agent arithmetic; `synthesized_from` = cross-agent synthesis. Every change to the claim schema, agent outputs, or assembler should preserve this taxonomy. If a feature doesn't fit these three layers, ask whether it belongs as a new layer or as metadata on an existing one.
- **Flag any task that smells like Path A.** If a request would push the product toward narrative-memo territory — or toward Tier 3 premium-feed dependencies — surface the tension before implementing.
- **No constraints have been declared on language, framework, deployment, or external services.** That means you have latitude to propose changes — but it also means you should *propose* meaningful architectural choices for review rather than silently introducing dependencies. When you introduce a new library, model, or service, justify it in the PR description.

---

## 7. One-paragraph summary for context windows

`agentic-due-diligence` (https://github.com/rbsundaramoorthy/agentic-due-diligence) is a multi-agent due-diligence pipeline producing structured, auditable research reports with per-claim provenance and declared confidence. It is not a polished-memo product and should not be tuned to compete with Perplexity Comet, Deep Research, or similar. The strategic direction is to deepen what already makes it distinct: structured JSON as the source of truth, per-claim inline citations, a tiered source registry (Tier 0 primary documents → Tier 1 agent-grade search → Tier 2 specialized commercial APIs, with Tier 3 premium licensed feeds explicitly out of scope), empirically calibrated tier-weighted confidence, domain-specific profiles, conflict resolution as a pipeline stage, reproducible runs, and a plugin architecture for agents and tools. Target users are compliance, engineering, and downstream-pipeline teams who need traceable, programmable research output — not analysts who want a memo handed to them.
