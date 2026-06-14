"""Tests for opencorporates_search_company tool function."""

import json
import os
import pytest

from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_no_api_key_returns_disabled():
    """Missing API key returns disabled=True immediately without making HTTP calls."""
    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("OPENCORPORATES_API_KEY", None)
        from src.sources.opencorporates import opencorporates_search_company
        result = json.loads(await opencorporates_search_company("Stripe"))
    assert result["disabled"] is True
    assert result["found"] is False
    assert "OPENCORPORATES_API_KEY" in result["note"]


@pytest.mark.asyncio
async def test_found_us_company(tmp_path):
    """Successful search returns incorporation_date and registered_address."""
    fixture = json.load(open("tests/fixtures/opencorporates/search_stripe.json"))

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fixture
    mock_resp.raise_for_status = MagicMock()

    with patch.dict(os.environ, {"OPENCORPORATES_API_KEY": "test-key"}):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_client.get.return_value = mock_resp

            from importlib import reload
            import src.sources.opencorporates as oc_mod
            reload(oc_mod)

            result = json.loads(await oc_mod.opencorporates_search_company("Stripe, Inc."))

    assert result["found"] is True
    assert result["incorporation_date"] == "2010-09-07"
    assert "San Francisco" in result["registered_address"] or "South San Francisco" in result["registered_address"]
    assert result["jurisdiction_code"] == "us_ca"
    assert "opencorporates.com" in result["source_url"]


@pytest.mark.asyncio
async def test_filters_non_us_results():
    """Only US jurisdictions (jurisdiction_code starting with 'us_') are returned."""
    fixture = json.load(open("tests/fixtures/opencorporates/search_no_us_results.json"))

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fixture
    mock_resp.raise_for_status = MagicMock()

    with patch.dict(os.environ, {"OPENCORPORATES_API_KEY": "test-key"}):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_client.get.return_value = mock_resp

            from importlib import reload
            import src.sources.opencorporates as oc_mod
            reload(oc_mod)

            result = json.loads(await oc_mod.opencorporates_search_company("EuroTech GmbH"))

    # Non-US jurisdiction should result in not-found
    assert result["found"] is False


@pytest.mark.asyncio
async def test_multi_match_prefers_exact_name():
    """When multiple US entities match, exact name match is preferred."""
    fixture = json.load(open("tests/fixtures/opencorporates/search_multi_match.json"))

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fixture
    mock_resp.raise_for_status = MagicMock()

    with patch.dict(os.environ, {"OPENCORPORATES_API_KEY": "test-key"}):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_client.get.return_value = mock_resp

            from importlib import reload
            import src.sources.opencorporates as oc_mod
            reload(oc_mod)

            result = json.loads(await oc_mod.opencorporates_search_company("Acme Corp"))

    assert result["found"] is True
    # Exact match "Acme Corp" (us_de) should be preferred over "Acme Corporation" (us_ny)
    assert result["jurisdiction_code"] == "us_de"


@pytest.mark.asyncio
async def test_cache_hit_skips_http():
    """Cache hit returns stored result without making an HTTP call."""
    cached = json.dumps({"found": True, "company_name": "Stripe", "from_cache": True})
    mock_cache = MagicMock()
    mock_cache.get.return_value = cached

    with patch.dict(os.environ, {"OPENCORPORATES_API_KEY": "test-key"}):
        with patch("httpx.AsyncClient") as mock_client_cls:
            from importlib import reload
            import src.sources.opencorporates as oc_mod
            reload(oc_mod)

            result_str = await oc_mod.opencorporates_search_company("Stripe", cache=mock_cache)

    assert json.loads(result_str)["from_cache"] is True
    mock_client_cls.assert_not_called()
