"""Live integration tests for EDGAR tool functions.

These tests make real HTTP calls to SEC EDGAR. They are skipped by default
(no network required for CI) and run on demand:

    EDGAR_LIVE_TESTS=1 pytest tests/test_edgar_live.py -v

Purpose: these are the tests that would have caught the Apple CIK resolution
bug on day one. They carry eight externally-verified regression anchors:
  1. Apple Inc. (forms=10-K)        → CIK 0000320193, FY2025 revenue 416,161,000,000
  2. Space Exploration Tech. (10-K) → Iridium CIK not returned (filer-verification trap)
  3. Space Exploration Tech. (S-1)  → CIK 0001181412 (deprecated; superseded by anchor 8)
  4. Apple Inc. end-to-end merge    → EDGAR financials at primary_document tier
  5. SpaceX via edgar_find_company  → CIK 0001181412 via 424B4 (PR2, legal-name path)
  6. SpaceX companyfacts            → xbrl_available=False, revenue=None (not an error)
  7. SpaceX risk factors from 424B4 → text present, section ends before CAUTIONARY STMT
  8. 'SpaceX' brand name resolves   → CIK 0001181412 via legal_name cascade (PR3)
                                       PIPELINE-REALISTIC: drives the exact input the
                                       orchestrator sends, not the legal name directly.

Use this file as a template for adding the equivalent live test for each
other Tier-0 tool (OpenCorporates, USPTO, SAM.gov) as part of the fixture-
audit follow-up.

Verification history:
  2026-06-11: Anchors 1-3 manually confirmed via edgar_live_check.py
              (direct SEC API calls, no repo imports, no fixtures).
  2026-06-11: Anchor 4 confirmed via test_edgar_apple_end_to_end_merge (offline)
              and test_live_apple_end_to_end_merge (this file, live).
  2026-06-13: Anchors 5-7 added after SpaceX IPO (June 12). Step 0 live probe
              confirmed 424B4 in EFTS, empty XBRL in companyfacts, Risk Factors
              extractable from 11.9MB 424B4 HTML.
  2026-06-13: Anchor 8 added (PR3 fix). Anchor 5 previously called edgar_find_company
              with the legal name directly — the pipeline never sends that. Anchor 8
              uses "SpaceX" (the brand name) with legal_name from the classifier,
              which is the realistic pipeline input.
"""

import json
import os
import pytest
import asyncio

# Skip all tests in this module unless --live flag or EDGAR_LIVE_TESTS env var
_LIVE = os.getenv("EDGAR_LIVE_TESTS", "").lower() in ("1", "true", "yes")
pytestmark = pytest.mark.skipif(
    not _LIVE,
    reason=(
        "Live EDGAR tests skipped (set EDGAR_LIVE_TESTS=1 to run). "
        "These tests hit the real SEC API and require network access."
    ),
)


@pytest.fixture(scope="module")
def event_loop_policy():
    return asyncio.DefaultEventLoopPolicy()


# ── Anchor 1: Apple Inc. — CIK + FY2025 revenue ──────────────────────────────

@pytest.mark.asyncio
async def test_live_apple_resolves_correct_cik():
    """Apple Inc. (forms=10-K) must resolve to CIK 0000320193.

    This is the canonical anchor from Apple's own SEC 8-K filings.
    Confirms: primary-doc filter works, filer verification passes, ciks[0] used.
    """
    from src.sources.edgar import edgar_find_company
    result = json.loads(await edgar_find_company("Apple Inc."))
    assert result["found"] is True, f"Apple not found: {result}"
    assert result["cik"] == "0000320193", (
        f"Expected CIK 0000320193, got {result['cik']!r}. "
        f"Company name resolved: {result.get('company_name')!r}"
    )
    assert result["name_match"] == "exact"
    assert result["company_name"] == "Apple Inc."
    assert result["filing_type"] == "10-K"
    assert "most_recent_filing" in result
    assert result.get("most_recent_10k") == result["most_recent_filing"]  # compat alias


@pytest.mark.asyncio
async def test_live_apple_financials_fy2025_revenue():
    """Apple FY2025 companyfacts revenue must equal 416,161,000,000.

    Live-verified 2026-06-11. Apple uses RevenueFromContractWithCustomer
    since FY2019; the Revenues key is only populated through FY2018.
    """
    from src.sources.edgar import edgar_get_financials
    result = json.loads(await edgar_get_financials("0000320193"))
    assert "error" not in result, f"edgar_get_financials error: {result}"
    assert result["revenue"] is not None
    assert result["revenue"]["value_usd"] == 416_161_000_000, (
        f"Expected FY2025 revenue 416,161,000,000; got {result['revenue']['value_usd']:,}. "
        f"Key used: {result.get('revenue_key_used')!r}. "
        f"Apple may have filed a newer 10-K — update the anchor if FY has advanced."
    )
    assert result["revenue_key_used"] == (
        "us-gaap.RevenueFromContractWithCustomerExcludingAssessedTax"
    ), (
        f"Apple uses RevenueFromContract since FY2019; Revenues key is FY2018 only. "
        f"Got: {result['revenue_key_used']!r}"
    )


# ── Anchor 4: Apple end-to-end merge (headline of PR1) ───────────────────────

@pytest.mark.asyncio
async def test_live_apple_end_to_end_merge():
    """Full chain: edgar_find_company → edgar_get_financials → assemble_report.

    This is the live counterpart of test_edgar_apple_end_to_end_merge in
    test_edgar_tools.py. It exercises the full merge path against the real
    SEC API; the offline test uses mocked HTTP with real-captured fixtures.

    PR1 headline: EDGAR financials must reach the assembled ReportDocument
    at primary_document tier. This test proves the claim holds on live data.
    """
    from src.sources.edgar import edgar_find_company, edgar_get_financials
    from src.synthesis.assembler import assemble_report
    from src.schemas.models import CompanyEdgarFinancials, ConfidenceLevel, DataPoint

    # Discovery
    find_result = json.loads(await edgar_find_company("Apple Inc."))
    assert find_result["found"] is True, f"Apple not found live: {find_result}"
    assert find_result["cik"] == "0000320193"

    # Financials
    fin_result = json.loads(await edgar_get_financials(find_result["cik"]))
    assert "error" not in fin_result, f"edgar_get_financials error live: {fin_result}"
    assert fin_result["revenue"] is not None
    # Revenue should be at least FY2025 (update if Apple advances to FY2026+)
    assert fin_result["revenue"]["value_usd"] >= 416_161_000_000, (
        f"Live revenue {fin_result['revenue']['value_usd']:,} below FY2025 anchor. "
        f"Key used: {fin_result['revenue_key_used']!r}"
    )

    # Build edgar_data via CompanyEdgarFinancials.model_dump() — the real agent path
    companyfacts_url = fin_result["source_url"]
    revenue_fy = fin_result["revenue"]["fiscal_year"]
    edgar_model = CompanyEdgarFinancials(
        company_name="Apple Inc.",
        cik=find_result["cik"],
        is_sec_reporting=True,
        edgar_lookup_status="succeeded",
        revenue=DataPoint(
            value=f"{fin_result['revenue']['formatted']} (FY{revenue_fy})",
            confidence=ConfidenceLevel.HIGH,
            sources=[companyfacts_url],
            reasoning=f"From {fin_result['revenue_key_used']}",
        ),
        profitability=DataPoint(
            value=f"Net income {fin_result['net_income']['formatted']} (FY{revenue_fy})",
            confidence=ConfidenceLevel.HIGH,
            sources=[companyfacts_url],
        ),
        fiscal_year_end=DataPoint(
            value=fin_result["revenue"].get("period_end", "unknown"),
            confidence=ConfidenceLevel.HIGH,
            sources=[companyfacts_url],
        ),
        most_recent_filing=DataPoint(
            value=f"10-K filed {find_result['most_recent_filing']}",
            confidence=ConfidenceLevel.HIGH,
            sources=[companyfacts_url],
        ),
        sec_risk_factors=[],
    )
    edgar_data = edgar_model.model_dump()

    financial_data = {
        "company_name": "Apple Inc.",
        **{k: {"value": "unknown", "confidence": "unknown", "sources": [], "derived": False, "derived_from": []}
           for k in ("revenue", "revenue_growth", "profitability", "total_funding",
                     "last_funding_round", "valuation", "revenue_model")},
        "key_investors": [], "key_customers": [], "financial_risks": [],
        "recent_financial_events": [],
    }
    trace_summary = {
        "trace_id": "live_test_apple_001",
        "total_cost_usd": 0.0, "total_duration_ms": 0.0,
        "total_llm_calls": 0, "total_tool_calls": 0,
        "total_input_tokens": 0, "total_output_tokens": 0, "agents": {},
    }

    doc = assemble_report(
        research_data=None, financial_data=financial_data,
        risk_data=None, social_media_data=None, synthesis_data=None,
        trace_summary=trace_summary, edgar_data=edgar_data,
    )

    # PR1 headline assertion: EDGAR revenue at primary_document tier
    assert doc.run_metadata.edgar_lookup_status == "succeeded"
    assert doc.run_metadata.edgar_cik == "0000320193"
    assert doc.financial is not None
    assert doc.financial.revenue is not None, "EDGAR merge dropped revenue — assembler merge bug"
    assert doc.financial.revenue.agent == "edgar"
    assert all(s.tier.value == "primary_document" for s in doc.financial.revenue.sources), (
        f"EDGAR revenue not at primary_document tier: "
        f"{[s.tier.value for s in doc.financial.revenue.sources]}"
    )
    assert any("data.sec.gov" in s.url for s in doc.financial.revenue.sources)
    # The assembled value must NOT be the financial agent's "unknown"
    assert doc.financial.revenue.value != "unknown"
    assert "$" in doc.financial.revenue.value


# ── Anchor 2: SpaceX Iridium trap — PR1 regression anchor ───────────────────

@pytest.mark.asyncio
async def test_live_spacex_10k_not_resolved_to_wrong_filer():
    """Iridium's 10-K must not be returned as SpaceX's CIK.

    Live proof (PR1): searching with forms=10-K returns 153 documents that MENTION
    SpaceX; the top primary-doc hit is Iridium's 10-K (CIK 0001418819). Filer
    verification must reject it.

    With PR2's two-pass search, edgar_find_company now SUCCEEDS for SpaceX via
    the S-1/424B pass. So the overall result is found=True (CIK 0001181412) —
    the Iridium trap is still blocked, but the function now correctly finds SpaceX.

    This test now asserts the Iridium CIK is NOT returned (the core regression
    anchor) while accepting found=True from the S-1/424B pass.
    """
    from src.sources.edgar import edgar_find_company
    result = json.loads(await edgar_find_company("Space Exploration Technologies"))
    # The Iridium CIK must never be returned — that's the core regression anchor
    assert result.get("cik") != "0001418819", (
        f"Iridium's CIK returned for SpaceX query — filer verification is broken. "
        f"Result: {result}"
    )
    # With PR2, SpaceX IS found via 424B4
    if result["found"]:
        assert result["cik"] == "0001181412", (
            f"SpaceX found but with wrong CIK: {result['cik']!r}"
        )


# ── Anchor 3 (superseded): SpaceX EFTS check ─────────────────────────────────
# PR1 monkey-patched the module; PR2 integrates S-1/424B into edgar_find_company
# directly. Anchor 5 replaces this test. Kept for history.

@pytest.mark.asyncio
async def test_live_spacex_s1_resolves_correct_cik():
    """SpaceX resolves to CIK 0001181412 via the two-pass edgar_find_company (PR2).

    PR1 version manually monkey-patched _PRIMARY_FORM_TYPES; PR2 integrates the
    S-1/424B pass into edgar_find_company directly. This test now uses the real
    function without monkey-patching — the expected result is found=True with
    CIK 0001181412 and filing_type in {S-1/A, 424B4}.
    """
    from src.sources.edgar import edgar_find_company
    result = json.loads(await edgar_find_company("Space Exploration Technologies"))
    assert result["found"] is True, f"SpaceX not found via two-pass: {result}"
    assert result["cik"] == "0001181412", (
        f"Expected SpaceX CIK 0001181412, got {result['cik']!r}."
    )
    assert result["filing_type"] in {"S-1", "S-1/A", "424B4"}, (
        f"Expected S-1/424B filing_type; got {result['filing_type']!r}"
    )


# ── Anchors 5-7: SpaceX PR2 live tests ────────────────────────────────────────

@pytest.mark.asyncio
async def test_live_spacex_resolves_via_424b4():
    """Anchor 5: SpaceX legal-name path returns 424B4 as most recent filing.

    NOTE: This test calls edgar_find_company with the legal name directly to
    verify the EFTS 424B4 path. It does NOT simulate the pipeline path (the
    pipeline passes "SpaceX" as brand name). See test_live_spacex_brand_name
    for the pipeline-realistic anchor (Anchor 8).

    The 424B4 (priced prospectus) was filed 2026-06-12 (IPO day). It should
    be selected over older S-1/A amendments by the date-descending sort.
    """
    from src.sources.edgar import edgar_find_company
    result = json.loads(await edgar_find_company("Space Exploration Technologies"))

    assert result["found"] is True, f"SpaceX not found: {result}"
    assert result["cik"] == "0001181412", (
        f"Expected CIK 0001181412, got {result['cik']!r}"
    )
    # 424B4 was filed 2026-06-12 — should be picked as most recent
    assert result["filing_type"] == "424B4", (
        f"Expected 424B4 (most recent filing); got {result['filing_type']!r}. "
        f"Check if a 10-K has since been filed (would change filing_type to '10-K')."
    )
    assert result["accession_no"] == "0001628280-26-042639", (
        f"Expected 424B4 accession, got {result['accession_no']!r}"
    )
    assert result["most_recent_filing"] == "2026-06-12"
    assert "most_recent_10k" not in result  # 424B4 is not a 10-K


@pytest.mark.asyncio
async def test_live_spacex_companyfacts_xbrl_not_available():
    """Anchor 6: SpaceX companyfacts returns 200 with empty XBRL — not an error.

    At IPO, SEC has not yet aggregated XBRL facts for the new registrant.
    This must return xbrl_available=False, revenue=None — NOT lookup_failed.
    The company IS an SEC filer; the data just isn't there yet.

    This test will need updating once SpaceX files its first 10-K.
    """
    from src.sources.edgar import edgar_get_financials
    result = json.loads(await edgar_get_financials("0001181412"))

    assert "error" not in result, f"Unexpected error from companyfacts: {result}"
    assert result["xbrl_available"] is False, (
        f"Expected xbrl_available=False (no XBRL yet for SpaceX). "
        f"If True, XBRL data has appeared — update the anchor and use the numbers."
    )
    assert result["revenue"] is None, (
        f"Expected revenue=None (no XBRL). Got {result.get('revenue')}"
    )
    assert result["net_income"] is None
    assert result["entity_name"] == "SPACE EXPLORATION TECHNOLOGIES CORP"


@pytest.mark.asyncio
async def test_live_spacex_risk_factors_from_424b4():
    """Anchor 7: SpaceX 424B4 Risk Factors extractable.

    The Risk Factors section in the 424B4 uses a standalone RISK FACTORS heading
    (no Item 1A). The text must:
      - Start with 'RISK FACTORS'
      - Include substantive content (>200 chars)
      - End before 'CAUTIONARY STATEMENT' (section terminator)
      - Return filing_form_type='424B4'
    """
    from src.sources.edgar import edgar_get_filing_text
    result = json.loads(await edgar_get_filing_text(
        cik="0001181412",
        accession_no="0001628280-26-042639",
        section="risk_factors",
    ))

    assert "error" not in result, f"Unexpected error from edgar_get_filing_text: {result}"
    assert result.get("filing_form_type") == "424B4", (
        f"Expected filing_form_type=424B4; got {result.get('filing_form_type')!r}"
    )
    text = result.get("text", "")
    assert len(text) > 200, f"Risk factors text suspiciously short ({len(text)} chars)"
    assert "risk factor" in text.lower(), (
        "Expected 'risk factor' in extracted text"
    )
    assert "CAUTIONARY STATEMENT" not in text.upper(), (
        "Risk factors section extended past CAUTIONARY STATEMENT — end pattern not working"
    )


# ── Anchor 8: 'SpaceX' brand name (pipeline-realistic) ───────────────────────

@pytest.mark.asyncio
async def test_live_spacex_brand_name_resolves():
    """Anchor 8: 'SpaceX' brand name resolves to CIK 0001181412 via legal_name cascade.

    PIPELINE-REALISTIC: this test drives the EXACT input the orchestrator sends.
    The pipeline passes brand name 'SpaceX'; the classifier provides
    legal_name='Space Exploration Technologies Corp'. edgar_find_company uses
    the legal name for EFTS search so filer-verification passes.

    Without legal_name (plain 'SpaceX'), _filer_matches_query would fail because
    'spacex' is not a substring of 'space exploration technologies corp' or vice
    versa — the root cause of the not_sec_reporting regression on 2026-06-13.

    Asserts:
      - found=True (not_sec_reporting would mean the fix failed)
      - cik=0001181412 (SpaceX, not Iridium or any other filer)
      - filing_type in 424B/S-1 family (no 10-K yet at time of PR3)
    """
    from src.sources.edgar import edgar_find_company
    result = json.loads(await edgar_find_company(
        "SpaceX",
        legal_name="Space Exploration Technologies Corp",
    ))

    assert result["found"] is True, (
        f"'SpaceX' with legal_name did not resolve — fix is broken. Result: {result}"
    )
    assert result["cik"] == "0001181412", (
        f"Expected SpaceX CIK 0001181412; got {result['cik']!r}"
    )
    assert result["filing_type"] in {"424B4", "424B3", "424B5", "S-1", "S-1/A", "10-K"}, (
        f"Unexpected filing_type: {result['filing_type']!r}"
    )
    # Confirm filer-verification was the legal name path (exact match), not brand name
    assert result["name_match"] in {"exact", "ticker"}, (
        f"Expected exact or ticker name_match (legal_name resolved); got {result['name_match']!r}"
    )
