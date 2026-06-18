# Changelog

All notable changes to this project will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added

- **Fix dangling synthesized_from provenance in synthesis claims (2026-06-18):**
  Synthesis claims for specific fields (key_strengths, key_concerns, red_flags,
  data_conflicts) were ending up with empty or dangling `synthesized_from` after
  assembly, failing the pipeline's provenance invariant.

  Three root causes and fixes:

  1. **EDGAR not annotated → EDGAR DataPoints had no stable `_claim_id`.**
     `edgar_data` is now run through `annotate_claim_ids` in `main.py` alongside
     the other agents. EDGAR's revenue, profitability, and sec_risk_factors
     DataPoints now carry stable IDs end-to-end.

  2. **EDGAR overlay used financial agent's (gap) `_claim_id`.**
     `_edgar_overlay_financial_dict` was copying the financial agent's `_claim_id`
     onto the EDGAR DataPoint. The financial agent's DataPoint is a gap
     (unknown/unknown) and does NOT become a Claim in the assembled document —
     so synthesis citing it was always dangling. Fix: the overlay now uses the
     EDGAR DataPoint's own annotated `_claim_id`, which IS the one placed into
     the assembled `doc.financial.revenue` (via `_merge_edgar`). The provenance
     chain is now stable end-to-end with no dangling references.

  3. **EDGAR sec_risk_factors not visible as citable claims in the synthesis task.**
     `_format_edgar_summary` showed only `sec_risk_factors: N found (count)`.
     Synthesis had no `_claim_id` values to cite and resorted to paraphrases
     like "sec_risk_factors extracted". Fix: the EDGAR section now lists each
     sec_risk_factor DataPoint with its `_claim_id` (block titled "SEC RISK
     FACTORS — cite these _claim_ids in synthesized_from…").

  **Pre-assembly validator** (`_validate_synthesis_before_assembly` in assembler):
  A new pre-assembly pass computes the citable set — the set of `_claim_id` values
  that WILL survive as Claims in the assembled document (non-gap DataPoints with
  annotated IDs, plus EDGAR claims that will be merged) — and strips any
  `synthesized_from` reference not in that set before `_assemble_synthesis` runs.
  For specific-field claims (key_strengths, key_concerns, red_flags, data_conflicts)
  left with no valid references, the claim is DROPPED (not kept with empty
  synthesized_from — the Pydantic Claim model enforces that specific fields must
  have non-empty provenance). The existing `_validate_synthesized_from` (post-
  assembly) remains as a backstop.

  **Synthesis prompt hardening** (`PROMPT_VERSION "2.4"` for SynthesisAgent):
  System prompt and task prompt both now explicitly list what must NOT appear in
  `synthesized_from`: metadata field names (edgar_lookup_status, most_recent_filing,
  cik), agent-output field names (e.g. "overall_sentiment"), and paraphrases.

  Tests: `TestPreAssemblyCitableIds` (8 cases) and
  `TestValidateSynthesisBeforeAssembly` (11 cases, including 3 integration tests
  that run the full assembler pipeline) in `tests/test_assembler.py`.

- **Synthesis reconciliation with EDGAR-merged financials (2026-06-17):**
  Synthesis now evaluates `data_quality` and `data_conflicts` against the
  EDGAR-merged financial state, not the pre-merge deferrals.

  Root cause: the financial agent returns `revenue/profitability = unknown`
  for US public companies (by design — EDGAR provides the authoritative values).
  The EDGAR-to-financial merge runs in the assembler, AFTER synthesis. Synthesis
  therefore saw unknown financials and wrote `data_quality.reasoning` describing
  them as deferred or unverified, and in some cases raised a spurious
  `data_conflicts` entry contrasting the financial agent's unknown with EDGAR's
  confirmed values — even though the final report showed those fields at
  primary_document tier with high confidence.

  Fix: `_edgar_overlay_financial_dict()` in `src/agents/synthesis.py` creates
  a shallow-merged view of `financial_data` with EDGAR revenue/profitability
  overlaid (same logic as `_merge_edgar` in the assembler, but at the dict
  level, before Claim construction). `build_synthesis_task` passes this merged
  view to the synthesis LLM. The assembler's own `_merge_edgar` is unchanged —
  it still runs at assembly time on the Claim objects.

  The original `financial_data` dict is not mutated. The `_claim_id` from the
  original DataPoint is preserved in the overlay so synthesis can cite it in
  `synthesized_from`; any dangling reference after assembly is stripped by
  `_validate_synthesized_from` with a warning (pre-existing behavior — EDGAR
  merge creates a new claim_id at assembly time).

  Side-effect guards verified: financial values, tiers, and confidences in the
  assembled report are unchanged; gap-pruning is unchanged; unrelated conflicts
  are unaffected.

  Tests: `TestEdgarOverlayFinancialDict` (10 cases) and
  `TestBuildSynthesisTaskEdgarOverlay` (4 cases) in `tests/test_synthesis.py`.

- **Graceful degradation for web agents (2026-06-17):** Web-searching agents
  (Research, Financial, Risk, Social Media) no longer return `data=None` when
  they run long. Two complementary changes:

  - **Soft budget in `BaseAgent.run()` (`src/agents/base.py`):** Agents accept
    a `soft_budget_seconds` instance variable (set by main.py to 70% of the
    hard timeout, i.e. 210s out of 300s). At the start of each turn, elapsed
    wall time is checked. When the soft budget is exceeded, the next LLM call
    is made *without* the `tools` parameter — forcing the model to emit its
    final JSON immediately rather than continue calling tools. A budget note is
    appended to the system prompt for that call. On successful parse, the result
    carries `status="partial"` and a gap note indicating limited coverage.
    EDGAR agent is excluded (deterministic tool sequence, no benefit from soft
    budget).

  - **Volume caps in `WebSearchMixin` (`src/agents/base.py`):** `MAX_SEARCH_RESULTS=5`
    caps how many results are fed to the model per search call (regardless of
    what the agent requests). `MAX_FETCHES=4` caps the total number of
    `web_fetch` calls per run; subsequent calls return a budget-exhausted JSON
    rather than raising. Research and Risk agents inherit these caps via their
    existing `super()` delegation chain.

  - **Backstop upgrade in `main.py`:** The hard-timeout handler now returns
    `status="partial"` (was `"failed"`) so timeouts produce the same schema as
    normal partial results and are handled uniformly by downstream assembler
    and renderers.

  - **Tests (`tests/test_agents.py`):** `TestSoftBudget` and
    `TestWebSearchVolumeCaps` cover: budget-exhausted forces partial with
    non-null data; no-budget path stays complete; search cap; fetch cap;
    per-agent fetch counter isolation.

  - **Test fix (`tests/test_schemas_and_tracer.py`):** Model ID in tracer cost
    tests updated from the retired `claude-sonnet-4-20250514` to `claude-sonnet-4-6`.

- **EDGAR PR2 — S-1/424B prospectus support (2026-06-13):** Extends EDGAR
  to handle companies whose only SEC filings are a registration statement or
  prospectus (no 10-K yet). Primary use case: SpaceX (CIK 0001181412, ticker
  SPCX, IPO'd 2026-06-12 on Nasdaq).

  - **Two-pass discovery in `edgar_find_company`:** Pass 1 searches `10-K`
    (preferred for companies with annual reports); Pass 2 searches
    `S-1, S-1/A, 424B4, 424B3, 424B5` as a fallback for pre-10-K filers.
    An ambiguous 10-K result is returned immediately; a 10-K mention-only
    rejection falls through to the S-1/prospectus pass. Result now includes
    `filing_type` and `most_recent_filing` (with `most_recent_10k` as a
    backwards-compat alias for 10-K results).

  - **Empty XBRL handling in `edgar_get_financials`:** When companyfacts
    returns HTTP 200 with an empty `us-gaap` section (brand-new registrant,
    XBRL not yet aggregated), the function returns `xbrl_available=False` with
    `revenue=None` and `net_income=None`. This is NOT an error — the company
    is a valid SEC filer; the data just isn't there yet. The EDGAR agent is
    instructed to gap the financials rather than fabricate numbers.

  - **S-1/424B section extraction in `edgar_get_filing_text`:** The function
    now accepts all `_PRIMARY_FORM_TYPES` as valid primary document types
    (previously only 10-K/S-1/S-1/A). The `_NEXT_SECTION["risk_factors"]`
    end-pattern is extended with S-1/424B equivalents (`cautionary statement
    regarding`, `use of proceeds`, `dividend policy`) so the Risk Factors
    section is cleanly bounded in prospectus documents. `filing_form_type`
    (e.g., "424B4") is now returned in the result for agent provenance.

  - **EDGAR agent prompt v1.2:** Updated system prompt explains the two-path
    flow (10-K vs. S-1/prospectus), enforces gaps-not-fabrication for
    xbrl_available=False, and requires `filing_form_type + filing date` in
    every claim's reasoning field.

  - **Live-captured fixtures** for the SpaceX case (`search_spacex_424b4.json`,
    `companyfacts_spacex_empty.json`); capture commands in `CAPTURE_COMMANDS.md`.

  - **Merge-path test** (`test_edgar_spacex_merge_path_succeeded_with_gapped_financials`):
    asserts that edgar_lookup_status=succeeded, CIK tracked, revenue stays
    "unknown" (no override), and 424B4 risk factors land in regulatory_risks at
    primary_document tier.

  - **Live anchors 5-7** in `test_edgar_live.py`: SpaceX resolves via 424B4,
    companyfacts returns xbrl_available=False (not an error), and Risk Factors
    text is extractable and bounded correctly.

- **Credibility caps (Cap 1a, Cap 1b, Cap 2)** — two assembler-level guardrails
  that couple confidence to source quality. All thresholds live in a single
  centralized block (`_HIGH_ELIGIBLE_TIERS`, `_CAP1B_*`, `_MATERIAL_FINANCIAL_FIELDS`,
  `_CAP2_WEAK_TIERS`). Applied in `_apply_credibility_caps()` after all EDGAR merges
  and before section-confidence computation so aggregates always reflect post-cap values.
  These are strict caps: they can only lower confidence/quality, never raise it.

  - **Cap 1a (per-claim confidence ceiling):** Each claim's declared confidence is
    capped to a ceiling derived from the best source tier among its sources (or
    derived-from / synthesized-from parents for derived and synthesis claims).
    `primary_document` / `reputable_secondary` → HIGH allowed;
    `aggregator` / `community` / `unknown` → ceiling = MEDIUM;
    no sources at all → ceiling = LOW.

  - **Cap 1b (report-level data_quality ceiling):** The synthesis agent's
    `data_quality` *value* ("high" / "medium" / "low") is capped by the run's
    unknown-tier share (U) and primary+reputable share (P):
    `high` only if U < 0.20 AND P ≥ 0.50;
    forced `low` if U ≥ 0.40 OR P < 0.25;
    otherwise `medium`.
    On the Apple #3 run (U = 0.479) this correctly forces `data_quality` to `"low"`.

  - **Cap 2 (material financial claims):** For the quantitative financial fields
    (`revenue`, `revenue_growth`, `profitability`, `valuation`, `total_funding`),
    if ALL sources fall within `{community, aggregator, unknown}` (no primary or
    reputable evidence, and not derived from a primary/reputable parent), the claim
    is capped to LOW and `unverified_financial = True` is set on the Claim. On the
    Apple run, `total_funding` (Quora + PitchBook) and `valuation` (market-cap
    aggregators) are the primary targets.

  - **`Claim.unverified_financial: bool = False`** — new field (PATCH; schema
    version bumped to 1.0.6) enabling downstream renderers and consumers to filter
    or annotate flagged financial claims.

- **`apple.com` added to `_PRIMARY_DOCUMENT` tier set** — same pattern as
  `openai.com` / `anthropic.com`; observed in the Apple run where
  `apple.com/newsroom/…` (press-release quarters) was assigned UNKNOWN tier.
  Prevents `revenue_growth` from being incorrectly flagged as unverified.

### Fixed

- **Shared `_is_assembled_empty` predicate** extracts the "empty = None scalar or
  empty list" definition used by both `_prune_gaps` and the invariant guard test into
  one helper, so the two call sites cannot drift apart. Complemented by a flattening
  assertion (`test_unknown_datapoint_is_flattened_to_none`) that locks the
  `_dp_to_claim` guarantee that fully-unknown DataPoints → `None`; if that guarantee
  ever breaks, the prune-after-merge approach would fail silently without it.

- **Gaps list not reconciled after EDGAR merge.** The real Apple run correctly
  populated `financial.revenue = "$416.16B (FY2025)"` and `financial.profitability`
  from EDGAR at `primary_document` tier, while simultaneously listing both as
  information gaps — the report contradicted itself. Root cause: `_collect_gaps`
  ran on the pre-merge financial agent dicts (which had `confidence=unknown` for
  both), and the resulting `gaps` list was never updated after `_merge_edgar` filled
  those fields. Fixed by calling `_prune_gaps()` after all merges in `assemble_report`;
  it removes any gap record whose field is now populated in the assembled sections.
  `revenue_growth` (genuinely unknown, not filled by EDGAR) correctly remains a gap.
  Invariant enforced: no field in `doc.gaps` may have a non-None value anywhere in
  the assembled `ReportDocument`.

### Fixed (EDGAR PR1 — CIK resolution hotfix)

- **EDGAR CIK resolution completely broken in production.** `edgar_find_company`
  was reading three fields from the EFTS `_source` that do not exist in the real
  EFTS v4 API: `entity_name` (real: `display_names` list), `accession_no` (real:
  `adsh`), and a CIK parsed from the accession number (real: `ciks[0]` directly).
  Because `accession_no` was always `""`, `_parse_cik_from_accession("")` always
  returned an empty string, producing `lookup_failed` for every US public company
  including Apple Inc. The `succeeded` path has never appeared in any of the five
  known production outputs (Anthropic, OpenAI, Stripe, SpaceX, Apple) — the EDGAR
  → assembler merge path was production-untested. Deleted `_parse_cik_from_accession`.

- **EFTS returns per-document records, not per-filing records.** A single 10-K
  generates dozens of EFTS hits — one for the primary filing, one per exhibit.
  Without a primary-document filter, sort-by-date could select an exhibit
  (e.g. EX-23.1 with a more recent date) over the actual 10-K. Added a
  `_PRIMARY_FORM_TYPES` filter (`file_type in {"10-K"}`) so disambiguation runs
  over filing documents only. Falls back to all hits if no primary docs are found.

- **Disambiguation now uses real field names.** Sort key updated to extract the
  entity name from `display_names[0]` (format: `"Apple Inc.  (AAPL)  (CIK ...)"`
  → strips to `"Apple Inc."`) for case-insensitive exact-name matching. The
  `name_match` field (`"exact"` or `"partial"`) is now returned in the response
  so callers can assess confidence and flag partial matches.

- **Multi-CIK ambiguity guard.** `edgar_find_company` now detects when multiple
  distinct filers pass `_filer_matches_query` on a partial name match and returns
  `found=False, error="ambiguous"` with the matching CIK list instead of silently
  picking the most-recent hit. Exact matches are unambiguous and bypass the guard.
  Disambiguation follow-up (company_tickers.json + submissions API resolver) tracked
  separately per the original plan; this guard eliminates the silent-wrong-CIK class
  of error for bare common names like "Apple".

- **Assembler EDGAR merge path verified end-to-end on real Apple data** for the
  first time. `test_edgar_apple_end_to_end_merge` walks the full chain:
  `edgar_find_company("Apple Inc.")` → CIK `0000320193` → `edgar_get_financials`
  → `$416.16B FY2025` → `CompanyEdgarFinancials.model_dump()` → `assemble_report`
  → `doc.financial.revenue` at `primary_document` tier from `data.sec.gov`.
  `edgar_data` is now built via `CompanyEdgarFinancials(**parsed).model_dump()`
  (the exact EDGAR agent path) rather than a manually-constructed dict.
  Verifies `run_metadata.edgar_lookup_status=succeeded` and `edgar_cik=0000320193`.

- **Mention-only EFTS hits rejected (filer verification).** A full-text search
  for "Space Exploration Technologies" with `forms=10-K` returns 153 hits where
  the top primary-doc result is Iridium Communications' 10-K (CIK 0001418819),
  because Iridium's filing mentions SpaceX in its text. Without filer verification,
  `edgar_find_company("Space Exploration Technologies")` would resolve to Iridium's
  CIK. Added `_filer_matches_query(entity_name, query)`: bidirectional substring
  check against `display_names` ensures the filing was made BY the queried company,
  not merely about it. Returns `found=False` with an explanatory note when rejected.

- **Revenue fallback chain picks the most-recent annual, not the first key with any data.**
  Apple deprecated `us-gaap.Revenues` after FY2018 and uses
  `us-gaap.RevenueFromContractWithCustomerExcludingAssessedTax` from FY2019 onward.
  The old "first key with data" logic returned Apple's FY2018 revenue ($265.60B from
  `Revenues`) instead of FY2025 ($416.16B from `RevenueFromContractWithCustomer...`).
  The hand-authored `companyfacts_aapl.json` fixture had fabricated FY2024 data under
  `Revenues`, masking this bug. Fixed: chain steps 1–2 now try both keys and pick
  the one with the most recent `end` date; `companyfacts_aapl.json` re-captured from
  live API (real FY2018 data under `Revenues`, real FY2025 data under
  `RevenueFromContractWithCustomerExcludingAssessedTax`).

- **EDGAR test fixtures regenerated from live API responses.** `search_aapl.json`
  and `search_apple_disambiguation.json` replaced with verbatim live captures
  (real `display_names`, `adsh`, `ciks`, `file_type`, `root_forms` — no invented
  fields). Capture commands documented in `tests/fixtures/edgar/CAPTURE_COMMANDS.md`.
  Guard test `test_efts_fixture_schema_uses_real_fields` now fails loudly if
  any EFTS fixture reintroduces invented field names (`entity_name`, `accession_no`,
  `form_type`). This is the second instance of this fixture-masked bug pattern
  (CourtListener `docket_absolute_url` was the first).

### Repo-wide convention (proposed)

Every `tests/fixtures/<tool>/` directory that covers an external API should contain
a `CAPTURE_COMMANDS.md` documenting the exact shell command to regenerate each
fixture from the live API. Any fixture file without a matching command is assumed
hand-authored and suspect. A schema-guard test (assert real fields present, assert
invented fields absent) should be added for each external-API fixture set.
Follow-up: audit `tests/fixtures/opencorporates/`, `tests/fixtures/samgov/`, and
`tests/fixtures/uspto/` for the same invented-schema pattern (out of scope here).

### Fixed

- **CourtListener citation bug:** per-case `source_url` was always `""` because the code
  read `absolute_url` but the CourtListener v4 API returns `docket_absolute_url`.
  Every litigation claim now cites a verifiable docket page URL. A three-level fallback
  ensures a specific URL is always produced: `docket_absolute_url` → `docket/{id}/` →
  search URL with encoded docket number. The bare API search endpoint
  (`/api/rest/v4/search/`) is never cited as a per-case source.

- **CourtListener attribution bug (real-world-harm grade):** the Risk Agent falsely
  attributed conspiracy and money laundering charges to a company that appeared only
  in the case caption as a third party (its hardware was the in rem res — the actual
  charges were against third-party criminal operators), not a named defendant. Root causes:
  (1) the `party` list was stripped from tool output, giving the agent no positive
  signal about the company's absence from the named-party list; (2) the prompt had
  no attribution discipline for in rem / seizure proceedings. Fixes:
  - `courtlistener_search_cases` now passes through `parties` (named parties list) and
    `cause` for each case.
  - `RiskAgent` system prompt (PROMPT_VERSION "2.2") adds a four-step determination
    order: party-membership check → case-name pattern recognition → docket-number prefix
    as a hint → conservative default when party data is absent.
  - Non-party references (in rem, forfeiture, third-party) are routed to
    `reputational_risks`, not `pending_litigation`. Only named defendants/plaintiffs go
    into `pending_litigation`.
  - Severity for non-party cases is capped at LOW regardless of statutes cited in the
    property description.
  - Each CourtListener-sourced DataPoint must record named parties and company role in
    `reasoning`.

### Changed

- `RiskAgent.PROMPT_VERSION` bumped to `"2.2"`.
- CourtListener tool result now includes `parties` (list of named parties) and `cause`
  (cause of action) per case.
- Test fixtures `cases_stripe.json` regenerated from real CourtListener v4 API
  responses (real `docket_absolute_url`, real `party` arrays); the hand-authored
  fixture used the wrong field name (`absolute_url`) and omitted party data.
- New fixture `cases_thirdparty_seizure.json` captured verbatim from live API: contains
  the in-rem seizure regression case (company is only a third party) and the
  true-defendant case (company is the named defendant).

### Notes

- The same name-in-caption misattribution pattern exists in other sources (news
  headlines, SAM.gov entity names). Out of scope for this fix; tracked as a follow-up.
- The 755-case hit count for the company → "systemic legal challenges" overreach is a
  sibling defect (raw hit-count read as liability magnitude); tracked as a follow-up.
- Structured `party_role` field on a `LitigationDataPoint` subtype is the right
  long-term schema design; deferred to a post-P4 MINOR-bump pass. For now, party role
  is captured in the `reasoning` field.

---

## [0.7.3] — 2026-05-22

**`_infer_tier()` out-of-distribution verification: Anthropic run confirmed generalization; second expansion pass eliminates remaining unknown coverage.**

### Added

- **13 new domains** from Anthropic run (first target not in the seed set). No logic changes — data only.

  **PRIMARY_DOCUMENT (2 domains):**
  - `anthropic.com` — official company primary domain; covers `alignment.anthropic.com` and all
    other Anthropic subdomains via endswith rule
  - `claude.com` — Claude product domain; authoritative for feature announcements and model docs

  **REPUTABLE_SECONDARY (3 domains):**
  - `cnn.com` — CNN, major US broadcast/cable news network; notable gap from seed targets (no
    SpaceX/OpenAI runs surface CNN at high rate; Anthropic's public AI-safety coverage does)
  - `ig.com` — IG Group, established UK financial services and market commentary
  - `investing.com` — Investing.com, global financial data and news with editorial standards

  **AGGREGATOR (8 domains):**
  - `dexteragent.ai`, `favikon.com`, `getpanto.ai`, `intuitionlabs.ai`, `makerstations.io`,
    `metacto.com` — AI company profile and analytics aggregators
  - `europeanbusinessmagazine.com` — aggregator-tier business profile publisher

- **13 new parameterized test cases** in `tests/test_assembler.py` covering every new domain entry.
  Total tier test cases: 103. Total test suite: 453 passing.

### Measured impact (Anthropic run; applied to URLs from real run JSON)

| Target    | Unknown before | Unknown after | Goal      |
|-----------|---------------|---------------|-----------|
| Anthropic | 29% (29/99)   | 0% (0/99)     | <20%\*    |
| OpenAI    | 0% (0/41)     | 0% (0/41)     | <15%      |
| SpaceX    | 0% (0/42)     | 0% (0/42)     | <15%      |

\* Live run was 29.3% — over the 20% target but under the 30% acceptable ceiling. Triggered the
second expansion pass. After adding 13 domains, all three targets at 0%.

### Generalization verdict

Anthropic was the first out-of-distribution target (not used in v0.7.2 seed pass). The 29.3%
live-run figure is consistent with the four-category model established in v0.7.2: ~2 own-domain
URLs (`anthropic.com`, `claude.com`), ~8 AI-adjacent aggregators, ~2 missing mainstream outlets
(CNN), and ~1 vertical press entry. The table is not overfit to SpaceX/OpenAI. Acceptance bar
passes: under 30% on first run; 0% after one expansion pass. Detailed analysis in
`docs/TIER_INFERENCE_NOTES.md` (v0.7.3 row added).

### Notes

- **Planned (not built):** At assembly time, auto-classify the target company's primary domain as
  `primary_document` using the classifier output or `website` field. Eliminates category 1
  (own-domain unknowns) for all future first runs without manual additions. Tracked as a small
  post-P4 item.

---

## [0.7.2] — 2026-05-22

**`_infer_tier()` domain expansion: out-of-distribution targets now show <5% unknown coverage.**

### Added

- **28 new domains** to `_infer_tier()` domain pattern tables, classified from unknown URLs
  observed in real OpenAI and SpaceX runs. No logic changes — data only.

  **PRIMARY_DOCUMENT (1 domain, covers 3 subdomains):**
  - `openai.com` — official company primary domain; authoritative for announcements, pricing,
    developer docs, status pages. Subdomain rule covers `developers.openai.com`,
    `help.openai.com`, `status.openai.com`.

  **REPUTABLE_SECONDARY (11 domains):**
  - `space.com` — major science/space journalism
  - `cmcmarkets.com` — CMC Markets, established financial trading and news
  - `grellas.com` — Grellas Shah LLP, Silicon Valley tech law firm publications
  - `wsgr.com` — Wilson Sonsini, leading tech/startup law firm insights
  - `pymnts.com` — PYMNTS.com, payments industry journalism
  - `saastr.com` — SaaStr, SaaS business publication with original analysis
  - `thenewstack.io` — The New Stack, cloud-native/developer journalism
  - `techi.com` — tech news outlet
  - `expressnews.com` — San Antonio Express-News (Gannett)
  - `floridatoday.com` — Florida Today (Gannett)
  - `valleycentral.com` — Valley Central, Rio Grande Valley TV news

  **AGGREGATOR (11 domains):**
  - `crescendo.ai`, `enterprise-ai.io`, `highperformr.ai` — AI content/analytics aggregators
  - `releasebot.io` — software release notes tracker
  - `tweetstorm.ai` — AI-curated social media content
  - `amperly.com` — social media account aggregator
  - `jobsbyculture.com` — company/job data aggregator
  - `salestools.io` — B2B sales data aggregator
  - `contrary.com` — VC research compilation (covers `research.contrary.com`)
  - `stockpil.com` — financial news aggregator
  - `newsbytesapp.com` — news summarization app

  **COMMUNITY (3 domains):**
  - `fandom.com` — fan wikis (covers `*.fandom.com` including `starship-spacex.fandom.com`)
  - `chatgptdisaster.com` — AI complaint/controversy commentary
  - `openrealnews.com` — user-generated complaints aggregator

- **31 new parameterized test cases** in `tests/test_assembler.py` covering every new domain
  entry and its subdomain variants (6 primary, 11 reputable_secondary, 11 aggregator, 3 community).

### Measured impact (applied to URLs from most recent real runs)

| Target  | Unknown before | Unknown after | Goal |
|---------|---------------|---------------|------|
| OpenAI  | 59% (24/41)   | 0% (0/41)     | <20% |
| SpaceX  | 19% (8/42)    | 0% (0/42)     | <15% |
| Stripe  | 50% (2/4)\*   | 50% (2/4)\*   | <20% |

\* Stripe's 2 unknown URLs are `example.com` — hallucinated URLs from a bad agent run, not a
domain coverage gap. Real URL coverage for Stripe is 100%.

Note: figures are from re-applying the updated `_infer_tier()` to URLs in existing report JSONs.
Fresh API-based runs required for live verification (awaiting credentials).

### Domain categories in the unknown bucket (PR summary)

The unknown bucket across OpenAI and SpaceX runs split into four identifiable categories:
(1) **The target company's own domain** — `openai.com` and its subdomains (help, developer docs,
status) accounted for 3 of 24 OpenAI unknowns. Official primary domains of the research target
are consistently missed because the domain table doesn't anticipate the specific company. Added
`openai.com` now; others will be added as they appear.
(2) **AI-adjacent niche aggregators** — `highperformr.ai`, `crescendo.ai`, `releasebot.io`,
`tweetstorm.ai`, `enterprise-ai.io`, `amperly.com` (6 domains) are content aggregators and
analytics tools that cluster around AI topics. Consistent aggregator material: low editorial
standards, compile from other sources.
(3) **B2B/SaaS vertical press** — `saastr.com`, `pymnts.com`, `thenewstack.io` have genuine
editorial standards and original journalism for their verticals (SaaS, payments, cloud-native).
Correctly classified as reputable_secondary. Law firm publications (`grellas.com`, `wsgr.com`)
are the same pattern — high editorial quality, niche audience.
(4) **Regional and local media** — `expressnews.com`, `floridatoday.com`, `valleycentral.com`
are Gannett-owned or local TV stations that appeared because SpaceX's Texas operations are
covered by local Texas media. These are reputable_secondary despite smaller reach.

---

## [0.7.1] — 2026-05-22

**Section confidence moved from render time to assembly time; `derived_from` now uses field-path resolution.**

### Added

- `compute_section_confidences(doc)` in `src/synthesis/assembler.py`. Computes per-section
  confidence percentage (0.0–100.0) using HIGH=1.0 / MEDIUM=0.66 / LOW=0.33 / UNKNOWN=0.0
  averaged over all Claims. Called by `assemble_report()` and persisted in
  `run_metadata.section_confidences`. Renderers read from there — no recomputation at render time.
- `compute_overall_confidence(section_confidences)` in `src/synthesis/assembler.py`. Weighted
  average: Financial 40%, Risk 40%, Social Media 20% (renormalized when sections are absent).
  Persisted in `run_metadata.overall_confidence`.
- `run_metadata.section_confidences: Dict[str, float]` and `run_metadata.overall_confidence:
  Optional[float]` added to `RunMetadata` schema. Both are populated by every assembled document.
  P4 calibration reads section-level confidence from here without recomputing from individual claims.
- `_build_field_map(data)` in `src/synthesis/assembler.py`. Maps agent field paths (e.g. "revenue",
  "recent_financial_events[0]") to `_claim_id` values in annotated agent output. Enables
  field-path resolution in `derived_from`: agents cite field names instead of self-assigned IDs,
  resolving the LLM look-ahead problem from v0.7.0.
- `_validate_derived_from` restored to a hard `ValueError` (was downgraded to warning in v0.7.0).
  Now symmetric with `_validate_synthesized_from`. Dangling field-path references raise at assembly
  time, not silently.
- 9 new tests in `tests/test_assembler.py`: `compute_section_confidences` unit (known values, all-high,
  excludes-None-sections), `compute_overall_confidence` (weighted, two-section, no-weighted-sections),
  integration (`assemble_report` populates fields), consistency (stored values match renderer formula),
  `test_derived_from_field_path_resolution`, `test_derived_from_dangling_raises`.
- 2 new contract tests in `tests/test_canonical_json_contract.py` verifying `section_confidences`
  and `overall_confidence` are populated in every report.

### Changed

- SCHEMA_VERSION: `1.0.4` → `1.0.5` (two additive fields with defaults = patch).
- `schema/report.schema.json` regenerated.
- Financial agent PROMPT_VERSION: `1.3` → `1.4`. `derived_from` now uses field paths
  (`"revenue"`, `"recent_financial_events[0]"`) instead of self-assigned `_claim_id` labels.
  COMPUTATION DISCIPLINE updated accordingly; `_claim_id` fields removed from OUTPUT_SCHEMA.
- All four section assemblers (`_assemble_research`, `_assemble_financial`, `_assemble_risk`,
  `_assemble_social_media`) now call `annotate_claim_ids` internally (idempotent) and build a
  `field_map` passed to `_dp_to_claim` for field-path resolution.
- Markdown and PDF/HTML renderers updated to read `run_metadata.section_confidences` /
  `overall_confidence` instead of recomputing. Fallback to on-the-fly computation (with a
  `warnings.warn`) is present for documents assembled before this version.

### Fixed

- Section-level confidence percentages were computed at render time and never persisted, breaking
  the canonical JSON as source-of-truth invariant. The numbers in the markdown header now match
  exactly what is in the JSON.
- `_validate_derived_from` downgrade from v0.7.0 reverted. The root cause (LLM look-ahead
  required for self-assigned `_claim_id`) is eliminated by field-path resolution; hard validation
  is now structurally sound.

---

## [0.7.0] — 2026-05-20

**Derivation tracking: computed values are now explicitly flagged and traceable to their atomic inputs.**

### Added

- `derived: bool = False` and `derived_from: List[str]` fields on `DataPoint` (agent output layer)
  and `Claim` (canonical layer). `derived=True` marks agent-computed values (percentages, growth
  rates, margins, ratios); `derived_from` lists the `claim_id`s of the atomic input claims used.
  Symmetric with the `synthesized_from` provenance chain from v0.6.0.
- Pydantic `check_derived_consistency` validator on `Claim`: `derived=True` requires non-empty
  `derived_from`; `derived=False` with non-empty `derived_from` raises. Enforces the invariant
  at construction time.
- `_collect_all_claim_ids(doc)` and `_validate_derived_from(doc)` in `src/synthesis/assembler.py`.
  Assembly-level validation ensures every `derived_from` entry resolves to a real Claim in the
  document. Called at end of `assemble_report()` alongside `_validate_synthesized_from`.
- `claim_as_dp()` in the assembler now appends `*(derived)*` to the value string of derived claims,
  so all markdown/HTML renders visually distinguish computed values from source-grounded ones.
- COMPUTATION DISCIPLINE block added to system prompts of all five agents (research, financial, risk,
  social_media, edgar). Explains when to set `derived=True`, how to self-assign `_claim_id` on atomic
  input claims, and the revenue-growth pattern (emit prior-year revenue in `recent_financial_events`,
  current revenue in `revenue`, then cite both IDs in `revenue_growth.derived_from`).
- `derived` and `derived_from` added to all agent `OUTPUT_SCHEMA` templates.
- 9 new tests in `tests/test_assembler.py`: validator invariants, passthrough via assembler,
  valid/dangling `derived_from` references, `claim_as_dp` marking.

### Changed

- SCHEMA_VERSION: `1.0.3` → `1.0.4` (two additive optional fields with defaults = patch).
- `schema/report.schema.json` regenerated.
- PROMPT_VERSIONs bumped: Research `2.0→2.1`, Financial `1.1→1.2`, Risk `2.0→2.1`,
  Social Media `1.0→1.1`, EDGAR `1.0→1.1`.

### Design notes

Option A (explicit `derived` flag + `derived_from`) was chosen over Option B (forbid computation
everywhere) because computed values (growth rates, margins, ratios) are genuinely useful and
downstream consumers should not have to reinvent them. Option B would have pushed the computation
outside the pipeline in an inconsistent way. The `derived`/`derived_from` pattern is symmetric with
`synthesized_from`, giving a unified provenance model: `sources` for direct retrieval, `derived_from`
for agent-layer transformations, `synthesized_from` for cross-agent synthesis. P4 calibration can
now distinguish source-grounded failures (wrong source) from derivation failures (wrong arithmetic).

### Known limitation

The harder gap — an agent computing a number and setting `derived=False` (or omitting the flag
entirely) — is not detectable by static validation. The validator can only enforce `derived=True ↔
derived_from non-empty`; it cannot detect that a value string contains agent-computed arithmetic
that was mislabeled as source-grounded. This will surface in P4 calibration. No structural defense
added now — wait for P4 data to show how common the failure mode is before adding complexity.

Note: `_validate_derived_from` was initially downgraded to a warning (v0.7.0 shipped) due to the
LLM look-ahead problem. Field-path resolution (v0.7.1) eliminates the root cause; hard validation
was restored.

---

## [0.6.0] — 2026-05-20

**Synthesis provenance chain: every synthesis claim is now traceable to specific upstream claims.**

### Added

- `synthesized_from: List[str]` field on `DataPoint` (agent output layer) and
  `Claim` (canonical layer). Empty for all upstream agent claims; non-empty for
  synthesis claims. Each entry is a `claim_id` of an upstream Claim that the
  synthesis claim draws from.
- `annotate_claim_ids(data: dict) -> dict` in `src/synthesis/assembler.py`.
  Deep-copies an agent output dict and injects a `_claim_id` (12-char hex)
  onto every DataPoint-shaped dict. Called on all Phase 1 outputs before synthesis
  runs, so the synthesis agent can cite stable IDs in `synthesized_from`.
  The assembler honours pre-assigned `_claim_id` values instead of generating new
  ones, keeping the chain stable end-to-end.
- 12 new tests in `tests/test_assembler.py`: `annotate_claim_ids` behaviour,
  `synthesized_from` pass-through through assembly, synthesis provenance invariant
  (every non-meta synthesis claim has non-empty `synthesized_from`), and
  verification that cited claim_ids are real upstream claims.

### Changed

- `src/agents/synthesis.py` — `PROMPT_VERSION` bumped `"2.0"` → `"2.1"`.
  Added `PROVENANCE` discipline block to system prompt requiring non-empty
  `synthesized_from` on every synthesis claim (except `data_quality`). Updated
  output schema to show `synthesized_from` field in every DataPoint. Added
  provenance check to the pre-output task reminder.
- `src/main.py` — Phase 1 outputs are now passed through `annotate_claim_ids()`
  before the synthesis task is built. Synthesis receives upstream data with
  `_claim_id` fields visible; assembled report uses those same IDs.
- `SCHEMA_VERSION` bumped `"1.0.2"` → `"1.0.3"` (PATCH — additive fields with
  `default_factory=list`).
- `schema/report.schema.json` regenerated.

---

## [0.5.0] — 2026-05-20

**Numbers discipline in synthesis agent + expanded tier coverage.**

### Changed

- `src/agents/synthesis.py` — `PROMPT_VERSION` bumped `"1.0"` → `"2.0"`. Added a
  `NUMBERS DISCIPLINE` block to the system prompt and a pre-output verification
  reminder to the task prompt. The synthesis agent is now prohibited from computing,
  estimating, averaging, rounding, or deriving any number not present verbatim in an
  upstream agent DataPoint. Conflicting numbers must be routed to `data_conflicts`
  rather than resolved in prose. Fabricated ranges (e.g., "4,000–8,000 employees")
  and computed percentages (e.g., "41% growth") are explicitly forbidden unless the
  exact string appears in upstream output. Qualitative phrasing is preferred over
  invented quantities.

- `src/synthesis/assembler.py` — `_infer_tier()` domain sets significantly expanded.
  `_PRIMARY_DOCUMENT` gains 20+ .gov domains. `_REPUTABLE_SECONDARY` gains 30+ domains
  (Wikipedia, Britannica, BBC, NPR, spacenews.com, darkreading.com, fool.com, etc.).
  `_AGGREGATOR` gains 25+ domains (sacra.com, tsginvest.com, spacexstock.com, pestel-
  analysis.com, zoominfo.com, etc.). `_COMMUNITY` gains 15+ domains (GitHub, Medium,
  Substack, YouTube, Trustpilot, etc.). Added a generic `*.gov` fallback rule: any
  domain ending in `.gov` not matched by a registry entry or explicit set returns
  `PRIMARY_DOCUMENT` — the `.gov` TLD is restricted to verified US government entities.
  Unknown tier coverage drops from ~68% to ~16% on the observed 414-URL corpus.

### Fixed

- `src/sources/samgov.py` — Removed invalid `purposeOfRegistrationCode=Z2` query
  parameter that caused HTTP 400 errors. Changed search parameter from `entityName`
  (invalid in API v3) to `legalBusinessName` (correct). Added `dbaName` fallback:
  if `legalBusinessName` returns no results, retries with `dbaName` to handle
  trade-name searches (e.g., "SpaceX" → "SPACE EXPLORATION TECHNOLOGIES CORP").
  Error responses now include `detail` field with first 300 chars of response body
  for easier debugging.

### Added

- `scripts/verify_samgov.py` — standalone SAM.gov connectivity check; accepts a
  company name argument; tests graceful degradation (no key) then live lookup.
- `scripts/verify_uspto.py` — standalone USPTO PatentsView connectivity check;
  accepts a company name argument; no API key required.

---

## [0.4.0] — 2026-05-18

**Priority 3b: Four remaining Tier 0 source tools on existing agents.** Research and
Risk agents now call OpenCorporates (US entity registry), USPTO PatentsView, SAM.gov
(federal contractor registry), and CourtListener (PACER dockets) as first-priority tool
calls before falling back to web search. Schema gains four new typed fields.

### Added

- `src/sources/opencorporates.py` — `opencorporates_search_company(name, cache)`:
  queries OpenCorporates v0.4 search API; filters server-side to US jurisdictions
  (`jurisdiction_code.startswith("us_")`); returns incorporation_date, registered_address,
  jurisdiction_code, company_number, company_status, opencorporates_url; fails loud
  when OPENCORPORATES_API_KEY is absent (returns `{disabled: true}` so agent logs
  and falls back to web search); cached with 30-day TTL.
- `src/sources/uspto.py` — `uspto_search_patents(company_name, cache)`: queries
  USPTO PatentsView API v2; searches by assignee_organization; returns patent_count
  and up to 10 recent patents with patent_id, title, grant date; no API key required;
  caches forever (granted patents are immutable).
- `src/sources/samgov.py` — `samgov_search_contracts(company_name, cache)`:
  queries SAM.gov Entity Management API v3 for active federal contractor registrations;
  returns UEI, CAGE code, registration_status, entity_type, NAICS codes; degrades
  gracefully when SAM_GOV_API_KEY is absent (returns `{no_api_key: true}`); 24h TTL.
- `src/sources/courtlistener.py` — `courtlistener_search_cases(company_name, cache)`:
  queries CourtListener REST API v4 for dockets; returns case_name, court_id,
  date_filed, docket_number, absolute_url per case; works unauthenticated; sends
  `Authorization: Token` header when COURTLISTENER_API_KEY is set; handles 429
  rate-limit responses; 24h TTL.
- `src/schemas/models.py` — four new PATCH-level fields:
  - `CompanyResearch.patent_count: Optional[DataPoint] = None`
  - `CompanyResearch.notable_patents: List[DataPoint] = Field(default_factory=list)`
  - `CompanyRisks.government_contract_exposure: Optional[DataPoint] = None`
  - `CompanyRisks.notable_federal_contracts: List[DataPoint] = Field(default_factory=list)`
  - Mirror fields in `ReportResearch` and `ReportRisk` (canonical layer).
- `tests/fixtures/opencorporates/` — `search_stripe.json` (single US match),
  `search_multi_match.json` (mixed US/non-US — tests US filter and exact-name
  preference), `search_no_us_results.json` (non-US only — tests empty-result path).
- `tests/fixtures/uspto/` — `patents_qualcomm.json` (54,821 patents, 3 returned),
  `patents_stripe.json` (zero patents — tests not-found path).
- `tests/fixtures/samgov/` — `entity_leidos.json` (active registration with UEI,
  CAGE, NAICS codes), `entity_stripe.json` (zero records — tests not-found path).
- `tests/fixtures/courtlistener/` — `cases_stripe.json` (2 dockets with full fields),
  `cases_empty.json` (zero results — tests not-found path).
- `tests/test_opencorporates.py` — 5 tests: no-key disables, found US company,
  non-US filter, multi-match exact-name preference, cache hit skips HTTP.
- `tests/test_uspto.py` — 4 tests: found patents (Qualcomm count + list), zero
  patents (Stripe not-found), cache hit, HTTP error returns error JSON.
- `tests/test_samgov.py` — 4 tests: no-key degrades gracefully, active contractor
  (Leidos UEI/CAGE/NAICS), not-found (Stripe), cache hit.
- `tests/test_courtlistener.py` — 6 tests: found cases (fields verified), empty,
  unauthenticated (no Authorization header), authenticated (Token header), 429
  rate-limit, cache hit.

### Changed

- `src/agents/research.py` — PROMPT_VERSION "1.0" → "2.0". Overrides `get_tools()`
  to expose 4 tools: `opencorporates_search`, `uspto_patent_search`, `web_search`,
  `web_fetch`. Overrides `handle_tool_call()` to route to new source modules.
  Updated prompt: Tier 0 priority (call OC first, then USPTO, then web search);
  OC data overrides web search for founded_year and headquarters; USPTO authoritative
  for patent_count; tool budget raised 4-5 → 6.
  OUTPUT_SCHEMA extended with `patent_count` and `notable_patents` fields.
- `src/agents/risk.py` — PROMPT_VERSION "1.0" → "2.0". Overrides `get_tools()`
  to expose 4 tools: `samgov_contract_search`, `courtlistener_case_search`,
  `web_search`, `web_fetch`. Updated prompt: SAM.gov called first for
  government_contract_exposure; CourtListener called second for pending_litigation
  dockets; tool budget raised 4-5 → 6; government_contract_exposure required in
  all responses (affirmative or negative). OUTPUT_SCHEMA extended with
  `government_contract_exposure` and `notable_federal_contracts`.
- `src/agents/classifier.py` — adds `is_government_contractor: bool` to output
  schema: true for defense, IT services, consulting, infrastructure companies with
  known/likely federal business. `max_tokens` raised 256 → 384.
- `src/agents/base.py` — `_format_context()` now renders `is_government_contractor`
  so all agents see it in their system prompts.
- `src/synthesis/assembler.py`:
  - `_assemble_research()` adds `patent_count` and `notable_patents` fields.
  - `_assemble_risk()` adds `government_contract_exposure` and `notable_federal_contracts`.
  - `build_render_dicts()` includes all four new fields in the reconstructed dicts.
- `src/synthesis/report_generator.py`:
  - Research section: renders `patent_count` scalar and `notable_patents` list table
    after Recent Developments.
  - Risk section: renders `government_contract_exposure` scalar and
    `notable_federal_contracts` list table after Pending Litigation.
- `tests/fixtures/sample_report.json` — updated: schema_version `"1.0.2"`;
  research section gains `"patent_count": null, "notable_patents": []`.
- `tests/test_report_schema.py` — asserts `schema_version == "1.0.2"`.
- `schema/report.schema.json` — regenerated from updated Pydantic models.

### Schema

- `SCHEMA_VERSION`: `"1.0.1"` → `"1.0.2"` (PATCH — four additive fields all have
  `None` or `default_factory=list` defaults; fully backward compatible)

---

## [0.3.0] — 2026-05-18

**Priority 3a: Tiered source registry and EDGAR primary-source agent.** The
pipeline now retrieves audited financials and SEC-disclosed risk factors directly
from EDGAR 10-K filings for US public companies, and classifies all source URLs
via an authoritative registry rather than hardcoded domain sets.

### Added

- `src/sources/registry.py` — `REGISTRY: dict[str, SourceEntry]` with five
  authoritative sources: `sec_edgar` (PRIMARY_DOCUMENT), `opencorporates_us`
  (REPUTABLE_SECONDARY), `uspto` (PRIMARY_DOCUMENT), `sam_gov`
  (PRIMARY_DOCUMENT), `courtlistener` (PRIMARY_DOCUMENT). Each entry carries
  tier, base_url, freshness_days, rate_limit_rps, requires_api_key, env_key,
  us_only, and description. `get_source()` and `sources_by_tier()` helpers.
- `src/sources/cache.py` — `SourceCache`: SQLite-backed TTL cache for Tier 0
  API calls. Table `source_cache` lives in `outputs/agent_log.db`. Cache key =
  sha256(source_id + ":" + canonical JSON of params). `ttl_seconds=None` caches
  forever (immutable per-accession EDGAR filings); `ttl_seconds=86400` for
  companyfacts (24h). Hit count tracked per entry.
- `src/sources/edgar.py` — EDGAR tool functions for EdgarAgent:
  - `edgar_find_company(name, cache)` — EFTS search for CIK; disambiguation by
    (exact_case_match DESC, file_date DESC); CIK parsed from accession_no
  - `edgar_get_financials(cik, cache)` — companyfacts API (24h cache); revenue
    fallback chain: `us-gaap.Revenues` → `RevenueFromContractWithCustomer...`
    → `InterestAndDividendIncomeOperating + NoninterestIncome` (bank fallback);
    `revenue_keys_attempted` always included for diagnostics
  - `edgar_get_filing_text(cik, accession_no, section, cache)` — fetches filing
    index, locates primary .htm doc, strips HTML, extracts section by regex;
    sections: `risk_factors` (Item 1A), `business` (Item 1), `mda` (Item 7);
    cached forever (immutable)
  - Global async rate limiter: `asyncio.Lock` + 100ms minimum interval per SEC
    10 req/sec policy; all EDGAR calls share the lock
- `src/agents/edgar.py` — `EdgarAgent(BaseAgent)`: Phase 1 parallel agent for
  US public company SEC filings. MAX_TURNS=5; 3-step deterministic workflow
  (find → financials → risk factors); returns `CompanyEdgarFinancials`;
  `edgar_lookup_status` distinguishes succeeded / not_sec_reporting (expected
  for private/non-US companies) / lookup_failed / rate_limited.
- `src/schemas/models.py` additions:
  - `EdgarLookupStatus` enum: SUCCEEDED | NOT_SEC_REPORTING | LOOKUP_FAILED |
    RATE_LIMITED
  - `CompanyEdgarFinancials` Pydantic model: cik, is_sec_reporting,
    edgar_lookup_status, revenue, profitability, fiscal_year_end,
    most_recent_filing, sec_risk_factors
  - `RunMetadata` new optional fields: `tier_coverage: Dict[str, float]`,
    `tier_attempts: Dict[str, int]`, `edgar_lookup_status: Optional[str]`,
    `edgar_cik: Optional[str]`
- `tests/fixtures/edgar/` — four fixture files for offline testing:
  - `search_aapl.json` — single EFTS hit for Apple Inc. (CIK 0000320193)
  - `search_apple_disambiguation.json` — two hits testing the disambiguation
    heuristic (Apple Inc. vs Apple Bank for Savings)
  - `companyfacts_aapl.json` — Apple XBRL facts (Revenues + NetIncomeLoss)
  - `companyfacts_jpm.json` — JPMorgan XBRL facts (no Revenues key; has
    InterestAndDividendIncomeOperating + NoninterestIncome — tests bank fallback
    chain)
- `tests/test_source_cache.py` — 8 tests: miss, put/get, TTL expiry, no-TTL
  (cache forever), hit_count increment, key stability across param ordering,
  clear all, clear by source_id
- `tests/test_edgar_tools.py` — 6 tests: `edgar_find_company` with AAPL
  fixture, disambiguation (Apple Inc. wins over Apple Bank), not-found case;
  `edgar_get_financials` AAPL (asserts `revenue_key_used == "us-gaap.Revenues"`
  and correct value); `edgar_get_financials` JPM (asserts full fallback chain
  traversal with explicit key assertion and combined value); most-recent-annual
  selection (2024 over 2023)

### Changed

- `src/synthesis/assembler.py`:
  - `_infer_tier(url)` now consults `REGISTRY` first (authoritative for known
    sources), then falls back to hardcoded domain sets for general web results.
    OpenCorporates now correctly classifies as REPUTABLE_SECONDARY (it had no
    prior hardcoded entry). All `*.sec.gov` subdomains match via the
    `endswith("." + reg_domain)` check against `base_url="https://www.sec.gov"`.
  - `assemble_report()` signature adds `edgar_data: Optional[dict] = None`.
    When present and `edgar_lookup_status == "succeeded"`, calls `_merge_edgar()`
    and `_merge_edgar_into_risk()` before computing tier coverage.
  - `_merge_edgar(financial, edgar_data)` — overlays EDGAR revenue and
    profitability onto the financial section; EDGAR values take precedence;
    all other financial fields (investors, funding, valuation) are preserved.
  - `_merge_edgar_into_risk(risk, edgar_data)` — prepends `sec_risk_factors`
    DataPoints (sourced from 10-K text) to `regulatory_risks`; EDGAR claims are
    prepended so they appear before web-search-derived claims.
  - `_compute_tier_coverage(*sections)` — walks all Claims via
    `_iter_section_claims()`, returns `(tier_coverage: Dict[str, float],
    tier_attempts: Dict[str, int])`. Called after EDGAR merge so counts include
    EDGAR-sourced PRIMARY_DOCUMENT URLs.
  - `_build_run_metadata()` accepts four new keyword arguments:
    `tier_coverage`, `tier_attempts`, `edgar_lookup_status`, `edgar_cik`.
- `src/agents/classifier.py` — adds `is_likely_public: bool` to output schema;
  true for US-listed SEC-reporting companies, false for private, non-US, or
  subsidiary entities.
- `src/agents/base.py` — `_format_context()` renders `is_likely_public` from
  company_context so all agents see it in their system prompts.
- `src/agents/financial.py` — EDGAR deferral: when `is_likely_public=True` and
  `primary_region == "United States"`, skips revenue/profitability search and
  returns those fields as `value="unknown", confidence="unknown"` — explicitly
  noted as expected, not a gap. Tool budget reallocated to investors, funding,
  business model, key customers, and financial news.
- `src/main.py` — adds `EdgarAgent` to Phase 1 `asyncio.gather`; instantiates
  `SourceCache` shared across the pipeline; passes `edgar_data` to
  `assemble_report()`.
- `src/synthesis/report_generator.py` — methodology footer now includes:
  - **Source Tier Coverage** row: `Primary (Tier 0): X% · Reputable (Tier 1):
    Y% · ...` computed from `run_metadata.tier_coverage`
  - **EDGAR status** row with visual distinction: `✓ succeeded (CIK: ...)` /
    `– not SEC-reporting (expected)` / `⚠ lookup failed` / `⚠ rate limited`
- `tests/test_assembler.py` — 20 new tests: registry-first `_infer_tier`
  (opencorporates, data.sec.gov, efts.sec.gov, search.patentsview.org),
  `_merge_edgar` (revenue overlay, profitability overlay, preserves other
  fields, no-op for not_sec_reporting, no-op for None/empty), `_merge_edgar_into_risk`
  (prepends SEC factors, PRIMARY_DOCUMENT tier, no-op for not_sec_reporting),
  tier_coverage computed and stored in run_metadata, tier_attempts counts,
  coverage sums to 1.0, edgar_status in run_metadata.
- `tests/fixtures/sample_report.json` — updated: schema_version `"1.0.1"`,
  run_metadata gains `tier_coverage: {}`, `tier_attempts: {}`,
  `edgar_lookup_status: null`, `edgar_cik: null`.
- `schema/report.schema.json` — regenerated from updated Pydantic models.

### Fixed

- `src/synthesis/assembler.py`, `src/synthesis/report_generator.py` — replaced
  deprecated `section.model_fields` (Pydantic V2.11 instance-level access) with
  `type(section).model_fields` (class-level access); eliminates 42 deprecation
  warnings from the test suite.

### Schema

- `SCHEMA_VERSION`: `"1.0.0"` → `"1.0.1"` (PATCH — four additive
  `RunMetadata` fields all have defaults)
- Policy clarification: PATCH for additive fields with `default_factory` or
  `None` default (non-breaking); MINOR for additive fields without defaults

---

## [0.2.0] — 2026-05-17

**Priority 1: Canonical JSON as the source of truth.** Every run now produces a
`ReportDocument` written to `outputs/report_{slug}.json`. Markdown, PDF, and
HTML are rendered from that single document — never from raw agent dicts
independently.

### Added

- `src/synthesis/assembler.py` — `assemble_report()` converts raw agent dicts
  into a canonical, Pydantic-validated `ReportDocument`; `_infer_tier()`
  classifies source URLs into five tiers at assembly time (no agent prompt
  changes needed); `build_render_dicts()` reconstructs legacy dict shapes for
  shim renderers
- `schema/report.schema.json` — JSON Schema generated from
  `ReportDocument.model_json_schema()`; kept in sync by a drift test
- `src/schemas/models.py` — canonical report layer: `SourceTier` enum,
  `SourceRef` (url + tier + retrieved_at), `Claim` (extends DataPoint with
  `claim_id`, `field_name`, `agent`, `List[SourceRef]`), `GapRecord`,
  `AgentRunMetadata`, `RunMetadata`, `ReportResearch`, `ReportFinancial`,
  `ReportRisk`, `ReportSocialMedia`, `ReportSynthesis`, `ReportDocument`;
  `SCHEMA_VERSION = "1.0.0"` with documented versioning policy
- `tests/fixtures/sample_report.json` — checked-in Stripe `ReportDocument`
  fixture for contract tests
- `tests/test_assembler.py` — 20 tests: `_infer_tier` (parametrized across all
  five tiers), `assemble_report` (sources become `SourceRef`, tier inference,
  gap detection, claim fields, build_render_dicts roundtrip), smoke perf test
  (assembler must complete in <100ms)
- `tests/test_report_schema.py` — schema drift test (properties + `$defs` in
  the file must match the live Pydantic model); `ReportDocument` round-trip
  JSON serialization test
- `tests/test_canonical_json_contract.py` — 25 contract tests against
  `tests/fixtures/sample_report.json`
- `--json-only` CLI flag — skips PDF/HTML rendering; JSON and markdown still
  written
- `report_json_path` column on the `runs` table in `outputs/agent_log.db`;
  `tracer.persist()` now accepts and stores this path; existing DBs are
  migrated automatically via `ALTER TABLE`

### Changed

- `src/synthesis/report_generator.py` — `render_report_from_doc(doc)` is the
  new primary entry point; accepts a `ReportDocument` and renders markdown from
  it
- `src/synthesis/pdf_report.py` — `render_pdf_report_from_doc(doc)` is the new
  primary entry point; accepts a `ReportDocument` and renders PDF + HTML from it
- `src/main.py` — pipeline now calls `assemble_report()` after Phase 2, writes
  canonical JSON before rendering, passes `ReportDocument` to both renderers,
  and stores `report_json_path` in the tracer

### Deprecated

- `render_report(research_data, ..., trace_summary)` in `report_generator.py` —
  shim; calls `assemble_report()` then delegates to `render_report_from_doc()`.
  Marked `TODO(P2)` for removal once all callers pass a `ReportDocument` directly
- `render_pdf_report(research_data, ..., trace_summary)` in `pdf_report.py` —
  same shim pattern; marked `TODO(P2)`

---

## [0.1.0] — 2026-05-16

Initial multi-agent due diligence system. Four specialist agents run in parallel
via `asyncio.gather`; a fifth synthesis agent aggregates their outputs.

### Added

- `src/agents/base.py` — `BaseAgent` with PLANNING → EXECUTING → REFLECTING →
  COMPLETE/RETRY/FAILED state machine; `WebSearchMixin` providing shared
  `web_search` and `web_fetch` tool handling; `strip_json()` helper
- `src/agents/research.py`, `financial.py`, `risk.py`, `social_media.py` —
  four specialist agents producing `CompanyResearch`, `CompanyFinancials`,
  `CompanyRisks`, `CompanySocialMedia` Pydantic models
- `src/agents/synthesis.py` — synthesis agent aggregating all Phase 1 outputs
  into `CompanySynthesis` with investment recommendation, key strengths/concerns,
  red flags, data conflicts, and follow-up questions
- `src/agents/classifier.py` — lightweight pre-classification call (no tools)
  that adds `company_context` (sector, type, business model, region) to each
  agent's prompt
- `src/schemas/models.py` — agent output layer: `DataPoint` (value + confidence
  + sources + reasoning + optional severity), `SeverityLevel`, `ConfidenceLevel`,
  `CompanyResearch`, `CompanyFinancials`, `CompanyRisks`, `CompanySocialMedia`,
  `CompanySynthesis`
- `src/tools/web_search.py` — `web_search` (Brave Search API or mock via
  `USE_MOCK_SEARCH=true`) and `web_fetch` (HTML-stripped, truncated to 8K chars)
- `src/observability/tracer.py` — `AgentTracer` with per-run 12-char hex
  `trace_id`; `TraceSpan` per LLM/tool call with cost, tokens, duration,
  model, prompt_version; `tracer.persist()` writes to `runs` + `spans` tables
- `src/observability/agent_db.py` — `AgentDB` logging every LLM request/
  response, tool I/O, and conversation message to `agent_runs`, `llm_calls`,
  `tool_calls`, `messages` tables; natural composite keys throughout
- `src/synthesis/report_generator.py` — markdown report with tables, confidence
  badges, severity sorting (CRITICAL first), weighted overall confidence
  (Financial 40% + Risk 40% + Social Media 20%), gaps section, methodology footer
- `src/synthesis/pdf_report.py` — styled ReportLab PDF and companion HTML with
  color-coded confidence/severity badges
- `src/main.py` — orchestrator: classify → Phase 1 parallel → Phase 2 synthesis
  → report generation; `_AGENT_TIMEOUT = 300s` per agent; `--dump-db` flag
- `evals/eval_runner.py` — `GroundTruthEvaluator` with exact, fuzzy (substring +
  word overlap), and list-overlap matching; `persist_eval_results()`;
  `print_scorecard()` via Rich
- `evals/ground_truth/` — stable fact fixtures for Stripe, Anthropic, Shopify,
  NVDA, GOOGL, TSLA, AAPL, MSFT, GEV, AVGO, TSM, CRCL, NEE
- `scripts/dashboard.py` — Streamlit dashboard (Overview, Traces, Agent Runs,
  Conversation Replay, LLM Calls, Tool Calls, Evals, Raw SQL)
- `scripts/query_db.py` — CLI for post-run DB inspection
- `docs/STRATEGY.md` — strategic brief: positioning, roadmap (P1–P8), non-goals,
  success metrics, working notes for Claude Code
- `tests/test_agents.py`, `test_schemas_and_tracer.py`, `test_evals.py`,
  `test_synthesis.py` — agent state machine, schemas, severity sorting,
  observability, eval system

---

[Unreleased]: https://github.com/rbsundaramoorthy/agentic-due-diligence/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/rbsundaramoorthy/agentic-due-diligence/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/rbsundaramoorthy/agentic-due-diligence/releases/tag/v0.1.0
