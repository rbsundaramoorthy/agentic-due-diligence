"""Tests for EDGAR tool functions using fixture JSON responses.

All HTTP calls are intercepted by patching _rate_limited_get so no network
access is required. Fixture files live in tests/fixtures/edgar/.

Fixture integrity: every EFTS search fixture and companyfacts fixture must be
captured from the live API (see tests/fixtures/edgar/CAPTURE_COMMANDS.md).
Hand-authored fixtures that encode the code's assumed schema (rather than the
real API schema) caused the Apple CIK resolution bug (EFTS fields) and the
Revenues-key fixture bug (Apple uses RevenueFromContract since FY2019).
The guard test test_efts_fixture_schema_uses_real_fields enforces the real-field
contract; test_edgar_live.py carries the live regression anchors.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.sources.edgar import edgar_find_company, edgar_get_financials

FIXTURES = Path(__file__).parent / "fixtures" / "edgar"


def _mock_response(fixture_name: str) -> MagicMock:
    """Return a mock httpx.Response backed by a fixture file."""
    body = (FIXTURES / fixture_name).read_text()
    resp = MagicMock()
    resp.json.return_value = json.loads(body)
    resp.raise_for_status = MagicMock()
    return resp


# ── Fixture integrity guard ───────────────────────────────────────────────────

def test_efts_fixture_schema_uses_real_fields():
    """EFTS search fixtures must carry real API field names, not invented ones.

    The real EFTS v4 _source uses:
      ciks, adsh, display_names, file_date, file_type, root_forms, ...

    It does NOT have:
      entity_name, accession_no, form_type  ← old invented fields

    A fixture encoding the wrong field names passes unit tests while production
    silently fails — the root cause of the Apple CIK resolution bug (PR1).
    """
    INVENTED_FIELDS = {"accession_no", "entity_name", "form_type"}
    REQUIRED_FIELDS = {"ciks", "adsh", "display_names", "file_date", "form", "file_type", "root_forms"}
    EFTS_FIXTURES = [
        "search_aapl.json",
        "search_apple_disambiguation.json",
        "search_apple_ambiguous.json",
        "search_spacex_mention_only.json",
        "search_spacex_s1.json",
        "search_spacex_424b4.json",   # PR2: 424B4 (priced prospectus) fixture
    ]

    for fname in EFTS_FIXTURES:
        fixture = json.loads((FIXTURES / fname).read_text())
        for hit in fixture["hits"]["hits"]:
            src = hit.get("_source", {})
            for bad in INVENTED_FIELDS:
                assert bad not in src, (
                    f"{fname}: invented field '{bad}' found in _source — "
                    f"re-capture from live EFTS (see CAPTURE_COMMANDS.md)"
                )
            for req in REQUIRED_FIELDS:
                assert req in src, (
                    f"{fname}: required real field '{req}' missing from _source — "
                    f"re-capture from live EFTS (see CAPTURE_COMMANDS.md)"
                )


def test_companyfacts_fixture_uses_real_revenue_key():
    """companyfacts_aapl.json must use the real Apple revenue key.

    Apple uses us-gaap.RevenueFromContractWithCustomerExcludingAssessedTax from
    FY2019 onward. The old hand-authored fixture put FY2024 data under
    us-gaap.Revenues — a key Apple stopped using after FY2018. This guard
    confirms the fixture reflects the actual XBRL schema.
    """
    fixture = json.loads((FIXTURES / "companyfacts_aapl.json").read_text())
    us_gaap = fixture["facts"]["us-gaap"]

    # us-gaap.Revenues must NOT have FY2019+ data
    revenues = us_gaap.get("Revenues", {})
    rev_annual = [
        u for u in revenues.get("units", {}).get("USD", [])
        if u.get("form") == "10-K" and u.get("fp") == "FY"
        and int(u.get("fy") or u.get("end", "0000")[:4]) >= 2019
    ]
    assert len(rev_annual) == 0, (
        f"Revenues key has FY2019+ entries — Apple uses RevenueFromContract since FY2019. "
        f"Fixture was likely hand-authored. Re-capture from live API."
    )

    # RevenueFromContractWithCustomerExcludingAssessedTax must have FY2024+
    rfct_key = "RevenueFromContractWithCustomerExcludingAssessedTax"
    assert rfct_key in us_gaap, (
        f"companyfacts_aapl.json missing {rfct_key} — re-capture from live API"
    )
    rfct_annual = [
        u for u in us_gaap[rfct_key]["units"]["USD"]
        if u.get("form") == "10-K" and u.get("fp") == "FY"
    ]
    assert any(int(u.get("fy") or 0) >= 2025 for u in rfct_annual), (
        f"companyfacts_aapl.json has no FY2025+ RevenueFromContract entry — "
        f"re-capture from live API to get current data"
    )


def test_companyfacts_spacex_empty_fixture_integrity():
    """companyfacts_spacex_empty.json must be captured from live API, not hand-authored.

    The fixture represents CIK 0001181412 (SpaceX) shortly after IPO — the
    companyfacts API returns entity metadata but zero us-gaap and dei facts.
    This guards that the fixture reflects the REAL API response format.
    """
    fixture = json.loads((FIXTURES / "companyfacts_spacex_empty.json").read_text())
    # Must have entity metadata
    assert fixture.get("entityName") == "SPACE EXPLORATION TECHNOLOGIES CORP", (
        "entityName mismatch — re-capture from live API"
    )
    assert fixture.get("cik") == "0001181412", (
        "CIK mismatch — re-capture from live API"
    )
    # Must have facts structure with empty us-gaap (the defining property of this fixture)
    assert "facts" in fixture, "Missing 'facts' key — fixture may be hand-authored"
    us_gaap = fixture["facts"].get("us-gaap", {})
    assert len(us_gaap) == 0, (
        f"Expected empty us-gaap (pre-XBRL SpaceX); got {len(us_gaap)} keys. "
        "Re-capture if SpaceX has filed XBRL data since last capture."
    )


# ── edgar_find_company: CIK resolution ───────────────────────────────────────

@pytest.mark.asyncio
async def test_find_company_aapl():
    """Apple resolves to CIK 0000320193 — the canonical anchor from Apple's own SEC 8-K."""
    mock_resp = _mock_response("search_aapl.json")
    with patch("src.sources.edgar._rate_limited_get", new=AsyncMock(return_value=mock_resp)):
        result_str = await edgar_find_company("Apple Inc.")
    result = json.loads(result_str)

    assert result["found"] is True
    assert result["cik"] == "0000320193"
    assert result["company_name"] == "Apple Inc."
    assert result["accession_no"] == "0000320193-25-000079"   # from adsh of primary 10-K
    assert result["most_recent_filing"] == "2025-10-31"       # primary 10-K date, not exhibit
    assert result["most_recent_10k"] == "2025-10-31"          # backwards-compat alias (10-K only)
    assert result["filing_type"] == "10-K"
    assert result["name_match"] == "exact"


@pytest.mark.asyncio
async def test_find_company_cik_from_ciks_list():
    """CIK must come from _source.ciks[0] — not parsed from accession numbers."""
    fixture = {
        "hits": {
            "total": {"value": 1},
            "hits": [{
                "_source": {
                    "ciks": ["0000320193"],
                    "display_names": ["Apple Inc.  (AAPL)  (CIK 0000320193)"],
                    "adsh": "0000320193-25-000079",
                    "file_date": "2025-10-31",
                    "period_ending": "2025-09-27",
                    "form": "10-K",
                    "file_type": "10-K",
                    "root_forms": ["10-K"],
                    "items": [],
                }
            }]
        }
    }
    resp = MagicMock()
    resp.json.return_value = fixture
    resp.raise_for_status = MagicMock()
    with patch("src.sources.edgar._rate_limited_get", new=AsyncMock(return_value=resp)):
        result = json.loads(await edgar_find_company("Apple Inc."))

    assert result["cik"] == "0000320193"
    assert result["accession_no"] == "0000320193-25-000079"


@pytest.mark.asyncio
async def test_find_company_filters_to_primary_docs_not_exhibits():
    """Primary-doc filter must select the 10-K filing, not an exhibit.

    The fixture has two hits for the same company:
      - An EX-23.1 exhibit (higher relevance score)
      - The primary 10-K doc (lower score, same date)

    Without the primary-doc filter, the exhibit scores higher and would win.
    With the filter, only the 10-K survives.
    """
    mock_resp = _mock_response("search_aapl.json")
    with patch("src.sources.edgar._rate_limited_get", new=AsyncMock(return_value=mock_resp)):
        result = json.loads(await edgar_find_company("Apple Inc."))

    assert result["found"] is True
    assert result["cik"] == "0000320193"
    # Must be the primary 10-K adsh — exhibit has the same adsh but would be
    # selected by raw score; primary-doc filter ensures 10-K wins
    assert result["accession_no"] == "0000320193-25-000079"


@pytest.mark.asyncio
async def test_find_company_rejects_mention_only_match():
    """Filer verification must reject hits where a different company filed the doc.

    Live proof: 'Space Exploration Technologies' with forms=10-K returns 153 hits,
    all from Iridium Communications Inc. (CIK 0001418819) — not SpaceX. Without
    filer verification, the top primary doc would resolve to Iridium's CIK.

    PR3: _resolve_hits now returns a verification_failed diagnostic dict (not None)
    so the final not-found note names the actual filer, making the rejection
    diagnosable at a glance without re-running the search.
    """
    mock_resp = _mock_response("search_spacex_mention_only.json")
    # return_value is used for all three passes (same Iridium fixture every call)
    with patch("src.sources.edgar._rate_limited_get", new=AsyncMock(return_value=mock_resp)):
        result = json.loads(await edgar_find_company("Space Exploration Technologies"))

    assert result["found"] is False
    assert result.get("cik") is None
    # PR3: the note is now diagnostic — names the rejected filer and explains why
    assert "filer-verification rejected" in result.get("note", "").lower()
    assert "Iridium" in result.get("note", "")


@pytest.mark.asyncio
async def test_find_company_spacex_s1_resolves_correct_filer():
    """SpaceX S-1/A fixture resolves to CIK 0001181412 via the S-1 pass (2b).

    Three-pass flow (PR2):
      Pass 1 (10-K): empty → None
      Pass 2a (424B): empty → None
      Pass 2b (S-1): search_spacex_s1.json → SpaceX S-1/A, filer check passes → found

    'SPACE EXPLORATION TECHNOLOGIES CORP' contains 'Space Exploration Technologies'
    (bidirectional substring) → filer verification passes → found=True.
    """
    empty_resp = MagicMock()
    empty_resp.json.return_value = {"hits": {"total": {"value": 0}, "hits": []}}
    empty_resp.raise_for_status = MagicMock()
    s1_resp = _mock_response("search_spacex_s1.json")
    with patch(
        "src.sources.edgar._rate_limited_get",
        # Pass 1 (10-K) empty, Pass 2a (424B) empty, Pass 2b (S-1) finds SpaceX
        new=AsyncMock(side_effect=[empty_resp, empty_resp, s1_resp]),
    ):
        result = json.loads(await edgar_find_company("Space Exploration Technologies"))

    assert result["found"] is True
    assert result["cik"] == "0001181412"
    assert result["name_match"] == "partial"  # query is substring of entity name
    assert result["filing_type"] == "S-1/A"
    assert result["most_recent_filing"] == "2026-06-03"
    assert "most_recent_10k" not in result  # not a 10-K filing


@pytest.mark.asyncio
async def test_find_company_disambiguation_picks_most_recent_exact_match():
    """With two hits, Apple Inc. beats Apple REIT Nine on exact name match.

    Fixture lists Apple REIT Nine first (higher relevance score in EFTS results),
    Apple Inc. second. The sort key (exact_match DESC, file_date DESC) must
    promote Apple Inc. because its entity name exactly matches the query.
    """
    mock_resp = _mock_response("search_apple_disambiguation.json")
    with patch("src.sources.edgar._rate_limited_get", new=AsyncMock(return_value=mock_resp)):
        result_str = await edgar_find_company("Apple Inc.")
    result = json.loads(result_str)

    assert result["found"] is True
    assert result["company_name"] == "Apple Inc."
    assert result["cik"] == "0000320193"
    assert result["name_match"] == "exact"
    assert result["cik"] != "0001418121"


@pytest.mark.asyncio
async def test_find_company_partial_match_flagged():
    """When the entity name does not exactly match the query, name_match='partial'."""
    fixture = {
        "hits": {
            "total": {"value": 1},
            "hits": [{
                "_source": {
                    "ciks": ["0001418121"],
                    "display_names": ["Apple Hospitality REIT, Inc.  (APLE)  (CIK 0001418121)"],
                    "adsh": "0001418121-24-000010",
                    "file_date": "2024-03-01",
                    "period_ending": "2023-12-31",
                    "form": "10-K",
                    "file_type": "10-K",
                    "root_forms": ["10-K"],
                    "items": [],
                }
            }]
        }
    }
    resp = MagicMock()
    resp.json.return_value = fixture
    resp.raise_for_status = MagicMock()
    with patch("src.sources.edgar._rate_limited_get", new=AsyncMock(return_value=resp)):
        result = json.loads(await edgar_find_company("Apple"))

    # "Apple" is a substring of "Apple Hospitality REIT, Inc." → filer passes, partial match
    assert result["found"] is True
    assert result["name_match"] == "partial"
    assert "partial" in result.get("note", "").lower() or "verify" in result.get("note", "").lower()


@pytest.mark.asyncio
async def test_find_company_ambiguous_query_returns_error():
    """When multiple distinct CIKs pass filer verification on a partial match,
    edgar_find_company must NOT silently pick the first — it returns found=False
    with error='ambiguous' and lists the matching CIKs.

    Fixture: 'Apple' (bare name) matches both Apple Inc. (CIK 0000320193) and
    Apple REIT Nine (CIK 0001418121). Neither is an exact match for 'Apple'.
    The caller should use 'Apple Inc.' to get a unique resolution.
    """
    mock_resp = _mock_response("search_apple_ambiguous.json")
    with patch("src.sources.edgar._rate_limited_get", new=AsyncMock(return_value=mock_resp)):
        result = json.loads(await edgar_find_company("Apple"))

    assert result["found"] is False
    assert result.get("error") == "ambiguous"
    assert "0000320193" in result.get("matching_ciks", [])
    assert "0001418121" in result.get("matching_ciks", [])
    # Note must tell caller to use a more specific name
    note = result.get("note", "")
    assert "Inc." in note or "full" in note.lower() or "specific" in note.lower()


@pytest.mark.asyncio
async def test_find_company_exact_match_not_ambiguous():
    """An exact name match resolves cleanly even when other partial-match CIKs exist.

    Query 'Apple Inc.' exactly matches Apple Inc.'s entity name — the exact match
    wins without triggering the ambiguity guard, even though 'Apple REIT Nine'
    also contains 'apple'.
    """
    mock_resp = _mock_response("search_apple_ambiguous.json")
    with patch("src.sources.edgar._rate_limited_get", new=AsyncMock(return_value=mock_resp)):
        result = json.loads(await edgar_find_company("Apple Inc."))

    assert result["found"] is True
    assert result["cik"] == "0000320193"
    assert result["name_match"] == "exact"
    assert result.get("error") != "ambiguous"


@pytest.mark.asyncio
async def test_find_company_not_found_returns_found_false():
    resp = MagicMock()
    resp.json.return_value = {"hits": {"hits": [], "total": {"value": 0}}}
    resp.raise_for_status = MagicMock()
    with patch("src.sources.edgar._rate_limited_get", new=AsyncMock(return_value=resp)):
        result_str = await edgar_find_company("Definitely Not A Company XYZ")
    result = json.loads(result_str)
    assert result["found"] is False


# ── edgar_get_financials ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_financials_aapl_uses_revenue_from_contract_key():
    """Apple FY2025: revenue from RevenueFromContractWithCustomerExcludingAssessedTax.

    Apple deprecated us-gaap.Revenues after FY2018 and now uses
    RevenueFromContractWithCustomerExcludingAssessedTax. The fallback chain
    correctly skips the empty Revenues key and uses the contract-revenue key.

    Regression anchor (live-verified 2026-06-11): FY2025 revenue = 416,161,000,000.
    """
    mock_resp = _mock_response("companyfacts_aapl.json")
    with patch("src.sources.edgar._rate_limited_get", new=AsyncMock(return_value=mock_resp)):
        result_str = await edgar_get_financials("0000320193")
    result = json.loads(result_str)
    assert result["revenue_key_used"] == (
        "us-gaap.RevenueFromContractWithCustomerExcludingAssessedTax"
    ), (
        f"Expected RevenueFromContract key (Apple deprecated Revenues after FY2018); "
        f"got: {result['revenue_key_used']!r}"
    )
    assert result["revenue"]["value_usd"] == 416_161_000_000
    assert result["revenue"]["formatted"] == "$416.16B"
    assert result["revenue"]["fiscal_year"] == "2025"
    assert result["net_income"]["value_usd"] == 112_010_000_000


@pytest.mark.asyncio
async def test_get_financials_jpm_uses_bank_fallback_chain():
    """JPMorgan: no Revenues key → should fall back to Interest + Noninterest income.

    This explicitly asserts the entire fallback chain:
    us-gaap.Revenues                              → not found
    us-gaap.RevenueFromContract...                → not found
    us-gaap.InterestAndDividendIncomeOperating
      + us-gaap.NoninterestIncome                 → used
    """
    mock_resp = _mock_response("companyfacts_jpm.json")
    with patch("src.sources.edgar._rate_limited_get", new=AsyncMock(return_value=mock_resp)):
        result_str = await edgar_get_financials("0000019617")
    result = json.loads(result_str)

    assert result["revenue_key_used"] == (
        "us-gaap.InterestAndDividendIncomeOperating + us-gaap.NoninterestIncome"
    ), (
        f"Expected bank fallback key, got: {result['revenue_key_used']!r}. "
        f"Keys attempted: {result.get('revenue_keys_attempted')}"
    )

    expected_combined = 89_272_000_000 + 69_380_000_000
    assert result["revenue"]["value_usd"] == expected_combined
    assert result["revenue"]["fiscal_year"] == "2023"
    assert result["net_income"]["value_usd"] == 49_552_000_000

    attempted = result["revenue_keys_attempted"]
    assert "us-gaap.Revenues" in attempted
    assert "us-gaap.RevenueFromContractWithCustomerExcludingAssessedTax" in attempted
    assert "us-gaap.InterestAndDividendIncomeOperating + us-gaap.NoninterestIncome" in attempted


@pytest.mark.asyncio
async def test_get_financials_most_recent_annual_is_selected():
    """Confirms _extract_two_most_recent_annual picks FY2025 over FY2024 for Apple."""
    mock_resp = _mock_response("companyfacts_aapl.json")
    with patch("src.sources.edgar._rate_limited_get", new=AsyncMock(return_value=mock_resp)):
        result_str = await edgar_get_financials("0000320193")
    result = json.loads(result_str)
    assert result["revenue"]["fiscal_year"] == "2025"
    assert result["revenue"]["period_end"] == "2025-09-27"


@pytest.mark.asyncio
async def test_get_financials_prior_year_extracted():
    """Apple FY2024 is the prior year for the FY2025 revenue, enabling YoY growth.

    Regression anchor: prior_revenue.value_usd == 391,035,000,000 (FY2024).
    The companyfacts series has comparative duplicates (same period in multiple
    10-K filings); _extract_two_most_recent_annual deduplicates by period end date
    so FY2024 and FY2025 10-K appearances of the same period collapse to one.
    """
    mock_resp = _mock_response("companyfacts_aapl.json")
    with patch("src.sources.edgar._rate_limited_get", new=AsyncMock(return_value=mock_resp)):
        result_str = await edgar_get_financials("0000320193")
    result = json.loads(result_str)

    assert "prior_revenue" in result, "prior_revenue must be present when ≥2 annual years available"
    assert result["prior_revenue"]["value_usd"] == 391_035_000_000
    assert result["prior_revenue"]["formatted"] == "$391.04B"
    assert result["prior_revenue"]["fiscal_year"] == "2024"

    assert "revenue_growth_pct" in result
    pct = result["revenue_growth_pct"]
    # (416161 - 391035) / 391035 * 100 ≈ 6.43 %
    assert abs(pct - 6.43) < 0.1, f"Expected ~6.43% growth, got {pct:.4f}%"


@pytest.mark.asyncio
async def test_get_financials_single_year_no_prior():
    """When only one annual observation exists, prior_revenue and revenue_growth_pct are absent."""
    import copy

    mock_resp_base = _mock_response("companyfacts_aapl.json")
    base_data = mock_resp_base.json()

    # Trim the RevenueFromContract series to a single observation (latest only)
    key = "RevenueFromContractWithCustomerExcludingAssessedTax"
    trimmed = copy.deepcopy(base_data)
    units = trimmed["facts"]["us-gaap"][key]["units"]["USD"]
    fy25_only = [u for u in units if u.get("end") == "2025-09-27"]
    trimmed["facts"]["us-gaap"][key]["units"]["USD"] = fy25_only

    single_resp = MagicMock()
    single_resp.json = lambda: trimmed
    single_resp.raise_for_status = lambda: None

    with patch("src.sources.edgar._rate_limited_get", new=AsyncMock(return_value=single_resp)):
        result_str = await edgar_get_financials("0000320193")
    result = json.loads(result_str)

    assert result["revenue"]["value_usd"] == 416_161_000_000, "Latest-year revenue must still resolve"
    assert "prior_revenue" not in result, "prior_revenue must be absent when only one annual year exists"
    assert "revenue_growth_pct" not in result, "revenue_growth_pct must be absent when prior is unavailable"


@pytest.mark.asyncio
async def test_get_financials_bank_fallback_includes_prior_year():
    """JPM bank fallback: both interest+noninterest have prior-year data → growth_pct computed."""
    mock_resp = _mock_response("companyfacts_jpm.json")
    with patch("src.sources.edgar._rate_limited_get", new=AsyncMock(return_value=mock_resp)):
        result_str = await edgar_get_financials("0000019617")
    result = json.loads(result_str)

    assert "prior_revenue" in result, "prior_revenue must be present for JPM (both concepts have ≥2 years)"
    # FY2022: Interest 72098B + Noninterest 57990B = 130088B
    assert result["prior_revenue"]["value_usd"] == 89_272_000_000 + 69_380_000_000 - (
        # FY2023 is latest; FY2022 is prior: 72098 + 57990
        89_272_000_000 + 69_380_000_000 - (72_098_000_000 + 57_990_000_000)
    ), "prior_revenue for JPM should be FY2022 sum"
    # Simpler check:
    assert result["prior_revenue"]["value_usd"] == 72_098_000_000 + 57_990_000_000
    assert result["prior_revenue"]["fiscal_year"] == "2022"
    assert "revenue_growth_pct" in result


# ── End-to-end merge test: discovery → financials → assembler ─────────────────

@pytest.mark.asyncio
async def test_edgar_apple_end_to_end_merge():
    """Full pipeline: edgar_find_company → edgar_get_financials → assemble_report.

    This is the first test to exercise the EDGAR → assembler merge path on real
    Apple data (CIK 0000320193). All five prior production runs returned
    edgar_lookup_status ≠ succeeded; the EDGAR merge path was never exercised
    on real data before the PR1 field-mapping fix.

    Regression anchors (live-verified 2026-06-11):
      - CIK resolves to 0000320193
      - FY2025 revenue = 416,161,000,000 from RevenueFromContractWithCustomer...
      - doc.financial.revenue lands at primary_document tier
      - run_metadata records edgar_lookup_status=succeeded, edgar_cik=0000320193
    """
    from src.synthesis.assembler import assemble_report

    # Step 1: edgar_find_company — fixture: exhibit (filtered out) + primary 10-K
    search_resp = _mock_response("search_aapl.json")
    with patch("src.sources.edgar._rate_limited_get", new=AsyncMock(return_value=search_resp)):
        find_result = json.loads(await edgar_find_company("Apple Inc."))

    assert find_result["found"] is True
    assert find_result["cik"] == "0000320193"

    # Step 2: edgar_get_financials with the resolved CIK
    facts_resp = _mock_response("companyfacts_aapl.json")
    with patch("src.sources.edgar._rate_limited_get", new=AsyncMock(return_value=facts_resp)):
        fin_result = json.loads(await edgar_get_financials(find_result["cik"]))

    assert "error" not in fin_result
    assert fin_result["revenue"] is not None
    assert fin_result["revenue"]["value_usd"] == 416_161_000_000
    assert fin_result["revenue"]["formatted"] == "$416.16B"

    # Step 3: build edgar_data via CompanyEdgarFinancials.model_dump() — the exact
    # path the EDGAR agent takes. This confirms the Pydantic model serialisation
    # round-trips correctly through the assembler merge.
    from src.schemas.models import CompanyEdgarFinancials, ConfidenceLevel, DataPoint
    companyfacts_url = fin_result["source_url"]  # https://data.sec.gov/...
    revenue_label = (
        f"{fin_result['revenue']['formatted']} "
        f"(FY{fin_result['revenue']['fiscal_year']})"
    )
    edgar_model = CompanyEdgarFinancials(
        company_name="Apple Inc.",
        cik=find_result["cik"],
        is_sec_reporting=True,
        edgar_lookup_status="succeeded",
        revenue=DataPoint(
            value=revenue_label,
            confidence=ConfidenceLevel.HIGH,
            sources=[companyfacts_url],
            reasoning=f"From {fin_result['revenue_key_used']}",
        ),
        profitability=DataPoint(
            value=(
                f"Net income {fin_result['net_income']['formatted']} "
                f"(FY{fin_result['revenue']['fiscal_year']})"
            ),
            confidence=ConfidenceLevel.HIGH,
            sources=[companyfacts_url],
        ),
        fiscal_year_end=DataPoint(
            value=fin_result["revenue"].get("period_end", "unknown"),
            confidence=ConfidenceLevel.HIGH,
            sources=[companyfacts_url],
        ),
        most_recent_filing=DataPoint(
            value=f"10-K filed {find_result['most_recent_10k']}",
            confidence=ConfidenceLevel.HIGH,
            sources=[companyfacts_url],
        ),
        sec_risk_factors=[],
    )
    edgar_data = edgar_model.model_dump()

    # Step 4: assemble report. Financial agent returns "unknown" for public companies
    # (the standard behaviour for is_likely_public=True); EDGAR overlays the real values.
    financial_data = {
        "company_name": "Apple Inc.",
        "revenue": {"value": "unknown", "confidence": "unknown", "sources": [], "derived": False, "derived_from": []},
        "revenue_growth": {"value": "unknown", "confidence": "unknown", "sources": [], "derived": False, "derived_from": []},
        "profitability": {"value": "unknown", "confidence": "unknown", "sources": [], "derived": False, "derived_from": []},
        "total_funding": {"value": "unknown", "confidence": "unknown", "sources": [], "derived": False, "derived_from": []},
        "last_funding_round": {"value": "unknown", "confidence": "unknown", "sources": [], "derived": False, "derived_from": []},
        "valuation": {"value": "unknown", "confidence": "unknown", "sources": [], "derived": False, "derived_from": []},
        "revenue_model": {"value": "unknown", "confidence": "unknown", "sources": [], "derived": False, "derived_from": []},
        "key_investors": [],
        "key_customers": [],
        "financial_risks": [],
        "recent_financial_events": [],
    }
    trace_summary = {
        "trace_id": "test000000001",
        "total_cost_usd": 0.0,
        "total_duration_ms": 0.0,
        "total_llm_calls": 0,
        "total_tool_calls": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "agents": {},
    }

    doc = assemble_report(
        research_data=None,
        financial_data=financial_data,
        risk_data=None,
        social_media_data=None,
        synthesis_data=None,
        trace_summary=trace_summary,
        edgar_data=edgar_data,
    )

    # Step 5: assert the merge worked
    assert doc.run_metadata.edgar_lookup_status == "succeeded"
    assert doc.run_metadata.edgar_cik == "0000320193"

    assert doc.financial is not None
    assert doc.financial.revenue is not None

    # EDGAR revenue at primary_document tier — the key provenance assertion
    rev_claim = doc.financial.revenue
    assert rev_claim.value == "$416.16B (FY2025)"
    assert rev_claim.agent == "edgar"
    assert rev_claim.confidence.value == "high"
    assert len(rev_claim.sources) > 0
    assert all(s.tier.value == "primary_document" for s in rev_claim.sources), (
        f"EDGAR revenue sources must be primary_document; got: "
        f"{[s.tier.value for s in rev_claim.sources]}"
    )
    assert any("data.sec.gov" in s.url for s in rev_claim.sources)

    # Profitability also merged
    assert doc.financial.profitability is not None
    assert doc.financial.profitability.agent == "edgar"


# ── PR2: S-1/424B SpaceX tests ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_find_company_spacex_resolves_via_424b4():
    """SpaceX 424B4 fixture resolves to CIK 0001181412 via the prospectus pass (2a).

    Three-pass flow:
      Pass 1 (10-K): empty hits → None
      Pass 2a (424B): search_spacex_424b4.json → 424B4 hit → found=True
      Pass 2b (S-1): never reached

    filing_type must be '424B4' (the priced prospectus, the most recent filing).
    Regression anchor (live-captured 2026-06-13): accession 0001628280-26-042639.
    """
    empty_resp = MagicMock()
    empty_resp.json.return_value = {"hits": {"total": {"value": 0}, "hits": []}}
    empty_resp.raise_for_status = MagicMock()
    b4_resp = _mock_response("search_spacex_424b4.json")
    with patch(
        "src.sources.edgar._rate_limited_get",
        # Pass 1 (10-K) empty, Pass 2a (424B) finds SpaceX, Pass 2b never reached
        new=AsyncMock(side_effect=[empty_resp, b4_resp]),
    ):
        result = json.loads(await edgar_find_company("Space Exploration Technologies"))

    assert result["found"] is True
    assert result["cik"] == "0001181412"
    assert result["filing_type"] == "424B4"
    assert result["most_recent_filing"] == "2026-06-12"
    assert result["accession_no"] == "0001628280-26-042639"
    assert result["name_match"] == "partial"
    assert "most_recent_10k" not in result  # 424B4 is not a 10-K


@pytest.mark.asyncio
async def test_get_financials_empty_xbrl_not_error():
    """companyfacts with empty XBRL (brand-new IPO filer) returns xbrl_available=false.

    SpaceX's companyfacts returns HTTP 200 with entity info but no us-gaap facts.
    This must NOT be treated as an error or lookup_failed — the company IS an SEC
    filer; the XBRL aggregation just hasn't happened yet. The caller must gap the
    financials (revenue=null, net_income=null) and cite the filing.

    Regression anchor (live-captured 2026-06-13): CIK 0001181412.
    """
    mock_resp = _mock_response("companyfacts_spacex_empty.json")
    with patch("src.sources.edgar._rate_limited_get", new=AsyncMock(return_value=mock_resp)):
        result = json.loads(await edgar_get_financials("0001181412"))

    assert "error" not in result, f"Unexpected error: {result.get('error')}"
    assert result["xbrl_available"] is False
    assert result["revenue"] is None
    assert result["net_income"] is None
    assert result["entity_name"] == "SPACE EXPLORATION TECHNOLOGIES CORP"


@pytest.mark.asyncio
async def test_filing_text_424b4_document_type_accepted():
    """edgar_get_filing_text must accept '424B4' as a primary filing document type.

    The filing index for the SpaceX 424B4 (accession 0001628280-26-042639)
    lists the primary doc with type='424B4'. The function must select it (not
    fall back to the .htm-extension fallback) and return the Risk Factors section.

    Risk Factors in 424B4 use a standalone 'RISK FACTORS' heading (no Item 1A),
    and the section ends at 'CAUTIONARY STATEMENT REGARDING FORWARD' — the
    _NEXT_SECTION pattern must terminate correctly.
    """
    from src.sources.edgar import edgar_get_filing_text

    index_resp = MagicMock()
    index_resp.raise_for_status = MagicMock()
    index_resp.json.return_value = {
        "directory": {
            "item": [
                {"name": "spaceexplorationtechnologi.htm", "type": "424B4"},
                {"name": "exhibit51.htm", "type": "EX-5.1"},
            ]
        }
    }

    html = (
        "<html><body>"
        "<p>Table of Contents</p>"
        "<p>RISK FACTORS Investing in our Class A common stock involves a high "
        "degree of risk. You should carefully consider the risks and "
        "uncertainties described below. Risks Related to Our Business "
        "Failure to develop Starship would materially harm our business. "
        "Risks Related to Regulation Government export controls may limit "
        "our ability to serve international customers.</p>"
        "<p>CAUTIONARY STATEMENT REGARDING FORWARD-LOOKING STATEMENTS</p>"
        "<p>USE OF PROCEEDS</p>"
        "</body></html>"
    )
    doc_resp = MagicMock()
    doc_resp.raise_for_status = MagicMock()
    doc_resp.text = html

    with patch(
        "src.sources.edgar._rate_limited_get",
        new=AsyncMock(side_effect=[index_resp, doc_resp]),
    ):
        result_str = await edgar_get_filing_text(
            cik="0001181412",
            accession_no="0001628280-26-042639",
            section="risk_factors",
        )

    result = json.loads(result_str)
    assert "error" not in result, f"Unexpected error: {result}"
    assert result["extraction_status"] == "extracted"
    assert result["filing_form_type"] == "424B4"
    text = result["text"]
    assert "RISK FACTORS" in text.upper()
    # Section must be cut before CAUTIONARY STATEMENT
    assert "CAUTIONARY STATEMENT" not in text.upper()
    assert "USE OF PROCEEDS" not in text.upper()


@pytest.mark.asyncio
async def test_filing_text_section_not_found_returns_explicit_status():
    """edgar_get_filing_text returns extraction_status='section_not_found' when the
    document has no recognizable risk_factors heading."""
    from src.sources.edgar import edgar_get_filing_text

    index_resp = MagicMock()
    index_resp.raise_for_status = MagicMock()
    index_resp.json.return_value = {
        "directory": {"item": [{"name": "filing.htm", "type": "10-K"}]}
    }
    # Document with NO risk factors heading — only generic text
    html = "<html><body><p>Annual Report. Financial overview. See exhibits.</p></body></html>"
    doc_resp = MagicMock()
    doc_resp.raise_for_status = MagicMock()
    doc_resp.text = html

    with patch(
        "src.sources.edgar._rate_limited_get",
        new=AsyncMock(side_effect=[index_resp, doc_resp]),
    ):
        result_str = await edgar_get_filing_text(
            cik="0000320193",
            accession_no="0001193125-24-018800",
            section="risk_factors",
        )

    result = json.loads(result_str)
    assert "error" not in result
    assert result["extraction_status"] == "section_not_found"
    assert "text" not in result
    assert "note" in result
    assert result["filing_form_type"] == "10-K"


@pytest.mark.asyncio
async def test_edgar_spacex_merge_path_succeeded_with_gapped_financials():
    """SpaceX EDGAR merge: succeeded status, risk factors present, revenue gapped.

    This tests the assembler merge path for a company whose EDGAR status is
    'succeeded' but financials are unknown (XBRL not yet aggregated).

    Key assertions:
      - doc.run_metadata.edgar_lookup_status == "succeeded"
      - doc.financial.revenue stays "unknown" (no EDGAR override when revenue=None)
      - sec_risk_factors from S-1/424B land in doc.risk.regulatory_risks
      - edgar_cik is set correctly
    """
    from src.synthesis.assembler import assemble_report
    from src.schemas.models import (
        CompanyEdgarFinancials, ConfidenceLevel, DataPoint,
    )

    # Simulate what EdgarAgent produces for SpaceX:
    # - found via 424B4, XBRL not yet available, but risk factors extracted
    filing_url = (
        "https://www.sec.gov/Archives/edgar/data/1181412/"
        "000162828026042639/spaceexplorationtechnologi.htm"
    )
    companyfacts_url = (
        "https://data.sec.gov/api/xbrl/companyfacts/CIK0001181412.json"
    )
    gap_note = (
        "XBRL not yet available in companyfacts for 424B4 filer; "
        "accession 0001628280-26-042639"
    )
    edgar_model = CompanyEdgarFinancials(
        company_name="Space Exploration Technologies Corp",
        cik="0001181412",
        is_sec_reporting=True,
        edgar_lookup_status="succeeded",
        # xbrl_available=False → agent gaps both fields (value="unknown")
        revenue=DataPoint(
            value="unknown", confidence=ConfidenceLevel.UNKNOWN,
            sources=[companyfacts_url], reasoning=gap_note,
        ),
        profitability=DataPoint(
            value="unknown", confidence=ConfidenceLevel.UNKNOWN,
            sources=[companyfacts_url], reasoning=gap_note,
        ),
        fiscal_year_end=DataPoint(
            value="S-1/424B4 filer — no annual period yet",
            confidence=ConfidenceLevel.MEDIUM,
            sources=[filing_url],
            reasoning="424B4 (priced prospectus) filed 2026-06-12; no 10-K yet",
        ),
        most_recent_filing=DataPoint(
            value="424B4 filed 2026-06-12",
            confidence=ConfidenceLevel.HIGH,
            sources=[filing_url],
            reasoning="From edgar_find_company: filing_type=424B4",
        ),
        sec_risk_factors=[
            DataPoint(
                value=(
                    "Failure to develop Starship at scale would delay our growth "
                    "strategy and materially harm our business."
                ),
                confidence=ConfidenceLevel.HIGH,
                sources=[filing_url],
                reasoning="Risk factor from 424B4 (priced prospectus) filed 2026-06-12",
            ),
        ],
    )
    edgar_data = edgar_model.model_dump()

    financial_data = {
        "company_name": "Space Exploration Technologies Corp",
        **{k: {"value": "unknown", "confidence": "unknown", "sources": [], "derived": False, "derived_from": []}
           for k in ("revenue", "revenue_growth", "profitability", "total_funding",
                     "last_funding_round", "valuation", "revenue_model")},
        "key_investors": [], "key_customers": [], "financial_risks": [],
        "recent_financial_events": [],
    }
    trace_summary = {
        "trace_id": "test_spacex_001",
        "total_cost_usd": 0.0, "total_duration_ms": 0.0,
        "total_llm_calls": 0, "total_tool_calls": 0,
        "total_input_tokens": 0, "total_output_tokens": 0, "agents": {},
    }

    # Provide minimal risk_data so the risk section exists for the EDGAR merge
    risk_data = {
        "company_name": "Space Exploration Technologies Corp",
        "regulatory_risks": [], "legal_risks": [], "cybersecurity_risks": [],
        "operational_risks": [], "reputational_risks": [], "esg_risks": [],
        "pending_litigation": [], "notable_federal_contracts": [],
    }

    doc = assemble_report(
        research_data=None, financial_data=financial_data,
        risk_data=risk_data, social_media_data=None, synthesis_data=None,
        trace_summary=trace_summary, edgar_data=edgar_data,
    )

    # EDGAR succeeded — CIK tracked
    assert doc.run_metadata.edgar_lookup_status == "succeeded"
    assert doc.run_metadata.edgar_cik == "0001181412"

    # Revenue stays "unknown" — no EDGAR override when revenue=None
    assert doc.financial.revenue is None or doc.financial.revenue.value == "unknown", (
        "When EDGAR revenue is None, the assembler must not fabricate a value"
    )

    # Risk factors from S-1/424B land in regulatory_risks
    assert doc.risk is not None
    rf = doc.risk.regulatory_risks
    assert len(rf) >= 1, "SEC risk factors from 424B4 must appear in regulatory_risks"
    assert any(
        "starship" in c.value.lower() or "424b4" in (c.reasoning or "").lower()
        for c in rf
    ), "Risk factor claim must reference Starship or 424B4 provenance"

    # Risk factor must be at primary_document tier (sec.gov source)
    rf0 = rf[0]
    assert all(s.tier.value == "primary_document" for s in rf0.sources), (
        f"SEC risk factor must be primary_document tier; got: "
        f"{[s.tier.value for s in rf0.sources]}"
    )


# ── PR3: name-resolution cascade (ticker + legal_name) ───────────────────────

@pytest.mark.asyncio
async def test_find_company_brand_name_with_legal_name():
    """Brand name 'SpaceX' + legal_name resolves via the legal_name EFTS path.

    This is the pipeline-realistic test: edgar_find_company receives "SpaceX"
    (what the user types and the pipeline passes) but also gets the classifier-
    provided legal_name "Space Exploration Technologies Corp". The function uses
    the legal name for EFTS search so filer-verification passes.

    Three-call flow (step b — legal_name EFTS search):
      Pass 1 (10-K) with legal_name: empty → None
      Pass 2a (424B) with legal_name: 424B4 fixture → filer check passes → found

    name_match is 'exact' because the EFTS entity 'SPACE EXPLORATION TECHNOLOGIES
    CORP' matches 'Space Exploration Technologies Corp' (case-insensitive).
    """
    empty_resp = MagicMock()
    empty_resp.json.return_value = {"hits": {"total": {"value": 0}, "hits": []}}
    empty_resp.raise_for_status = MagicMock()
    b4_resp = _mock_response("search_spacex_424b4.json")
    with patch(
        "src.sources.edgar._rate_limited_get",
        # legal_name path: Pass 1 empty, Pass 2a finds 424B4 → return; brand-name path never reached
        new=AsyncMock(side_effect=[empty_resp, b4_resp]),
    ):
        result = json.loads(await edgar_find_company(
            "SpaceX",
            legal_name="Space Exploration Technologies Corp",
        ))

    assert result["found"] is True, f"Expected found=True; got {result}"
    assert result["cik"] == "0001181412"
    assert result["filing_type"] == "424B4"
    assert result["most_recent_filing"] == "2026-06-12"
    assert result["accession_no"] == "0001628280-26-042639"
    # Legal name is an exact case-insensitive match for the EFTS entity name
    assert result["name_match"] == "exact"
    assert "most_recent_10k" not in result


@pytest.mark.asyncio
async def test_find_company_via_ticker():
    """Ticker 'SPCX' resolves SpaceX to CIK 0001181412 without any EFTS search.

    Step a of the resolution cascade: ticker → company_tickers.json → CIK →
    submissions API → filing info. No EFTS name matching is attempted because
    we have the CIK directly from the ticker.

    Two HTTP calls:
      1. company_tickers.json (ticker lookup)
      2. submissions API (filing info)
    """
    tickers_resp = MagicMock()
    tickers_resp.raise_for_status = MagicMock()
    tickers_resp.json.return_value = {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        "1": {"cik_str": 1181412, "ticker": "SPCX",
              "title": "SPACE EXPLORATION TECHNOLOGIES CORP"},
    }

    submissions_resp = MagicMock()
    submissions_resp.raise_for_status = MagicMock()
    submissions_resp.json.return_value = {
        "cik": "0001181412",
        "name": "SPACE EXPLORATION TECHNOLOGIES CORP",
        "tickers": ["SPCX"],
        "filings": {
            "recent": {
                "form": ["424B4", "S-1/A", "S-1"],
                "accessionNumber": [
                    "0001628280-26-042639",
                    "0001628280-26-030000",
                    "0001628280-26-010000",
                ],
                "filingDate": ["2026-06-12", "2026-06-03", "2026-01-01"],
            }
        },
    }

    with patch(
        "src.sources.edgar._rate_limited_get",
        new=AsyncMock(side_effect=[tickers_resp, submissions_resp]),
    ):
        result = json.loads(await edgar_find_company("SpaceX", ticker="SPCX"))

    assert result["found"] is True, f"Expected found=True; got {result}"
    assert result["cik"] == "0001181412"
    assert result["company_name"] == "SPACE EXPLORATION TECHNOLOGIES CORP"
    assert result["filing_type"] == "424B4"
    assert result["most_recent_filing"] == "2026-06-12"
    assert result["accession_no"] == "0001628280-26-042639"
    assert result["name_match"] == "ticker"
    assert "most_recent_10k" not in result  # 424B4 is not a 10-K


@pytest.mark.asyncio
async def test_find_company_ticker_fallthrough_to_legal_name():
    """When ticker lookup fails, fall through to legal_name EFTS search.

    Simulates the case where company_tickers.json doesn't contain the ticker
    (e.g. very recent listing not yet in the file). The function must fall
    through to step b (legal_name EFTS search) rather than returning not_found.
    """
    empty_tickers_resp = MagicMock()
    empty_tickers_resp.raise_for_status = MagicMock()
    # SPCX not in the file
    empty_tickers_resp.json.return_value = {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    }

    empty_efts = MagicMock()
    empty_efts.json.return_value = {"hits": {"total": {"value": 0}, "hits": []}}
    empty_efts.raise_for_status = MagicMock()
    b4_resp = _mock_response("search_spacex_424b4.json")

    with patch(
        "src.sources.edgar._rate_limited_get",
        # (1) company_tickers.json — SPCX not found
        # (2) legal_name Pass 1 (10-K) — empty
        # (3) legal_name Pass 2a (424B) — 424B4 found → return
        new=AsyncMock(side_effect=[empty_tickers_resp, empty_efts, b4_resp]),
    ):
        result = json.loads(await edgar_find_company(
            "SpaceX",
            ticker="SPCX",
            legal_name="Space Exploration Technologies Corp",
        ))

    assert result["found"] is True
    assert result["cik"] == "0001181412"
    assert result["name_match"] == "exact"  # legal_name matched exactly
