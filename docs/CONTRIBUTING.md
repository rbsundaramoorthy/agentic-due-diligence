# Contributing

This document describes how work gets done on `agentic-due-diligence`. It's primarily a guide for AI-assisted development (Claude Code), but the process is the same for human contributors. Read this before starting work on any priority from `docs/STRATEGY.md`, before proposing a new priority, or before opening a non-trivial PR.

## The three-document contract

Three documents work together. Each has a different job. Keeping them in their lanes prevents drift.

| Document | Purpose | Tense |
|---|---|---|
| `docs/STRATEGY.md` | Strategic positioning, prioritized roadmap, non-goals, success metrics | Forward-looking |
| `CLAUDE.md` | Accurate inventory of what exists today, plus working principles for contributors | Present tense |
| `README.md` | Public-facing description of what the system does and how to use it | Present tense |

When a priority ships:
- `STRATEGY.md` gets updated to mark the priority done and shift focus to the next one
- `CLAUDE.md` and `README.md` get updated to describe the new "what is" state in the same PR

If you find yourself wanting to document something speculative ("we'll eventually have X") in `CLAUDE.md` or `README.md`, that's a signal it belongs in `STRATEGY.md` instead. Don't pre-document features that haven't shipped.

## How priorities get planned and implemented

Every priority from `STRATEGY.md` follows the same loop. This isn't bureaucracy — it exists because the priorities are foundational architectural work, and the cost of getting the shape wrong is much higher than the cost of one review cycle.

### Step 1 — Strategic check

Before any planning happens, confirm:

- Is the priority still the right next thing to ship? Priorities in `STRATEGY.md` are ordered, but the order is a recommendation, not a contract. Skipping a priority or reordering is legitimate when context has changed — but the reasoning should be written down.
- Is there a named user or use case that motivates this priority *now*? Speculative engineering against hypothetical users has a poor track record. If the answer is "to be more like Path B in the brief," that's not enough on its own.
- What's the opportunity cost? Every priority delays the next one.

If a priority is being deferred or reordered, update `STRATEGY.md` to reflect that and note why.

### Step 2 — Planning prompt

For any priority larger than a single self-contained change, write a planning prompt rather than jumping to implementation. The planning prompt is a structured request for Claude Code (or any contributor) to *propose* an approach before writing code.

A good planning prompt includes:

1. **Context** — what's already shipped, what shipped most recently, what's deferred. Two or three sentences. Assumes the reader will read `STRATEGY.md` and the codebase, doesn't duplicate them.
2. **Scope constraints** — anything that's out of scope for this priority that might otherwise be inferred as in-scope. Put these prominently, at the top of the prompt. Examples: jurisdiction limits ("US only"), platform limits, deferral of related concerns to a later priority.
3. **What the plan needs to cover** — a numbered list of specific topics the plan must address. This is what makes plans comparable across priorities. Typical items: schema design, capture mechanism, integration with existing pipeline, migration strategy, test plan, doc updates.
4. **Open questions** — explicit decisions the planner must make, presented as multiple options with the trade-offs sketched. This is the most important section. Don't ask "design X"; ask "for X, choose between options A, B, C, here's the trade-off for each, pick one with reasoning." Forces explicit decisions instead of buried assumptions.
5. **Additional thoughts to include** — second-order effects, things to flag if the planner spots them, honest assessments of risk.
6. **Deliverable specification** — exactly what the plan document should contain: Pydantic models, sample JSON, diff-style file change summary, complexity rankings, recommended PR splits, the planner's own open questions.

The planning prompt should explicitly tell Claude Code (or the contributor) **not to write code yet**. The output is a plan document, nothing more.

Past planning prompts for shipped priorities live in the PR descriptions or in `docs/priorities/` — referencing one is a good way to start the next.

### Step 3 — Plan review

The plan comes back as a markdown document. Review it against:

- **Does it follow the brief, or does it drift toward Path A?** Watch for proposals that look like Perplexity / Deep Research mimicry (narrative synthesis, chart generation, premium-feed integration). If you spot drift, push back rather than approving with reservations.
- **Are the open questions actually resolved, or punted?** Phrases like "this could be revisited in P4" or "P3 accepts the limitation of X" are sometimes correct deferrals and sometimes hidden punts. Apply judgment.
- **Are the easy decisions actually right?** Schema version bumps, cache TTLs, default values, error semantics, classifier gating — these read as small but are the kind of choice that bites you in production. Check them deliberately.
- **Are the planner's own open questions substantive or stylistic?** A good plan ends with two or three substantive questions for the reviewer. If the planner has no questions, it's either nailed the design or hidden the hard parts.

The review either approves the plan, approves with required changes, or rejects it. "Required changes" should be specific — name the section, name the change, name the reason. Don't approve in principle and assume Claude Code will infer the details.

### Step 4 — Approval message

The approval message is a short structured response that includes:

- **Required changes** — numbered, specific, with reasoning where it's not obvious
- **Refinements** — smaller adjustments that don't block implementation but should be made
- **Answers to the planner's open questions** — one per question, decisive
- **What to keep as proposed** — explicit affirmation of the parts that are right, so the planner knows what *not* to change
- **Final instruction** — "proceed to implementation" or "surface any second-order effects you find before writing code"

This message is the contract for implementation. If you find yourself wanting to renegotiate parts of it mid-implementation, that's a signal the plan wasn't ready and a new plan-review round is warranted.

### Step 5 — Implementation

Once the plan is approved, Claude Code (or the contributor) implements. During implementation:

- **Surface unexpected complexity before writing the code, not after.** If a required change turns out to be much harder than estimated, or has second-order effects the plan didn't see, raise it. Don't silently substitute an easier approach.
- **Stay within scope.** The plan defines what ships in this PR. Tempting refactors, drive-by improvements, and "while I'm in here" changes belong in separate PRs or notes for future priorities.
- **Update docs in the same PR.** `CLAUDE.md`, `README.md`, and `CHANGELOG.md` must reflect the shipped state when the PR merges. Don't defer documentation; it accumulates as drift.
- **Follow the test plan.** New test modules and fixtures defined in the plan are part of the deliverable, not optional.

### Step 6 — Post-merge housekeeping

After a priority ships:

- Update the `STRATEGY.md` priority list to mark the priority done. Move it from active direction to a "Shipped" section or remove it entirely.
- Update `CHANGELOG.md` with a versioned entry.
- Update `README.md`'s Direction section.
- If the priority introduced new public surface (env vars, CLI flags, file outputs), make sure the README's Environment / CLI / Project Structure sections reflect it.
- Tag the release if appropriate.

This step is small enough that it's tempting to skip — and skipping it is how the three-document contract erodes. Do it.

## Working principles

These apply across all contributions, not just priority implementations.

### Investigate before refactoring

The orchestration side of this codebase — agents, tool invocation, confidence labeling, observability — has earned trust. Read it before proposing structural changes. The pattern that looks like a smell from outside often turns out to be load-bearing.

### Schema-first

When in doubt, define the data shape first, then the code that produces it, then the code that consumes it. The canonical `ReportDocument` and its component models are the contract. Changes to that contract are not casual.

### Tier-aware thinking

When adding any source of information, tag its tier explicitly. When weighing claims, weight by tier. When a question can be answered from a primary document (SEC EDGAR, court records, regulatory filings), prefer that over a secondary aggregator. The tier dimension is one of the project's core differentiators; protect it.

### Path A drift

If a proposed change pushes the product toward narrative-memo polish, chart-heavy output, premium-licensed-feed dependencies, or any other Path A direction (see `STRATEGY.md` non-goals), surface the tension before implementing. The project competes on provenance, auditability, and programmability — not on output polish. Drift in this direction is the single biggest risk to the project's positioning.

### Keep the methodology footer

The transparency about LLM calls, tokens, cost, tool invocations, and (now) tier coverage is part of the product, not debug output. Don't remove or de-emphasize it in pursuit of a cleaner-looking report.

### Versioning policy

The canonical schema follows semver:
- **Patch** (1.0.x): additive optional fields with sensible defaults. Fully backward compatible.
- **Minor** (1.x.0): additive fields with no default (consumers must handle absence), or breaking changes to non-required fields.
- **Major** (x.0.0): breaking changes to required fields, restructuring of section shape.

Every schema change includes a `SCHEMA_VERSION` bump and a regenerated `schema/report.schema.json`. The contract test in `tests/test_canonical_json_contract.py` catches drift.

### Reproducibility discipline

Prompts have `PROMPT_VERSION` constants. Schemas have `SCHEMA_VERSION` constants. Tools have caching keyed by stable hashes. These mechanisms exist so that runs are defensible and replayable. Don't bypass them for convenience — that's exactly the cost a compliance buyer is paying us to eat.

### Update docs in the same PR as the code

This bears repeating. The three-document contract only works if the docs change when the code changes. A PR that ships a feature without doc updates ships drift.

## Process exceptions

Not every contribution needs the full plan-review-approve-implement cycle. Use judgment:

| Type of change | Process |
|---|---|
| Priority implementation (from `STRATEGY.md`) | Full cycle: planning prompt → plan → review → approval → implementation |
| New agent, tool, or schema field | Full cycle |
| Bug fix | Direct PR; describe the bug and the fix in the description |
| Test addition or improvement | Direct PR |
| Doc fix (typos, clarifications, accurate-state corrections) | Direct PR |
| Refactor of an existing module with no behavior change | Brief proposal in an issue or PR description first; doesn't need a full plan |
| New feature not in `STRATEGY.md` | Open an issue first describing the strategic case. If accepted, it becomes a new priority and follows the full cycle. |

When in doubt, lean toward the heavier process. Plan-review is cheap; mid-implementation course corrections are expensive.

## Working with Claude Code specifically

A few things that have proven to help when using Claude Code on this project:

- **Make sure `docs/STRATEGY.md` is present in the repo before prompting.** `CLAUDE.md` references it; if the file is missing, the references don't resolve and the strategic context doesn't load.
- **Run Claude Code from the project root.** It reads `CLAUDE.md` automatically from there.
- **Trust the planning prompt template, but tune it per priority.** The structure (context, scope, plan-covers, open questions, deliverables) is stable. The specifics inside each section change per priority.
- **Push back on punts.** When a plan defers a hard decision to "a later priority," scrutinize whether that's a legitimate deferral or a hidden shortcut. Often it's the former; sometimes it's the latter.
- **Approve specifically.** Don't say "approved with some changes I'll send in a follow-up." The approval message is the contract; if it's vague, the implementation will be too.
- **Surface, don't substitute.** If implementation reveals a problem with the approved plan, raise it in chat before writing the workaround into code.

## Past planning prompts

Reference prompts from prior priorities, useful as templates:

- **P1 (JSON as source of truth)** — see PR description for `feat/p1-canonical-json` or `docs/priorities/p1-plan.md` if archived
- **P3 (Tiered source registry + EdgarAgent)** — see PR description for `feat/p3a-edgar-registry` and `feat/p3b-tier0-tools`

If you're planning a new priority, skim one of these first to see what a complete plan-prompt looks like in practice.
