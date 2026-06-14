"""Tests for samgov_search_contracts tool function."""

import json
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_no_api_key_degrades_gracefully():
    """Missing API key returns no_api_key=True without making HTTP calls."""
    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("SAM_GOV_API_KEY", None)
        from src.sources.samgov import samgov_search_contracts
        result = json.loads(await samgov_search_contracts("Leidos"))
    assert result["no_api_key"] is True
    assert result["found"] is False
    assert "SAM_GOV_API_KEY" in result["note"]


@pytest.mark.asyncio
async def test_found_active_contractor():
    """Active SAM.gov registration returns entity details."""
    fixture = json.load(open("tests/fixtures/samgov/entity_leidos.json"))

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fixture
    mock_resp.raise_for_status = MagicMock()

    with patch.dict(os.environ, {"SAM_GOV_API_KEY": "test-key"}):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_client.get.return_value = mock_resp

            from importlib import reload
            import src.sources.samgov as sam_mod
            reload(sam_mod)

            result = json.loads(await sam_mod.samgov_search_contracts("Leidos"))

    assert result["found"] is True
    assert result["uei"] == "LGVMRR4MJR42"
    assert result["cage_code"] == "1P784"
    assert result["registration_status"] == "Active"
    assert any("541512" in n for n in result["naics_codes"])


@pytest.mark.asyncio
async def test_not_found_no_entity():
    """Company not in SAM.gov returns found=False."""
    fixture = json.load(open("tests/fixtures/samgov/entity_stripe.json"))

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fixture
    mock_resp.raise_for_status = MagicMock()

    with patch.dict(os.environ, {"SAM_GOV_API_KEY": "test-key"}):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_client.get.return_value = mock_resp

            from importlib import reload
            import src.sources.samgov as sam_mod
            reload(sam_mod)

            result = json.loads(await sam_mod.samgov_search_contracts("Stripe"))

    assert result["found"] is False


@pytest.mark.asyncio
async def test_cache_hit_skips_http():
    """Cache hit returns stored result without making an HTTP call."""
    cached = json.dumps({"found": True, "uei": "CACHED123", "from_cache": True})
    mock_cache = MagicMock()
    mock_cache.get.return_value = cached

    with patch.dict(os.environ, {"SAM_GOV_API_KEY": "test-key"}):
        with patch("httpx.AsyncClient") as mock_client_cls:
            from importlib import reload
            import src.sources.samgov as sam_mod
            reload(sam_mod)

            result_str = await sam_mod.samgov_search_contracts("Leidos", cache=mock_cache)

    assert json.loads(result_str)["from_cache"] is True
    mock_client_cls.assert_not_called()
