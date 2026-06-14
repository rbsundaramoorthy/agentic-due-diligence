"""Tests for the SQLite-backed SourceCache."""

import time

import pytest

from src.sources.cache import SourceCache


@pytest.fixture()
def cache(tmp_path):
    return SourceCache(db_path=str(tmp_path / "test.db"))


def test_miss_returns_none(cache):
    assert cache.get("sec_edgar", {"op": "find", "name": "Stripe"}) is None


def test_put_then_get_returns_value(cache):
    cache.put("sec_edgar", {"op": "find", "name": "Stripe"}, '{"found": true}')
    result = cache.get("sec_edgar", {"op": "find", "name": "Stripe"})
    assert result == '{"found": true}'


def test_ttl_expiry_returns_none(cache):
    cache.put("sec_edgar", {"op": "cf", "cik": "1"}, '{"rev": 1}', ttl_seconds=1)
    time.sleep(1.1)
    assert cache.get("sec_edgar", {"op": "cf", "cik": "1"}) is None


def test_no_ttl_never_expires(cache):
    cache.put("sec_edgar", {"op": "filing", "cik": "1"}, '{"text": "x"}', ttl_seconds=None)
    assert cache.get("sec_edgar", {"op": "filing", "cik": "1"}) == '{"text": "x"}'


def test_hit_count_increments(cache):
    params = {"op": "find", "name": "Apple"}
    cache.put("sec_edgar", params, '{"found": true}')
    cache.get("sec_edgar", params)
    cache.get("sec_edgar", params)
    import sqlite3
    conn = sqlite3.connect(cache.db_path)
    row = conn.execute("SELECT hit_count FROM source_cache").fetchone()
    conn.close()
    assert row[0] == 2


def test_key_is_stable_across_param_ordering(cache):
    p1 = {"b": 2, "a": 1}
    p2 = {"a": 1, "b": 2}
    cache.put("src", p1, '{"ok": true}')
    assert cache.get("src", p2) == '{"ok": true}'


def test_clear_all(cache):
    cache.put("src_a", {"x": 1}, "val_a")
    cache.put("src_b", {"x": 2}, "val_b")
    cache.clear()
    assert cache.get("src_a", {"x": 1}) is None
    assert cache.get("src_b", {"x": 2}) is None


def test_clear_by_source_id(cache):
    cache.put("src_a", {"x": 1}, "val_a")
    cache.put("src_b", {"x": 2}, "val_b")
    cache.clear("src_a")
    assert cache.get("src_a", {"x": 1}) is None
    assert cache.get("src_b", {"x": 2}) == "val_b"
