"""Tests for uspto_search_patents tool function."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_found_patents_qualcomm():
    """Company with patents returns correct count and list."""
    fixture = json.load(open("tests/fixtures/uspto/patents_qualcomm.json"))

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fixture
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = mock_resp

        from src.sources.uspto import uspto_search_patents
        result = json.loads(await uspto_search_patents("Qualcomm"))

    assert result["found"] is True
    assert result["patent_count"] == 54821
    assert len(result["patents"]) == 3
    assert result["patents"][0]["patent_id"] == "US11870547"
    assert "beamforming" in result["patents"][0]["title"].lower()
    assert "search.patentsview.org" in result["source_url"]


@pytest.mark.asyncio
async def test_zero_patents_stripe():
    """Company with no patents returns found=False and patent_count=0."""
    fixture = json.load(open("tests/fixtures/uspto/patents_stripe.json"))

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fixture
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = mock_resp

        from src.sources.uspto import uspto_search_patents
        result = json.loads(await uspto_search_patents("Stripe"))

    assert result["found"] is False
    assert result["patent_count"] == 0


@pytest.mark.asyncio
async def test_cache_hit_skips_http():
    """Cache hit returns stored result without making an HTTP call."""
    cached = json.dumps({"found": True, "patent_count": 100, "from_cache": True})
    mock_cache = MagicMock()
    mock_cache.get.return_value = cached

    with patch("httpx.AsyncClient") as mock_client_cls:
        from src.sources.uspto import uspto_search_patents
        result_str = await uspto_search_patents("Qualcomm", cache=mock_cache)

    assert json.loads(result_str)["from_cache"] is True
    mock_client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_http_error_returns_error_json():
    """HTTP error returns {found: false, error: ...} without raising."""
    mock_resp = MagicMock()
    mock_resp.status_code = 503
    mock_resp.raise_for_status.side_effect = __import__("httpx").HTTPStatusError(
        "503", request=MagicMock(), response=mock_resp
    )

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.get.return_value = mock_resp

        from src.sources.uspto import uspto_search_patents
        result = json.loads(await uspto_search_patents("Qualcomm"))

    assert result["found"] is False
    assert "503" in result["error"]
