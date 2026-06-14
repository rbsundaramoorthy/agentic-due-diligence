# Plan: EDGAR CIK Resolution Fix + S-1 Support

**Status:** Draft — awaiting review  
**Proposed split:** PR1 (CIK hotfix) / PR2 (S-1 feature) — see §Q2  
**PROMPT_VERSION:** No change PR1; `"1.1"` → `"1.2"` PR2  
**SCHEMA_VERSION:** No change in either PR (see §6)

---

## Step 0 — Confirmed Proximate Cause

### HTTP layer: not the issue

The EFTS endpoint returned HTTP 200 for `q="Apple Inc."&forms=10-K`. `_HEADERS` carries `User-Agent: AgenteAuditBot/1.0 contact@example.com`, which is SEC-compliant format. No 403 or 429 was observed. The failure is **not HTTP-layer**.

### The real EFTS `_source` schema (captured live, 2026-06-11)

```json
{
  "_index": "edgar_file",
  "_id": "0000320193-24-000123:a10-kexhibit23109282024.htm",
  "_score": 9.271851,
  "_source": {
    "ciks":          ["0000320193"],
    "display_names": ["Apple Inc.  (AAPL)  (CIK 0000320193)"],
    "adsh":          "0000320193-24-000123",
    "file_date":     "2024-11-01",
    "period_ending": "2024-09-28",
    "form":          "10-K",
    "file_type":     "EX-23.1",
    "root_forms":    ["10-K"],
    "biz_locations": ["Cupertino, CA"],
    "inc_states":    ["CA"],
    "biz_states":    ["CA"],
    "sics":          ["3571"],
    "file_num":      ["001-36743"],
    "sequence":      "9",
    "items":         [],
    "xsl":           null
  }
}
```

### Fields the code reads vs. what the API returns

| Code reads | Real field | Impact |
|---|---|---|
| `_source.entity_name` | **absent** (real: `display_names` list) | Disambiguation sort always ties on `exact=0` |
| `_source.accession_no` | **absent** (real: `adsh`) | `accession = ""` → `_parse_cik_from_accession("")` → `None` |
| `_source.form_type` | **absent** (real: `form`, `root_forms`) | Not used in CIK path, but wrong in notes |
| CIK parsed from accession | **direct in `ciks[0]`** | The whole parsing detour is unnecessary |

With `accession = ""` (line 149), `_parse_cik_from_accession("")` returns `None` (splits on `-`, parts is `[""]`, returns `""`… actually `parts[0]` returns the empty string, and `edgar_find_company` checks `if not cik` → True → returns `"Could not parse CIK from accession number."` → the agent maps any `"error"` key to `lookup_failed`. This matches the Apple failure exactly.

**Both defects confirmed:** (a) CIK is read from a field that doesn't exist; (b) the entity-name disambiguation has been non-functional since day one.

### Additional finding: the EFTS returns document records, not filing records

The EFTS indexes individual documents within a filing (primary form + all exhibits). A single Apple 10-K generates dozens of hits — one for the 10-K itself (`file_type="10-K"`) and one per exhibit (`file_type="EX-23.1"`, `"EX-21.1"`, etc.). The top hit by relevance score is often an exhibit, not the primary filing. The code never filtered to primary documents. After fixing the field names, disambiguation must also filter to `file_type in {wanted_forms}` to sort over primary filings only.

### The `succeeded` path has never run in production

Five known production outputs:

| Report | `edgar_lookup_status` |
|---|---|
| report_anthropic.json | `not_sec_reporting` |
| report_openai.json | `not_sec_reporting` |
| report_stripe.json | `not_sec_reporting` |
| report_spacex.json | `not_sec_reporting` |
| report_apple.json | `lookup_failed` |

`succeeded` has **never appeared** in any production output. No EDGAR financials have ever been assembled into a real report. The EDGAR → assembler merge path (`_merge_edgar`, `_merge_edgar_into_risk`) is untested in production. We cannot trust any production EDGAR behavior as established and correct.

---

## 1. Field Mapping Fix (PR1 scope)

### 1.1 `edgar_find_company` in `src/sources/edgar.py`

**Current code reading non-existent fields (lines ~138–170):**

```python
def _sort_key(hit):
    src = hit.get("_source", {})
    file_date = src.get("file_date", "1900-01-01")      # ✓ field exists
    exact = 1 if src.get("entity_name", "").strip()     # ✗ field does NOT exist
                     == name_stripped else 0
    return (exact, file_date)

best = hits[0]["_source"]
accession = best.get("accession_no", "")                # ✗ field is "adsh"
cik = _parse_cik_from_accession(accession)              # always returns None
```

**Fixed reads:**

```python
# Parse entity name from display_names (e.g. "Apple Inc.  (AAPL)  (CIK 0000320193)")
def _entity_name_from_display(display_names: list) -> str:
    raw = display_names[0] if display_names else ""
    return raw.split("  (")[0].strip()   # strips " (AAPL) (CIK 0000320193)" suffix

def _sort_key(hit):
    src = hit.get("_source", {})
    file_date = src.get("file_date", "1900-01-01")
    entity = _entity_name_from_display(src.get("display_names", []))
    exact = 1 if entity.lower() == name_stripped.lower() else 0
    return (exact, file_date)

# Filter to primary filing documents before sorting
wanted_file_types = {"10-K"}   # PR2 adds: "S-1", "S-1/A"
primary_hits = [h for h in hits if h["_source"].get("file_type", "") in wanted_file_types]
if not primary_hits:
    primary_hits = hits   # fallback: don't discard all results if filter overshoots

primary_hits.sort(key=_sort_key, reverse=True)
best = primary_hits[0]["_source"]

cik = best.get("ciks", [None])[0]           # direct — no parsing needed
accession = best.get("adsh", "")            # replaces accession_no
company_name = _entity_name_from_display(best.get("display_names", []))
most_recent = best.get("file_date", "unknown")
```

`_parse_cik_from_accession` can remain as a utility but is no longer called in the primary path. Do not delete it — it may be needed in downstream `edgar_get_filing_text` URL construction.

### 1.2 `_EDGAR_FIND_TOOL` description (agents/edgar.py)

The tool description currently says `Returns {found, cik, company_name, most_recent_10k, accession_no, note}` — this is still accurate for the output shape (we still return `accession_no` in the result, now sourced from `adsh`). No change needed.

### 1.3 No change to `edgar_get_financials` or `edgar_get_filing_text`

`edgar_get_financials` uses only the `cik` argument, which now comes from `ciks[0]` directly. It will work unchanged.

`edgar_get_filing_text` receives its accession number from `edgar_get_financials`'s `revenue.accession_no` (which is the XBRL observation's `accn` field — sourced from companyfacts, not EFTS). This path is unaffected.

---

## 2. Q1 Decision — Discovery Strategy: Option A (EFTS only, fix field mapping)

**Recommendation: Option A** for both PR1 and PR2. No `company_tickers.json` lookup in this PR.

**Rationale:**

Option B (company_tickers.json first) is appealing but adds two problems with marginal gain:
1. A second fetched resource with its own caching/staleness concerns (10,416 entries; updates when companies list/delist).
2. It doesn't resolve the accession number — after getting the CIK from tickers, you'd still need an EFTS call to get the most recent filing's `adsh` for `edgar_get_filing_text`. That reduces Option B to "a pre-filter before the EFTS call you're making anyway."
3. DBA names break it: searching "SpaceX" → company_tickers has `"SPACE EXPLORATION TECHNOLOGIES CORP"` — a case-insensitive substring match would be needed, re-introducing ambiguity risk.

The fixed EFTS resolver (Option A) handles Apple correctly once the field names are right: filtering to `file_type="10-K"` primary docs, then sorting by (exact name match in display_names, file_date DESC) picks Apple Inc. correctly over Apple Bank for Savings. Verified against live data.

Option B noted for a potential P5+ enhancement: for ticker-known companies (e.g., "AAPL"), a tickers lookup would skip the EFTS call entirely and return CIK immediately — useful if EDGAR rate limits become a concern at scale.

---

## 3. Fixture Re-Capture (PR1)

### 3.1 What's wrong with the current fixtures

The existing `search_aapl.json` and `search_apple_disambiguation.json` have `_source` objects with `entity_name`, `accession_no`, `form_type` — none of which exist in the real EFTS response. This is the identical pattern as the CourtListener `absolute_url` fixture bug. Tests pass against invented schemas; production fails against the real API.

### 3.2 Re-capture commands (to be documented in `tests/fixtures/edgar/CAPTURE_COMMANDS.md`)

```bash
# AAPL 10-K search — single unambiguous primary filer
curl -s "https://efts.sec.gov/LATEST/search-index?q=%22Apple+Inc.%22&forms=10-K&dateRange=custom&startdt=2024-01-01" \
  -H "User-Agent: AgenteAuditBot/1.0 contact@example.com" \
  -H "Accept: application/json" \
  > tests/fixtures/edgar/search_aapl.json

# Apple disambiguation — search that returns multiple filers named "Apple"
# Trim to 2-3 hits covering Apple Inc. + Apple Hospitality REIT (different CIKs)
curl -s "https://efts.sec.gov/LATEST/search-index?q=%22Apple%22&forms=10-K&dateRange=custom&startdt=2024-01-01" \
  -H "User-Agent: AgenteAuditBot/1.0 contact@example.com" \
  -H "Accept: application/json" \
  > tests/fixtures/edgar/search_apple_disambiguation.json
  # Then trim to ≤3 representative hits in the file

# SpaceX S-1 — for PR2
curl -s "https://efts.sec.gov/LATEST/search-index?q=%22Space+Exploration+Technologies%22&forms=10-K%2CS-1%2CS-1%2FA" \
  -H "User-Agent: AgenteAuditBot/1.0 contact@example.com" \
  -H "Accept: application/json" \
  > tests/fixtures/edgar/search_spacex_s1.json
  # Trim to ≤3 hits: at least one S-1/A primary doc

# Zero-result fixture (static shape, no real company needed)
curl -s "https://efts.sec.gov/LATEST/search-index?q=%22ZeroHitCorporation12345%22&forms=10-K%2CS-1" \
  -H "User-Agent: AgenteAuditBot/1.0 contact@example.com" \
  -H "Accept: application/json" \
  > tests/fixtures/edgar/search_zero_result.json
```

All fixture files must include `_source` objects with the real EFTS field names. Each file gets a header comment (or a companion `.md`) pointing back to `CAPTURE_COMMANDS.md`.

### 3.3 Sample re-captured `search_aapl.json` shape (real schema)

```json
{
  "hits": {
    "total": {"value": 1794, "relation": "eq"},
    "hits": [
      {
        "_index": "edgar_file",
        "_id": "0000320193-24-000123:a10-k.htm",
        "_score": 9.27,
        "_source": {
          "ciks": ["0000320193"],
          "display_names": ["Apple Inc.  (AAPL)  (CIK 0000320193)"],
          "adsh": "0000320193-24-000123",
          "file_date": "2024-11-01",
          "period_ending": "2024-09-28",
          "form": "10-K",
          "file_type": "10-K",
          "root_forms": ["10-K"],
          "biz_locations": ["Cupertino, CA"],
          "inc_states": ["CA"],
          "sics": ["3571"],
          "items": []
        }
      }
    ]
  }
}
```

Note `file_type: "10-K"` (primary document, not an exhibit). The disambiguation fixture must include at least one hit with `ciks: ["0001045604"]` (Apple Hospitality REIT) to test the sort key.

---

## 4. S-1 Support (PR2 scope)

### 4.1 Q3 — Form set

**Recommendation:** `forms=10-K,S-1,S-1/A` for the EFTS query in PR2.

- `10-K`: standard annual report. Primary form of EDGAR data.
- `S-1`: initial registration statement. SpaceX's first EDGAR filing type.
- `S-1/A`: S-1 amendment (SpaceX's most recent filings are S-1/A). Must include — these often contain more complete financial information than the initial S-1.
- `20-F` / `F-1`: foreign annual / foreign registration — noted as extension, explicitly out of scope for this PR.

**Tie-break rule when a company has both 10-K and S-1 filings:**

Prefer the most recent 10-K. A 10-K contains audited annual financials; an S-1 contains pre-IPO disclosures that may include unaudited projections. The code should:
1. Check if any primary-doc hit has `file_type="10-K"` → use the most-recent one.
2. If no 10-K hits, fall back to most-recent S-1 or S-1/A.

This preference is encoded in the sort key: `form_priority = 0 if file_type == "10-K" else 1`, sort by `(exact_match DESC, form_priority ASC, file_date DESC)`.

`edgar_find_company` result gains a `filing_type` field (`"10-K"` or `"S-1"`) so the agent and assembler know which path was taken.

### 4.2 Q4 — companyfacts sparsity for S-1 filers

**Recommendation:** companyfacts 404 when CIK is known valid → `succeeded` with gaps, not `lookup_failed`.

SpaceX's companyfacts URL (`CIK0001181412.json`) will 404 if SpaceX hasn't filed XBRL-tagged financial statements (pre-IPO S-1 filers often haven't). The current code at line 231:

```python
if e.response.status_code == 404:
    return json.dumps({"error": "cik_not_found", "cik": cik})
```

This returns `{"error": ...}` which the agent maps to `lookup_failed`. But the CIK IS found — `edgar_find_company` already returned `found=true`. A 404 on companyfacts means "no XBRL financial data yet," not "company doesn't exist."

**Fix in `edgar_get_financials`:** distinguish the two 404 scenarios:

```python
if e.response.status_code == 404:
    return json.dumps({
        "cik": cik,
        "revenue": None,
        "net_income": None,
        "revenue_note": (
            "No XBRL companyfacts available for this CIK. "
            "Company may be a new S-1 registrant whose financial statements "
            "are not yet tagged in the EDGAR XBRL database."
        ),
        "source_url": url,
    })
```

This returns a non-error response with `revenue: null`. The agent treats this as a gap (`value="unknown"`, `confidence="unknown"`) and still emits `edgar_lookup_status=succeeded` — which is correct: we found the company, we retrieved what EDGAR has, there just isn't structured financial data yet.

**S-1 financial data is in narrative form, not XBRL.** If SpaceX's S-1 includes financial tables, they'll only be available as HTML text in `edgar_get_filing_text`. The agent can note: "Revenue from S-1 narrative (not XBRL) — see risk factors for financial disclosures."

### 4.3 Q5 — S-1 section extraction

**Recommendation:** extend existing section patterns for S-1 structure, same downstream fields.

The current `_SECTION_PATTERNS` in `edgar_get_filing_text`:

```python
"risk_factors": [r"item\s*1a[\s\.]+risk\s*factors", r"risk\s*factors"],
"business":     [r"item\s*1[\s\.]+business\b",      r"our\s*business"],
"mda":          [r"item\s*7[\s\.]+management",       r"management.{0,30}discussion"],
```

S-1 filings use labeled sections without Item numbers: "RISK FACTORS", "BUSINESS", "MANAGEMENT'S DISCUSSION AND ANALYSIS." The fallback patterns (`r"risk\s*factors"`, `r"our\s*business"`, `r"management.{0,30}discussion"`) already handle this — no new patterns needed.

However, `edgar_get_filing_text` currently looks for `item.get("type") == "10-K"` when finding the primary document in the filing index:

```python
for item in index.get("directory", {}).get("item", []):
    if item.get("type") == "10-K" and item.get("name", "").endswith(".htm"):
```

For S-1/A filings, the `type` field is `"S-1/A"`. Fix: check `type in {"10-K", "S-1", "S-1/A"}` (or accept any `.htm` file via the existing fallback, which already handles missing type).

**Same downstream fields** (`sec_risk_factors`, `most_recent_filing`) work for S-1 — no schema change needed. The agent prompt should note that for S-1 filers, `most_recent_filing` is the S-1/A date, not a 10-K date.

### 4.4 EDGAR Agent prompt changes (PR2 only)

`PROMPT_VERSION`: `"1.1"` → `"1.2"`

Add S-1 awareness to Step 1 and Step 3:

```
Step 1 — Call edgar_find_company.
  • If found=false: not_sec_reporting. Stop.
  • If found=true and filing_type="10-K": standard path (Steps 2-3).
  • If found=true and filing_type="S-1" or "S-1/A": S-1 path.
    - Note: XBRL financials may be absent. edgar_get_financials may return 
      revenue=null — this is expected, not a lookup failure.
    - For filing text, use section="risk_factors" (S-1 structure is similar;
      fallback patterns will locate the section).
    - Set edgar_lookup_status="succeeded" even if revenue is unknown.
    - Set is_sec_reporting=true (the company has filed with the SEC).

Step 2 (S-1 path) — Call edgar_get_financials.
  • If revenue=null: set revenue DataPoint to value="unknown", 
    confidence="unknown", note "S-1 registrant — no XBRL revenue data yet".
  • Still proceed to Step 3.
```

---

## 5. Test Plan

### 5.1 Fixture-schema guard test (new, blocks both PRs)

**`tests/test_edgar_tools.py::test_efts_fixture_schema_uses_real_fields`**

```python
def test_efts_fixture_schema_uses_real_fields():
    """Fixtures must not contain invented EFTS field names.
    Regression for: search_aapl.json was hand-authored with accession_no/entity_name
    — fields the real EFTS response does not carry — so unit tests passed while
    production always failed.
    """
    INVENTED_FIELDS = {"accession_no", "entity_name", "form_type"}
    REQUIRED_FIELDS = {"ciks", "adsh", "display_names", "file_date", "file_type", "root_forms"}
    for fname in ["search_aapl.json", "search_apple_disambiguation.json"]:
        fixture = json.load(open(f"tests/fixtures/edgar/{fname}"))
        for hit in fixture["hits"]["hits"]:
            src = hit.get("_source", {})
            for bad in INVENTED_FIELDS:
                assert bad not in src, f"{fname}: invented field '{bad}' in _source"
            for req in REQUIRED_FIELDS:
                assert req in src, f"{fname}: required field '{req}' missing from _source"
```

This test will **fail** before the fixture re-capture, making the PR self-enforcing.

### 5.2 PR1 tests

**`test_find_company_aapl` (update existing)**
- Use re-captured `search_aapl.json` (real schema)
- Assert `result["cik"] == "0000320193"` — the anchor from Apple's own SEC 8-K
- Assert `result["found"] is True`
- Assert `result["accession_no"]` starts with `"0000320193"` (sourced from `adsh`)
- Assert `result["company_name"] == "Apple Inc."` (parsed from `display_names`)
- Assert the old field `entity_name` is NOT read from the fixture (guard: if we revert the code, this fails)

**`test_find_company_disambiguation_picks_most_recent_exact_match` (update existing)**
- Use re-captured disambiguation fixture with real schema (Apple Inc. + Apple Hospitality REIT)
- Assert `result["cik"] == "0000320193"` (Apple Inc. wins, not Apple Hospitality REIT)
- Verify the sort prefers exact name match (`display_names` → entity name extraction) and more-recent `file_date`

**`test_find_company_filters_to_primary_docs_not_exhibits` (new)**
- Fixture contains two hits: one exhibit (`file_type="EX-23.1"`) with an older `file_date`, one primary doc (`file_type="10-K"`) with a newer `file_date`
- Assert: result uses the primary doc's `adsh`, not the exhibit's
- Assert `result["cik"]` resolves correctly from `ciks[0]`

**`test_find_company_cik_from_ciks_list` (new)**
- Fixture: single hit with `ciks: ["0000320193"]` and no `accession_no` field
- Assert: `result["cik"] == "0000320193"` without any accession-parsing detour

**`test_find_company_zero_result` (update existing)** — reuse logic, verify fixture still returns `found=false`.

### 5.3 PR2 tests

**`test_find_company_spacex_s1` (new)**
- Fixture: real `search_spacex_s1.json` showing S-1/A hits for SPACE EXPLORATION TECHNOLOGIES CORP
- Assert `result["cik"] == "0001181412"` (SpaceX's real CIK, confirmed live)
- Assert `result["filing_type"] == "S-1"` or `"S-1/A"`
- Assert `result["found"] is True`

**`test_find_company_prefers_10k_over_s1_when_both_exist` (new)**
- Fixture: multi-hit response with both a 10-K (older) and an S-1/A (newer) for the same company
- Assert: result uses the 10-K filing even though S-1/A is more recent

**`test_get_financials_returns_gap_not_error_on_404` (new)**
- Mock: companyfacts API returns 404
- Assert: result has no `"error"` key
- Assert: `result["revenue"] is None`
- Assert: `"revenue_note"` mentions S-1 or no XBRL data

**`test_get_filing_text_works_on_s1_primary_doc` (new)**
- Mock: filing index `type="S-1/A"`, primary `.htm` file
- Assert: primary document is found despite `type != "10-K"`
- Assert: text extraction via `risk\s*factors` pattern succeeds

**`test_get_financials_aapl_uses_revenues_key` (existing)** — must still pass with real AAPL CIK; no changes to `edgar_get_financials` logic.

**`test_get_financials_jpm_uses_bank_fallback_chain` (existing)** — unchanged.

### 5.4 Failure-mode guard tests

**`test_no_cik_parse_from_accession_in_production_path` (new)**
- Verify `edgar_find_company`'s production path does NOT call `_parse_cik_from_accession`
- Mock EFTS to return a real-schema fixture with no `accession_no` field
- Assert: result still has a valid `cik` (from `ciks[0]`)
- This ensures a future refactor can't silently reintroduce the broken parse path

---

## 6. Migration — PROMPT_VERSION and SCHEMA_VERSION

### PROMPT_VERSION

- **PR1:** No prompt change. `EdgarAgent.PROMPT_VERSION` stays `"1.1"`. The fix is purely in `edgar_find_company`; the agent's workflow instructions are unchanged.
- **PR2:** `"1.1"` → `"1.2"`. The agent gains S-1-awareness instructions in Step 1 and Step 2.

### SCHEMA_VERSION

- **Neither PR** requires a `SCHEMA_VERSION` bump. `CompanyEdgarFinancials` already has all needed fields:
  - `edgar_lookup_status` covers `succeeded` (the new happy path) ✓
  - `revenue: DataPoint` with `confidence="unknown"` covers the S-1 gap case ✓
  - `is_sec_reporting: bool` correctly set to `True` for S-1 filers ✓
- A `filing_type: Optional[str]` field on `CompanyEdgarFinancials` would be useful (downstream consumers could distinguish "audited 10-K data" from "S-1 narrative"), but is deferred to avoid a schema bump in a correctness fix. Add as a PATCH bump in PR2 if agreed — `filing_type: Optional[str] = None`, SCHEMA_VERSION `"1.0.5"` → `"1.0.6"`.

---

## 7. CHANGELOG Entry

```
## [Unreleased] — 2026-06-11

### Fixed (PR1)

- **EDGAR CIK resolution completely broken in production.** The `edgar_find_company`
  tool was reading `_source.entity_name`, `_source.accession_no`, and
  `_source.form_type` from the EFTS search response — none of which exist in the
  real EFTS v4 API. The real fields are `display_names`, `adsh`, `form`, and `ciks`.
  Because `accession_no` was always `""`, `_parse_cik_from_accession("")` always
  returned an empty string, producing `lookup_failed` for every US public company
  including Apple. The `succeeded` path has never run in any production output.

- **EFTS returns document records, not filing records.** Added a primary-doc filter
  (`file_type in {"10-K"}`) so disambiguation sorts over primary filings, not
  exhibits. Previously, an auditor-consent exhibit (EX-23.1) could sort above the
  actual 10-K.

- EDGAR fixtures `search_aapl.json` and `search_apple_disambiguation.json` regenerated
  verbatim from the live EFTS API with documented capture commands. The old fixtures
  encoded the code's assumed (wrong) schema — the same class of bug as the CourtListener
  `docket_absolute_url` fixture. A schema-guard test (`test_efts_fixture_schema_uses_real_fields`)
  now fails loudly if invented field names re-appear in any EFTS fixture.

### Added (PR2)

- **S-1 support in EDGAR agent.** Form filter expanded to `10-K,S-1,S-1/A`. SpaceX
  (CIK 0001181412, ticker SPCX) and other S-1-only registrants now resolve. Tie-break
  rule: prefer most-recent 10-K over S-1/A when both exist.

- **companyfacts 404 → succeeded-with-gaps, not lookup_failed.** A 404 on the
  companyfacts API when the CIK is known valid (found=true) now returns `revenue: null`
  with a note rather than `{"error": ...}`. Prevents S-1 registrants from mapping to
  `lookup_failed` just because their XBRL financial data isn't yet available.

- **S-1 section extraction.** `edgar_get_filing_text` now accepts `file_type="S-1/A"`
  in the primary-doc scan. Existing fallback patterns (`risk\s*factors`, `our\s*business`)
  already work for S-1 section headers. New fixture `search_spacex_s1.json` from live API.

### Changed (PR2)
- `EdgarAgent.PROMPT_VERSION` bumped to `"1.2"` for S-1-aware workflow instructions.
```

---

## 8. Q2 — PR Split Recommendation: Yes, split

**PR1 (ship immediately, ~1 day):**
- `src/sources/edgar.py`: fix `edgar_find_company` field mapping + primary-doc filter
- `tests/fixtures/edgar/`: re-capture `search_aapl.json`, `search_apple_disambiguation.json` from live API
- `tests/fixtures/edgar/CAPTURE_COMMANDS.md`: document capture commands
- `tests/test_edgar_tools.py`: fixture guard test + updated CIK assertion tests
- `CHANGELOG.md`: PR1 entry

**PR2 (next, ~2 days):**
- `src/sources/edgar.py`: form filter expansion + companyfacts 404 handling + S-1 primary-doc scan fix
- `src/agents/edgar.py`: PROMPT_VERSION 1.2 + S-1 agent instructions
- `tests/fixtures/edgar/search_spacex_s1.json`: new real fixture
- New tests: S-1 resolution, 404-to-gap, filing-text S-1 path
- `CHANGELOG.md`: PR2 entry

Rationale: Apple failing on every run is a production hotfix. PR1 is minimal, easily reviewed, and immediately unblocks EDGAR for all 10-K filers. PR2 is a feature (S-1 support is additive) and needs more test surface. Combining them would delay the Apple fix.

---

## 9. Repo-Wide Fixture Convention (flag for follow-up)

This is the **second tool** to ship hand-authored fixtures encoding the code's assumed API schema instead of the real response (CourtListener `absolute_url` first; EDGAR `entity_name`/`accession_no` now). Both bugs survived in production because the fixtures matched the code, not the API.

**Proposed convention:** every `tests/fixtures/<tool>/` directory must contain a `CAPTURE_COMMANDS.md` documenting the exact shell command to regenerate each fixture file from the live API. Any fixture file without a matching command in `CAPTURE_COMMANDS.md` is assumed hand-authored and suspect. The fixture-schema guard test pattern (assert real fields are present, assert invented fields are absent) should be applied to all external-API fixture sets.

**Explicit follow-up (out of scope for this PR):** audit `tests/fixtures/opencorporates/`, `tests/fixtures/samgov/`, `tests/fixtures/courtlistener/` (already fixed) and `tests/fixtures/uspto/` for the same invented-schema pattern. The CourtListener fix (PR before this one) already added a guard test for that fixture set; extend to the remaining three.

---

## 10. Open Questions for the Reviewer

**Q-A: `_parse_cik_from_accession` — keep or remove?**

The function is no longer called in the primary resolution path. It may still be used in internal URL construction in `edgar_get_filing_text` (which constructs paths from accession numbers). Proposal: keep the function, add a docstring note that it is NOT used for CIK resolution, and add a test that the production `edgar_find_company` path doesn't call it. Or: remove it entirely if we verify `edgar_get_filing_text` has no dependency on it. Reviewer call on whether to clean up in PR1 or defer.

**Q-B: `filing_type` in `CompanyEdgarFinancials` schema — PR2 or post-P4?**

A `filing_type: Optional[str] = None` field (`"10-K"` or `"S-1"`) on `CompanyEdgarFinancials` would let the assembler and renderers distinguish "we have audited XBRL data" from "we have S-1 narrative only" — useful for calibration (P4) and for rendering the methodology footer accurately. It's a PATCH bump (SCHEMA_VERSION `"1.0.5"` → `"1.0.6"`). Add in PR2, or defer to the post-P4 schema design pass?

**Q-C: SpaceX financials in the S-1**

SpaceX's S-1 contains financial statements in HTML, not XBRL. `edgar_get_filing_text` could be called with `section="business"` to extract financial narrative, but parsing tabular financial data from raw HTML text is lossy. Proposal: for S-1 filers where companyfacts returns nothing, call `edgar_get_filing_text(section="business")` rather than `section="risk_factors"` as the primary call, and let the LLM extract whatever revenue/income figures appear in the text (marked LOW confidence, since they're from HTML-stripped tables rather than validated XBRL). Or: skip the financials step entirely for S-1 filers and rely on the Research/Financial agents. Reviewer preference?

---

## File Change Summary

**PR1:**
```
src/sources/edgar.py                          — fix _source field reads; primary-doc filter; _entity_name_from_display helper
tests/fixtures/edgar/search_aapl.json         — REPLACE with real EFTS response (new schema)
tests/fixtures/edgar/search_apple_disambiguation.json — REPLACE with real EFTS response
tests/fixtures/edgar/CAPTURE_COMMANDS.md      — NEW: documented curl commands for each fixture
tests/test_edgar_tools.py                     — fixture guard test; CIK anchor assertion; updated disambiguation test; primary-doc filter test
CHANGELOG.md                                  — PR1 entry
```

**PR2:**
```
src/sources/edgar.py                          — form filter S-1/S-1/A; 404→gap; S-1 primary-doc type scan
src/agents/edgar.py                           — PROMPT_VERSION 1.2; S-1 workflow instructions
tests/fixtures/edgar/search_spacex_s1.json    — NEW real EFTS S-1 response
tests/test_edgar_tools.py                     — S-1 resolution; 404→gap; filing-text S-1 primary doc
CHANGELOG.md                                  — PR2 entry
```

No changes to: `src/synthesis/assembler.py`, `src/schemas/models.py`, `schema/report.schema.json`, renderers, other Tier-0 tools, or strategic documents (except CHANGELOG).
