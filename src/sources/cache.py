"""
SQLite-backed cache for Tier 0 source API responses.

Cache key: sha256(source_id + ":" + canonical JSON of params).
TTL:
  - None  → cache forever (per-accession EDGAR filings, USPTO patents)
  - N sec → evict after N seconds (companyfacts 24h, SAM.gov/CourtListener 1 day)

The cache lives in the same SQLite file as the observability DB (agent_log.db)
but in its own table. It is self-managing — the table is created on first use
via CREATE TABLE IF NOT EXISTS.
"""

import hashlib
import json
import sqlite3
import time
from typing import Optional


_DDL = """
CREATE TABLE IF NOT EXISTS source_cache (
    cache_key        TEXT PRIMARY KEY,
    source_id        TEXT NOT NULL,
    url              TEXT,
    params_json      TEXT,
    response_body    TEXT NOT NULL,
    cached_at        REAL NOT NULL,
    ttl_seconds      INTEGER,
    hit_count        INTEGER DEFAULT 0,
    created_by_trace TEXT
);
"""


class SourceCache:
    """SQLite-backed response cache for Tier 0 API calls."""

    def __init__(self, db_path: str = "outputs/agent_log.db"):
        self.db_path = db_path
        self._ensure_table()

    def _ensure_table(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(_DDL)
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _make_key(source_id: str, params: dict) -> str:
        raw = f"{source_id}:{json.dumps(params, sort_keys=True)}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, source_id: str, params: dict) -> Optional[str]:
        """Return a cached response body, or None on miss or TTL expiry."""
        key = self._make_key(source_id, params)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT response_body, cached_at, ttl_seconds "
                "FROM source_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            if row["ttl_seconds"] is not None:
                age = time.time() - row["cached_at"]
                if age > row["ttl_seconds"]:
                    conn.execute(
                        "DELETE FROM source_cache WHERE cache_key = ?", (key,)
                    )
                    conn.commit()
                    return None
            conn.execute(
                "UPDATE source_cache SET hit_count = hit_count + 1 "
                "WHERE cache_key = ?",
                (key,),
            )
            conn.commit()
            return row["response_body"]
        finally:
            conn.close()

    def put(
        self,
        source_id: str,
        params: dict,
        response_body: str,
        url: str = "",
        ttl_seconds: Optional[int] = None,
        trace_id: str = "",
    ) -> None:
        """Store a response. ttl_seconds=None means cache forever."""
        key = self._make_key(source_id, params)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO source_cache
                    (cache_key, source_id, url, params_json, response_body,
                     cached_at, ttl_seconds, hit_count, created_by_trace)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    key, source_id, url,
                    json.dumps(params, sort_keys=True),
                    response_body, time.time(),
                    ttl_seconds, trace_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def clear(self, source_id: Optional[str] = None) -> None:
        """Delete all cache entries, or only those for a specific source_id."""
        conn = sqlite3.connect(self.db_path)
        try:
            if source_id:
                conn.execute(
                    "DELETE FROM source_cache WHERE source_id = ?", (source_id,)
                )
            else:
                conn.execute("DELETE FROM source_cache")
            conn.commit()
        finally:
            conn.close()
