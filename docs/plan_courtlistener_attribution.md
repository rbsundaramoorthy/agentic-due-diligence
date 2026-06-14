# Plan: Fix CourtListener Legal-Claim Attribution + Clickable Docket Citations

**Status:** Draft — awaiting review  
**Scope:** `src/sources/courtlistener.py`, `src/agents/risk.py`, test fixtures, CHANGELOG  
**Schema version change:** None (see §5)  
**PROMPT_VERSION:** `RiskAgent` "2.1" → "2.2"

---

## 1. Root Cause — Confirmed in Code and Live Data

Two independent defects combined to produce the false CRITICAL claim.

### 1.1 Citation defect: wrong field name in `courtlistener.py:94`

The tool reads:

```python
case_url = c.get("absolute_url", "")
```

The **actual** field name returned by the CourtListener REST v4 search endpoint (`/api/rest/v4/search/?type=d`) is **`docket_absolute_url`**, not `absolute_url`. Live API proof — top result for "Example Aerospace Corp":

```json
{
  "caseName": "Romero v. Example Aerospace Corp",
  "docket_absolute_url": "/docket/[DOCKET-ID]/romero-v-example-aerospace-corp/",
  "docket_id": "[DOCKET-ID]",
  ...
}
```

Because `c.get("absolute_url", "")` finds nothing, `case_url` is always `""`. The guard `if case_url and not case_url.startswith("http")` never fires. Every case in the output gets `"source_url": ""`.

The tool then appends a top-level `"source_url": _SEARCH_URL` to the entire result blob. When the agent builds DataPoint `sources`, it finds no per-case URL and falls back to the only URL it sees: the bare API endpoint `https://www.courtlistener.com/api/rest/v4/search/`. This is the URL that landed in every litigation claim's `sources` field in `report_spacex.json`.

The existing **fixture** (`tests/fixtures/courtlistener/cases_stripe.json`) uses `"absolute_url"` — so the unit test passes, but it's testing a field name that the real API never returns.

### 1.2 Attribution defect: agent given too little signal to determine party role

The tool currently strips each result down to five fields:

```json
{
  "case_name": "United States v. SEIZURE OF [EQUIPMENT] AND ASSOCIATED ACCOUNTS FOR VIOLATIONS OF 18 U.S.C. §§ 1349, 1956",
  "court": "dcd",
  "date_filed": "[DATE]",
  "docket_number": "[DOCKET-NUMBER-SZ]",
  "source_url": ""
}
```

The **`party`** list is available from the API but is not passed through:

```json
"party": ["USA", "SEIZURE OF [EQUIPMENT] AND ASSOCIATED ACCOUNTS UNDER THE CONTROL OF EXAMPLE AEROSPACE CORP FOR VIOLATIONS OF 18 U.S.C. §§ 1349, 1956"]
```

Example Aerospace Corp is **absent from the `party` list**. With only the `case_name` and nothing else, the agent read "Example Aerospace Corp" and "violations of 18 U.S.C. §§ 1349, 1956" in close proximity and concluded Example Aerospace Corp committed those violations. The system prompt had no instruction for:

- Recognizing docket prefix patterns (e.g., `sz` = seizure warrant, `mc` = misc, `cr` = criminal)
- Recognizing in rem case-name patterns ("United States v. SEIZURE OF...", "In re Seizure of...")
- Checking whether the company appears as a named party vs. merely referenced in a property description
- Constraining phrasing to "named in" / "referenced in" unless the company IS a defendant

The two defects are independent but compounded: even with correct per-case URLs, the attribution error would have produced the false CRITICAL claim. Even with correct attribution, the bare API endpoint citations would still make every litigation claim unverifiable.

### 1.3 Confirmed real data shapes

**Seizure case — live API (confirmed):**

```json
{
  "caseName": "United States v. SEIZURE OF [EQUIPMENT] AND ASSOCIATED ACCOUNTS FOR VIOLATIONS OF 18 U.S.C. §§ 1349, 1956",
  "docketNumber": "[DOCKET-NUMBER-SZ]",
  "docket_absolute_url": "/docket/[DOCKET-ID]/united-states-v-seizure-of-equipment/",
  "docket_id": "[DOCKET-ID]",
  "court_id": "dcd",
  "dateFiled": "[DATE]",
  "party": [
    "USA",
    "SEIZURE OF [EQUIPMENT] AND ASSOCIATED ACCOUNTS UNDER THE CONTROL OF EXAMPLE AEROSPACE CORP FOR VIOLATIONS OF 18 U.S.C. §§ 1349, 1956"
  ],
  "cause": ""
}
```

Example Aerospace Corp is not in `party`. The docket number prefix `sz` is a seizure-warrant type. The case caption follows the in rem pattern "United States v. [description of seized property]."

**True-defendant case — live API (confirmed):**

```json
{
  "caseName": "Romero v. Example Aerospace Corp",
  "docketNumber": "[DOCKET-NUMBER-CV]",
  "docket_absolute_url": "/docket/[DOCKET-ID]/romero-v-example-aerospace-corp/",
  "court_id": "nysd",
  "dateFiled": "[DATE]",
  "party": ["Evan Romero", "Example Aerospace Corp"],
  "cause": "28:1332bc Diversity-Breach of Contract"
}
```

Example Aerospace Corp IS in `party`. This is a standard employment civil case and must continue to surface.

---

## 2. Citation Fix

### 2.1 `src/sources/courtlistener.py`

**Change 1 — field name (line 94):**

```python
# Before
case_url = c.get("absolute_url", "")

# After
case_url = c.get("docket_absolute_url", "")
```

The existing prefix logic (`if case_url and not case_url.startswith("http")`) already handles the relative-path case correctly and needs no change.

**Change 2 — fallback URL when `docket_absolute_url` is absent:**

If `docket_absolute_url` is absent AND `docket_id` is present, construct:
```
https://www.courtlistener.com/docket/{docket_id}/
```

If neither is available, construct a CourtListener search URL with the docket number encoded:
```
https://www.courtlistener.com/?q=%22{docket_number}%22&type=d
```

**Never** fall back to the bare `_SEARCH_URL` endpoint in a per-case `source_url`. The bare endpoint remains in the top-level `source_url` of the overall result JSON (for the tool call itself), but the agent prompt (§4) will instruct the agent to use the per-case `source_url`, not the top-level one.

**Change 3 — add `parties` and `cause` to per-case dict:**

```python
cases.append({
    "case_name":     c.get("caseName") or c.get("case_name", ""),
    "court":         c.get("court_id") or c.get("court", ""),
    "date_filed":    c.get("dateFiled") or c.get("date_filed", ""),
    "docket_number": c.get("docketNumber") or c.get("docket_number", ""),
    "source_url":    case_url,           # specific docket URL (see above)
    "parties":       c.get("party") or [],   # NEW — list of named parties
    "cause":         c.get("cause") or "",   # NEW — cause of action (e.g. "28:1332 Diversity")
})
```

No new API calls required. Both `party` and `cause` are present in the existing search response.

---

## 3. Attribution Model

### 3.1 Q1 decision: Hybrid (Option C)

Passing the `parties` list (§2.1 Change 3) gives the agent a positive signal: if the target company does NOT appear in `parties`, it is not a named party. This zero-cost improvement eliminates the need for docket-detail fetches (Option B) and is strictly more reliable than prompt heuristics alone (Option A). The prompt heuristics (§4) serve as belt-and-suspenders for cases where `parties` is empty or ambiguous.

### 3.2 Q2 decision: No schema change in this PR

Adding a structured `party_role` field to `DataPoint` or a new `LitigationDataPoint` subtype is the right long-term design and fits the provenance thesis. But:

- `DataPoint` is the universal base; adding `party_role` there pollutes all non-litigation DataPoints with an irrelevant field
- A proper `LitigationDataPoint(DataPoint)` subtype requires careful typing changes to `CompanyRisks.pending_litigation`, the assembler, and renderers — more than a patch-level change and not the right scope for a correctness hotfix
- The fix can be expressed with full fidelity in `value` (phrasing discipline) + `reasoning` (explicit role statement) + `confidence`/`severity` (capped values) + per-case `source_url` (verifiable)

**Deferred:** structured `party_role: Optional[Literal["defendant", "plaintiff", "third_party", "in_rem_subject", "unknown"]] = None` on a new `LitigationDataPoint` is noted for a follow-up priority (post-P4). When that ships it will be a MINOR schema bump (no default, consumers must handle absence).

**No schema version bump in this PR.** `SCHEMA_VERSION` stays at `"1.0.5"`. No changes to `schema/report.schema.json`.

### 3.3 Q3 decision: Severity/confidence policy when company is not a named defendant

When the target company is **not a named party** (not in `parties` list):

| Field | Value |
|---|---|
| `value` | "Example Aerospace Corp terminals referenced as subject of [in rem / seizure / forfeiture] proceeding: [1-sentence neutral description]. Example Aerospace Corp is not a named defendant." |
| `confidence` | `high` (the docket exists, confirmed Tier 0) |
| `severity` | `low` (company is not accused; in rem action targets property/service, not company liability) |
| `reasoning` | Explicit: "Example Aerospace Corp is not listed as a party. Docket type 'sz' indicates seizure warrant. Company's role: third-party operator directed to disable service." |
| Sources | Per-case docket URL (not the API endpoint) |

When the target company **is a named defendant** (present in `parties`):

| Field | Value |
|---|---|
| `value` | Normal case description; may assert the company "faces", "is defendant in", or "is party to" |
| `confidence` | `high` (Tier 0 primary document) |
| `severity` | Assessed from case type (enforcement = high, employment/contract = medium, etc.) |

When `parties` is empty (API returned no party list):

- Fall back to docket-number prefix and case-name pattern heuristics (§4 attribution block)
- Default to `low` severity and "referenced in" phrasing unless case name unambiguously names the company as defendant
- `confidence`: `medium` (case confirmed but party role uncertain)

---

## 4. Risk-Agent Prompt Changes

`PROMPT_VERSION`: `"2.1"` → `"2.2"`

### 4.1 CourtListener instruction block (replace current step 2)

Current text (step 2 in RESEARCH STRATEGY):
```
2. Call courtlistener_case_search.
   - Translate each case into a pending_litigation DataPoint: case name, court,
     docket number, date filed — 1 sentence each, HIGH confidence with case URL.
   - If no cases found: leave pending_litigation empty (do not fabricate entries).
```

Replacement:
```
2. Call courtlistener_case_search.
   Each case result includes: case_name, court, date_filed, docket_number,
   source_url (specific docket URL), parties (list of named parties), cause.

   CITATION RULE: Use each case's own source_url as the source for that
   DataPoint. Never cite the bare API endpoint as a source.

   ATTRIBUTION DISCIPLINE — determine the company's role before writing the claim:

   a) Named defendant: the company appears by name in the parties list AND is
      not the plaintiff (case_name pattern: "X v. [Company]" or "[Company] v. X"
      where Company is a defendant).
      → Permitted phrasing: "is a defendant in", "faces", "is party to"
      → Severity: assessed from case type (enforcement = HIGH, employment/contract = MEDIUM)
      → Confidence: HIGH

   b) In rem / seizure / forfeiture — NOT a defendant:
      Recognize by ANY of:
        - docket_number prefix "sz", "mc" (misc), "frf", "cv-forf"
        - case_name starting with "United States v. SEIZURE OF", "United States v. [ALL CAPS PROPERTY]",
          "In re Seizure of", "In re Forfeiture of", "United States v. Approximately $...",
          "United States v. [numeric amount or physical asset description]"
        - company NOT in parties list and parties contains "USA" / "United States"
      → Required phrasing: "referenced in in rem [seizure/forfeiture] proceeding",
        "company terminals subject to seizure warrant", "Example Aerospace Corp's property/service
        named in [docket type] — Example Aerospace Corp is not a defendant"
      → Severity: LOW (company is not accused; proceeding targets property or service)
      → Confidence: HIGH (docket confirmed) but add reasoning note: company is NOT a party

   c) Company not in parties list (parties field populated) — third-party reference:
      → Required phrasing: "referenced in", "named in case caption", "mentioned as [role]"
      → Severity: LOW
      → Confidence: MEDIUM (role is uncertain)

   d) parties list empty — cannot determine role from data alone:
      → Apply docket-number and case-name heuristics from (b) above
      → Default to LOW severity and "referenced in" phrasing
      → Confidence: MEDIUM

   NEVER attribute statutory violations (e.g. 18 U.S.C. § 1349, § 1956) to the
   company unless the company is a named criminal defendant and the indictment
   explicitly charges the company. An in rem case that mentions statutes in the
   property description is NOT an accusation against the company.

   If no cases found: leave pending_litigation empty.
```

### 4.2 Confidence ranking rule (minor update)

Current:
```
CourtListener dockets in pending_litigation: use HIGH confidence, assess severity
from case type (enforcement = HIGH, contract dispute = MEDIUM, etc.).
```

Updated:
```
CourtListener dockets in pending_litigation: HIGH confidence when the docket is
confirmed (regardless of company role). Severity is HIGH only when the company
is a named defendant in an enforcement or criminal action. For in rem or
third-party references: severity = LOW regardless of the statutes cited.
```

---

## 5. Tool, Schema, and Migration Changes

### 5.1 `src/sources/courtlistener.py`

| Change | Detail |
|---|---|
| Fix field name | `c.get("absolute_url", "")` → `c.get("docket_absolute_url", "")` |
| Add fallback URL | Prefer `docket_absolute_url`; fall back to `docket/{docket_id}/`; last resort: search URL with docket number |
| Add `parties` to per-case dict | `c.get("party") or []` |
| Add `cause` to per-case dict | `c.get("cause") or ""` |
| Update fixture | `tests/fixtures/courtlistener/cases_stripe.json` — rename `absolute_url` → `docket_absolute_url` in fixture; add `party` and `cause` fields |

### 5.2 `src/agents/risk.py`

| Change | Detail |
|---|---|
| `PROMPT_VERSION` | `"2.1"` → `"2.2"` |
| System prompt | Replace step-2 block with attribution discipline block (§4.1) |
| Confidence ranking | Update CourtListener ranking rule (§4.2) |

### 5.3 Schema — no change

`SCHEMA_VERSION` stays at `"1.0.5"`. No Pydantic model changes. No JSON schema regeneration required.

### 5.4 No migration required

No changes to existing ReportDocument fields, assembler logic, renderer, or observability tables. Existing `report_spacex.json` is not retroactively fixed (it's a snapshot). Future runs for SpaceX will produce correct output.

---

## 6. Test Plan

### 6.1 Regression fixture: in rem seizure case (the real failure)

**New fixture:** `tests/fixtures/courtlistener/cases_thirdparty_seizure.json`

```json
{
  "count": 2,
  "next": null,
  "previous": null,
  "results": [
    {
      "caseName": "United States v. SEIZURE OF [EQUIPMENT] AND ASSOCIATED ACCOUNTS FOR VIOLATIONS OF 18 U.S.C. §§ 1349, 1956",
      "docketNumber": "[DOCKET-NUMBER-SZ]",
      "docket_absolute_url": "/docket/[DOCKET-ID]/united-states-v-seizure-of-equipment/",
      "docket_id": "[DOCKET-ID]",
      "court_id": "dcd",
      "dateFiled": "[DATE]",
      "party": ["USA", "SEIZURE OF [EQUIPMENT] AND ASSOCIATED ACCOUNTS UNDER THE CONTROL OF EXAMPLE AEROSPACE CORP FOR VIOLATIONS OF 18 U.S.C. §§ 1349, 1956"],
      "cause": ""
    },
    {
      "caseName": "Romero v. Example Aerospace Corp",
      "docketNumber": "[DOCKET-NUMBER-CV]",
      "docket_absolute_url": "/docket/[DOCKET-ID]/romero-v-example-aerospace-corp/",
      "docket_id": "[DOCKET-ID]",
      "court_id": "nysd",
      "dateFiled": "[DATE]",
      "party": ["Evan Romero", "Example Aerospace Corp"],
      "cause": "28:1332bc Diversity-Breach of Contract"
    }
  ]
}
```

### 6.2 New tests in `tests/test_courtlistener.py`

**`test_docket_url_populated_from_docket_absolute_url`**
- Mock response contains `docket_absolute_url: "/docket/[DOCKET-ID]/..."` (no `absolute_url`)
- Assert: `result["cases"][0]["source_url"] == "https://www.courtlistener.com/docket/[DOCKET-ID]/..."`
- Assert: `result["cases"][0]["source_url"]` does NOT equal `"https://www.courtlistener.com/api/rest/v4/search/"`

**`test_fallback_url_from_docket_id`**
- Mock response has neither `absolute_url` nor `docket_absolute_url` but has `docket_id: 99999`
- Assert: `source_url` == `"https://www.courtlistener.com/docket/99999/"`

**`test_fallback_url_from_docket_number`**
- Mock response has neither `absolute_url` nor `docket_absolute_url` nor `docket_id` but has `docketNumber: "1:25-sz-00048"`
- Assert: `source_url` contains `"courtlistener.com"` and `"1%3A25-sz-00048"` (URL-encoded docket number)
- Assert: `source_url` does NOT equal `_SEARCH_URL`

**`test_parties_and_cause_in_case_dict`**
- Mock response contains `party: ["USA", "SEIZURE OF..."]` and `cause: ""`
- Assert: `result["cases"][0]["parties"] == ["USA", "SEIZURE OF..."]`
- Assert: `result["cases"][0]["cause"] == ""`

**`test_citation_never_bare_api_endpoint`**
- Any mock response with populated `docket_absolute_url`
- Assert: no `source_url` in `result["cases"]` equals `"https://www.courtlistener.com/api/rest/v4/search/"`

**Update `test_found_cases_returns_dockets`**
- Update the Stripe fixture (`cases_stripe.json`): rename `absolute_url` → `docket_absolute_url`, add `party` and `cause` fields
- Update assertion: `assert case["source_url"] == "https://www.courtlistener.com/docket/18523401/stripe-inc-v-a-b-c-payments-llc/"`

### 6.3 New file: `tests/test_risk_attribution.py`

These are prompt-level integration tests. They mock the CourtListener tool response and assert that the Risk Agent's output DataPoints are correctly classified. They do NOT require a live LLM call — they exercise the agent state machine with a mocked `handle_tool_call` that returns the fixture JSON.

**`test_in_rem_seizure_not_attributed_as_defendant`**

Input: CourtListener returns the seizure case above ([DOCKET-NUMBER-SZ]). Example Aerospace Corp is not in `parties`.

Assertions on `pending_litigation[0]` (or whichever item maps to the seizure case):
- `"conspiracy"` and `"money laundering"` do NOT appear in `value` as accusations against Example Aerospace Corp
- `"seizure"` OR `"in rem"` OR `"not a defendant"` appears in `value` or `reasoning`
- `severity` is `"low"` (not `"critical"` or `"high"`)
- `sources` list does NOT contain `"https://www.courtlistener.com/api/rest/v4/search/"` (bare endpoint)
- `sources` list contains a URL that includes `"courtlistener.com/docket/"` (specific docket)

**`test_true_defendant_still_surfaces`**

Input: CourtListener returns only the Romero v. Example Aerospace Corp case ([DOCKET-NUMBER-CV]). Example Aerospace Corp IS in `parties`.

Assertions on `pending_litigation[0]`:
- Example Aerospace Corp appears in `value`
- `sources` contains a URL matching `"courtlistener.com/docket/[DOCKET-ID]"` or equivalent
- `severity` is NOT `"low"` (employment case warrants at least `"medium"`)
- Claim is present in the output (not suppressed)

**`test_citation_policy_no_bare_endpoint_in_sources`**

Input: CourtListener returns any two cases with populated `docket_absolute_url`.

Assertion: for every DataPoint in `pending_litigation`, no `source` string equals `"https://www.courtlistener.com/api/rest/v4/search/"`.

---

## 7. Doc Updates

**CHANGELOG.md** — new entry at top:

```
## [Unreleased] — 2026-06-01

### Fixed
- CourtListener: per-case docket URLs now correctly sourced from `docket_absolute_url`
  field (was `absolute_url`, which the API does not return); every litigation claim
  now cites a verifiable docket page, not the bare API search endpoint.
- Risk Agent: in rem / seizure-warrant dockets (docket prefix "sz", "United States v.
  SEIZURE OF..." caption patterns) are now recognized as third-party references rather
  than accusations against the target company. Severity capped at LOW; phrasing requires
  "referenced in" rather than "faces violations of".
- Risk Agent: `parties` list from CourtListener is now passed through to the agent so
  it can determine whether the target company is a named party vs. merely referenced in
  a case caption.

### Changed
- `RiskAgent.PROMPT_VERSION` bumped to "2.2".
- CourtListener tool result now includes `parties` and `cause` fields per case.

### Notes
- The same name-in-caption misattribution pattern exists in other sources (news
  headlines, SAM.gov entity names). Out of scope here; tracked for a follow-up.
```

**README.md** — no public surface changes; no update required.

---

## 8. Open Questions for the Reviewer

**Q-A: Structured `party_role` field deferral**

This plan defers the structured `party_role: Optional[Literal[...]]` field to a follow-up priority, relying on phrasing discipline + `value`/`reasoning` fields instead. The argument is speed (this is a hotfix for real-world harm) and avoiding polluting the universal `DataPoint` base. Is this deferral acceptable, or should this PR include a `LitigationDataPoint(DataPoint)` subtype with `party_role`? If yes, it needs a MINOR schema bump (1.0.6 or 1.1.0) and assembler/renderer changes.

**Q-B: Fixture update scope**

The existing `cases_stripe.json` fixture uses `"absolute_url"` (wrong field name). Updating it to `"docket_absolute_url"` fixes the test to match reality, but it also means the existing `test_found_cases_returns_dockets` test will fail until both the fixture AND the code are updated in sync. Is this acceptable (one-PR atomic fix), or should the fixture update happen independently with an intermediate compatibility shim? I'd prefer the atomic fix — the fixture was always testing against the wrong field name.

**Q-C: Docket-prefix heuristics completeness**

The attribution block (§4.1) handles `sz` (seizure), `mc` (misc), `frf`/`cv-forf` (forfeiture). Are there other federal docket prefixes that indicate in rem or third-party-reference proceedings that compliance reviewers specifically care about? The current list covers the known misattribution pattern; additions can come via incremental prompt updates without a PR.

---

## 9. File Change Summary

```
src/sources/courtlistener.py     — fix absolute_url → docket_absolute_url; add parties + cause; URL fallback chain
src/agents/risk.py               — PROMPT_VERSION 2.1→2.2; attribution discipline block; updated confidence rules
tests/fixtures/courtlistener/
  cases_stripe.json              — rename absolute_url → docket_absolute_url; add party + cause fields
  cases_thirdparty_seizure.json      — NEW: regression fixture (seizure + true-defendant cases)
tests/test_courtlistener.py      — update existing test; add 5 new citation/party tests
tests/test_risk_attribution.py   — NEW: 3 attribution tests (in_rem, true_defendant, no_bare_endpoint)
CHANGELOG.md                     — new entry
```

No changes to: `src/schemas/models.py`, `schema/report.schema.json`, `src/synthesis/assembler.py`, renderers, observability layer, EDGAR agent, other Tier 0 tools, or any strategic/architecture documents except CHANGELOG.
