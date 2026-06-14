"""Tests for courtlistener_search_cases tool function."""

import json
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

_BARE_ENDPOINT = "https://www.courtlistener.com/api/rest/v4/search/"
_DOCKET_PREFIX = "https://www.courtlistener.com/docket/"


# ── Fixture integrity ─────────────────────────────────────────────────────────

def test_stripe_fixture_uses_docket_absolute_url():
    """cases_stripe.json must use docket_absolute_url (not absolute_url).

    This test guards against re-introducing the field-name bug that caused every
    litigation claim to cite the bare API search endpoint instead of a real docket.
    """
    fixture = json.load(open("tests/fixtures/courtlistener/cases_stripe.json"))
    for result in fixture["results"]:
        assert "docket_absolute_url" in result, (
            f"Result {result.get('docketNumber')} missing docket_absolute_url field"
        )
        assert "absolute_url" not in result, (
            f"Result {result.get('docketNumber')} uses wrong field name 'absolute_url'"
        )
        assert result["docket_absolute_url"].startswith("/docket/"), (
            f"docket_absolute_url should start with /docket/, got: {result['docket_absolute_url']}"
        )
        assert "party" in result, (
            f"Result {result.get('docketNumber')} missing party field"
        )


def test_thirdparty_seizure_fixture_uses_docket_absolute_url():
    """cases_thirdparty_seizure.json must use docket_absolute_url and include party list."""
    fixture = json.load(open("tests/fixtures/courtlistener/cases_thirdparty_seizure.json"))
    for result in fixture["results"]:
        assert "docket_absolute_url" in result
        assert "absolute_url" not in result
        assert result["docket_absolute_url"].startswith("/docket/")
        assert "party" in result

    docket_numbers = {r["docketNumber"] for r in fixture["results"]}
    assert "1:25-sz-00048" in docket_numbers, "Seizure regression case must be in fixture"
    assert "1:26-cv-03843" in docket_numbers, "True-defendant case must be in fixture"


def test_thirdparty_seizure_fixture_parties_are_correct():
    """The seizure case must have USA + property as parties; the company is NOT a named party."""
    fixture = json.load(open("tests/fixtures/courtlistener/cases_thirdparty_seizure.json"))
    seizure = next(r for r in fixture["results"] if r["docketNumber"] == "1:25-sz-00048")
    defendant = next(r for r in fixture["results"] if r["docketNumber"] == "1:26-cv-03843")

    # The company is NOT a named party in the in rem seizure case — its hardware is the res.
    # The party string contains the company name in ALL CAPS as part of the property
    # description; the check uses mixed-case "Example Aerospace Corp" which does not match
    # the all-caps property string, preserving the original case-sensitive distinction.
    assert not any("Example Aerospace Corp" in p for p in seizure["party"]), (
        "Company must not appear as a named party in the seizure case"
    )
    assert any("USA" in p or "United States" in p for p in seizure["party"])

    # The company IS a named party in the civil employment case
    assert any("Example Aerospace Corp" in p for p in defendant["party"])


# ── Tool output: citation URLs ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_docket_url_populated_from_docket_absolute_url():
    """Tool returns a per-case https://…/docket/<id>/… URL, not the bare API endpoint."""
    fixture = json.load(open("tests/fixtures/courtlistener/cases_stripe.json"))

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fixture
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = mock_resp

        from src.sources.courtlistener import courtlistener_search_cases
        result = json.loads(await courtlistener_search_cases("Stripe"))

    assert result["found"] is True
    for case in result["cases"]:
        assert case["source_url"].startswith(_DOCKET_PREFIX), (
            f"Expected docket URL, got: {case['source_url']}"
        )
        assert case["source_url"] != _BARE_ENDPOINT


@pytest.mark.asyncio
async def test_fallback_url_from_docket_id():
    """When docket_absolute_url is absent, tool falls back to /docket/{id}/ URL."""
    fixture = {
        "count": 1,
        "next": None,
        "previous": None,
        "results": [{
            "caseName": "Corp v. Other",
            "court_id": "cacd",
            "dateFiled": "2025-01-01",
            "docketNumber": "2:25-cv-00001",
            "docket_id": 99999,
            # no docket_absolute_url
            "party": ["Corp", "Other"],
            "cause": "",
        }],
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fixture
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = mock_resp

        from src.sources.courtlistener import courtlistener_search_cases
        result = json.loads(await courtlistener_search_cases("Corp"))

    assert result["cases"][0]["source_url"] == "https://www.courtlistener.com/docket/99999/"


@pytest.mark.asyncio
async def test_fallback_url_from_docket_number():
    """When neither docket_absolute_url nor docket_id is present, URL uses docket number."""
    fixture = {
        "count": 1,
        "next": None,
        "previous": None,
        "results": [{
            "caseName": "Corp v. Other",
            "court_id": "cacd",
            "dateFiled": "2025-01-01",
            "docketNumber": "1:25-sz-00048",
            # no docket_absolute_url, no docket_id
            "party": ["USA", "SEIZED PROPERTY"],
            "cause": "",
        }],
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fixture
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = mock_resp

        from src.sources.courtlistener import courtlistener_search_cases
        result = json.loads(await courtlistener_search_cases("Corp"))

    url = result["cases"][0]["source_url"]
    assert "courtlistener.com" in url
    assert url != _BARE_ENDPOINT
    assert "1" in url  # docket number is encoded


@pytest.mark.asyncio
async def test_parties_and_cause_in_case_dict():
    """Tool passes through parties list and cause from API response."""
    fixture = json.load(open("tests/fixtures/courtlistener/cases_thirdparty_seizure.json"))

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fixture
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = mock_resp

        from src.sources.courtlistener import courtlistener_search_cases
        result = json.loads(await courtlistener_search_cases("Example Aerospace Corp"))

    assert result["found"] is True
    romero = next(c for c in result["cases"] if "Romero" in c["case_name"])
    seizure = next(c for c in result["cases"] if "1:25-sz-00048" == c["docket_number"])

    assert "Example Aerospace Corp" in romero["parties"]
    assert "Evan Romero" in romero["parties"]
    assert romero["cause"] == "28:1332bc Diversity-Breach of Contract"

    # Company not a named party in the seizure case (its hardware is the in rem res)
    assert not any("Example Aerospace Corp" in p for p in seizure["parties"])
    assert any("USA" in p for p in seizure["parties"])


@pytest.mark.asyncio
async def test_citation_never_bare_api_endpoint():
    """Every per-case source_url must be a verifiable docket URL, never the bare endpoint."""
    fixture = json.load(open("tests/fixtures/courtlistener/cases_thirdparty_seizure.json"))

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fixture
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = mock_resp

        from src.sources.courtlistener import courtlistener_search_cases
        result = json.loads(await courtlistener_search_cases("Example Aerospace Corp"))

    for case in result["cases"]:
        assert case["source_url"] != _BARE_ENDPOINT, (
            f"Case {case['docket_number']} cites bare API endpoint"
        )
        assert "courtlistener.com" in case["source_url"]


# ── Existing tests (updated for new fixture schema) ───────────────────────────

@pytest.mark.asyncio
async def test_found_cases_returns_dockets():
    """Cases found returns case_count, case list with required fields."""
    fixture = json.load(open("tests/fixtures/courtlistener/cases_stripe.json"))

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fixture
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = mock_resp

        from src.sources.courtlistener import courtlistener_search_cases
        result = json.loads(await courtlistener_search_cases("Stripe"))

    assert result["found"] is True
    assert result["case_count"] == 6551
    assert len(result["cases"]) == 2

    # Verify first case fields — Lena Brands
    case = result["cases"][0]
    assert "Lena Brands" in case["case_name"] or "Stripe" in case["case_name"]
    assert case["docket_number"] == "26-50368"
    assert case["source_url"] == "https://www.courtlistener.com/docket/73407918/lena-brands-llc-v-stripe-inc/"
    assert "Stripe, Inc." in case["parties"]
    # No bare endpoint in per-case source
    assert case["source_url"] != _BARE_ENDPOINT


@pytest.mark.asyncio
async def test_no_cases_returns_not_found():
    """Zero results returns found=False."""
    fixture = json.load(open("tests/fixtures/courtlistener/cases_empty.json"))

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fixture
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = mock_resp

        from src.sources.courtlistener import courtlistener_search_cases
        result = json.loads(await courtlistener_search_cases("NonexistentCorp"))

    assert result["found"] is False
    assert result["case_count"] == 0


@pytest.mark.asyncio
async def test_unauthenticated_no_header():
    """Without COURTLISTENER_API_KEY, Authorization header is not sent."""
    fixture = json.load(open("tests/fixtures/courtlistener/cases_empty.json"))

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fixture
    mock_resp.raise_for_status = MagicMock()

    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("COURTLISTENER_API_KEY", None)
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_client.get.return_value = mock_resp

            from importlib import reload
            import src.sources.courtlistener as cl_mod
            reload(cl_mod)

            await cl_mod.courtlistener_search_cases("TestCorp")

    # Verify Authorization header was NOT sent
    call_kwargs = mock_client.get.call_args
    headers_sent = call_kwargs.kwargs.get("headers", {})
    assert "Authorization" not in headers_sent


@pytest.mark.asyncio
async def test_authenticated_sends_token():
    """With COURTLISTENER_API_KEY set, Authorization: Token is sent."""
    fixture = json.load(open("tests/fixtures/courtlistener/cases_empty.json"))

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fixture
    mock_resp.raise_for_status = MagicMock()

    with patch.dict(os.environ, {"COURTLISTENER_API_KEY": "my-token"}):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_client.get.return_value = mock_resp

            from importlib import reload
            import src.sources.courtlistener as cl_mod
            reload(cl_mod)

            await cl_mod.courtlistener_search_cases("TestCorp")

    call_kwargs = mock_client.get.call_args
    headers_sent = call_kwargs.kwargs.get("headers", {})
    assert headers_sent.get("Authorization") == "Token my-token"


@pytest.mark.asyncio
async def test_rate_limit_returns_error_json():
    """429 response returns found=False with rate_limited error."""
    mock_resp = MagicMock()
    mock_resp.status_code = 429

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = mock_resp

        from src.sources.courtlistener import courtlistener_search_cases
        result = json.loads(await courtlistener_search_cases("Stripe"))

    assert result["found"] is False
    assert result["error"] == "rate_limited"


@pytest.mark.asyncio
async def test_cache_hit_skips_http():
    """Cache hit returns stored result without making an HTTP call."""
    cached = json.dumps({"found": True, "case_count": 5, "from_cache": True})
    mock_cache = MagicMock()
    mock_cache.get.return_value = cached

    with patch("httpx.AsyncClient") as mock_client_cls:
        from src.sources.courtlistener import courtlistener_search_cases
        result_str = await courtlistener_search_cases("Stripe", cache=mock_cache)

    assert json.loads(result_str)["from_cache"] is True
    mock_client_cls.assert_not_called()
