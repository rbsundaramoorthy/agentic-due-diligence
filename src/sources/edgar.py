"""
EDGAR tool functions for the EdgarAgent.

Wraps the SEC EDGAR REST APIs with rate limiting and caching. All public
functions are async and return JSON strings suitable for LLM tool_result
messages.

API endpoints used:
  Full-text search:  https://efts.sec.gov/LATEST/search-index
  Company facts:     https://data.sec.gov/api/xbrl/companyfacts/CIK{n}.json
  Filing index:      https://www.sec.gov/Archives/edgar/data/{cik}/{accession}-index.json
  Filing document:   https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{doc}.htm

SEC policy requires a User-Agent header. Set EDGAR_USER_AGENT to identify
your application: e.g. "MyApp/1.0 user@example.com".

Rate limit: 10 requests/second across all *.sec.gov endpoints.

Cache TTLs:
  - CIK lookups and per-accession filing text: None (immutable)
  - Companyfacts: 86400s (24h) — updates when new filings are submitted

EFTS _source field mapping (confirmed from live API 2026-06-11):
  Real field       Former (wrong) assumption
  -----------      --------------------------
  ciks             (parsed from accession_no — field did not exist)
  display_names    entity_name
  adsh             accession_no
  form             form_type
  file_type        (not captured — needed to filter primary docs from exhibits)
  root_forms       (not captured)

Discovery search order (PR2 — 2026-06-13):
  Pass 1:  forms=10-K  (periodic report; preferred when available)
  Pass 2a: forms=424B4,424B3,424B5  (final prospectus; searched separately
           so a just-priced 424B4 is not buried by S-1 relevance hits)
  Pass 2b: forms=S-1,S-1/A  (initial registration statement; fallback)
  An ambiguous 10-K result is returned immediately without trying later passes.
  A 10-K mention-only result (filer verification failure) falls through to 2a/2b.
"""

import asyncio
import json
import os
import re
from typing import Dict, Optional, Tuple

import httpx

from src.sources.cache import SourceCache


_USER_AGENT = os.getenv(
    "EDGAR_USER_AGENT",
    "AgenteAuditBot/1.0 contact@example.com",
)
_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "application/json",
}
_COMPANYFACTS_TTL = 86400    # 24h — new filings update the aggregate
_FILING_TEXT_TTL = None      # Immutable after submission

# Global rate limiter — 10 req/sec cap across all EDGAR calls
_rate_lock = asyncio.Lock()
_last_request_time: float = 0.0
_MIN_INTERVAL = 0.10         # 100ms between requests

# Pass 1: annual periodic reports (preferred).
_ANNUAL_FORM_TYPES: frozenset = frozenset({"10-K"})

# Pass 2a: final prospectus forms (searched separately so a freshly-priced
# 424B4 is selected over older S-1/A amendments in EFTS relevance ranking).
_PROSPECTUS_FORM_TYPES: frozenset = frozenset({"424B4", "424B3", "424B5"})

# Pass 2b: initial registration statement forms.
_REGISTRATION_STMT_FORM_TYPES: frozenset = frozenset({"S-1", "S-1/A"})

# All registration/prospectus form types (passes 2a + 2b combined).
_REGISTRATION_FORM_TYPES: frozenset = _PROSPECTUS_FORM_TYPES | _REGISTRATION_STMT_FORM_TYPES

# Union of all primary filing document types (used for document-type detection
# in edgar_get_filing_text index lookup).
_PRIMARY_FORM_TYPES: frozenset = _ANNUAL_FORM_TYPES | _REGISTRATION_FORM_TYPES


async def _rate_limited_get(
    client: httpx.AsyncClient, url: str, **kwargs
) -> httpx.Response:
    """GET request with EDGAR 10 req/sec rate limiting."""
    global _last_request_time
    async with _rate_lock:
        loop = asyncio.get_running_loop()
        elapsed = loop.time() - _last_request_time
        if elapsed < _MIN_INTERVAL:
            await asyncio.sleep(_MIN_INTERVAL - elapsed)
        _last_request_time = loop.time()
    return await client.get(url, **kwargs)


def _pad_cik(cik: str) -> str:
    """Return CIK zero-padded to 10 digits."""
    return str(cik).lstrip("0").zfill(10)


def _entity_name_from_display(display_names: list) -> str:
    """Extract the plain entity name from an EFTS display_names entry.

    EFTS display_names entries have the form:
      "Apple Inc.  (AAPL)  (CIK 0000320193)"
      "SOME COMPANY  (CIK 0001234567)"

    Returns the name portion before the first parenthesized group.
    """
    raw = display_names[0] if display_names else ""
    return raw.split("  (")[0].strip()


def _filer_matches_query(
    entity_name: str,
    query: str,
    legal_name: Optional[str] = None,
) -> bool:
    """Return True when the EFTS hit was filed BY the queried company.

    Primary check: bidirectional case-insensitive substring between the EFTS
    filer entity name and the search query:
      query IN entity  (e.g. "Space Exploration Technologies" ⊂ "SPACE EXPLORATION TECHNOLOGIES CORP")
      entity IN query  (e.g. "Apple Inc." ⊂ "Apple Inc.")

    Secondary check (when legal_name provided): same bidirectional check between
    the filer entity name and the known legal/registered name. This handles
    brand-name queries (e.g. "SpaceX") where the EFTS search returns the
    company's own filing but the brand name doesn't substring-match the full
    registered name ("SPACE EXPLORATION TECHNOLOGIES CORP").

    Rejects mention-only hits where a different company (e.g. Iridium) filed a
    document that merely references the queried company in its text. The legal
    name secondary check does NOT weaken this guard — Iridium's entity name
    fails both the query and legal_name checks.
    """
    e = entity_name.strip().lower()
    q = query.strip().lower()
    if q and e and (q in e or e in q):
        return True
    if legal_name:
        ln = legal_name.strip().lower()
        if ln and e and (ln in e or e in ln):
            return True
    return False


# ── edgar_find_company ────────────────────────────────────────────────────────

def _resolve_hits(
    hits: list,
    primary_types: frozenset,
    name_stripped: str,
    legal_name: Optional[str] = None,
) -> Optional[Dict]:
    """Resolve one EFTS hit-list to a result dict, or None if no hits.

    Returns:
      {"found": True, ...}                       — unique valid filer found
      {"found": False, "error": "ambiguous", ...} — multiple CIKs pass filer check
      {"found": False, "verification_failed": True, "filer_entity": ..., "note": ...}
                                                  — hits found but filer check rejected all
      None                                        — empty hit list
    """
    if not hits:
        return None

    # Filter to recognised primary filing document types; fall back to all hits
    # only if none pass the filter (e.g. very old filings with absent file_type).
    primary_hits = [
        h for h in hits
        if h.get("_source", {}).get("file_type", "") in primary_types
    ]
    if not primary_hits:
        primary_hits = hits

    # Disambiguation: sort by (exact-case name match DESC, file_date DESC).
    def _sort_key(hit: dict) -> tuple:
        src = hit.get("_source", {})
        file_date = src.get("file_date", "1900-01-01")
        entity = _entity_name_from_display(src.get("display_names", []))
        exact = 1 if entity.lower() == name_stripped.lower() else 0
        return (exact, file_date)

    primary_hits.sort(key=_sort_key, reverse=True)
    best = primary_hits[0]["_source"]

    cik_list = best.get("ciks", [])
    cik = cik_list[0] if cik_list else None
    if not cik:
        return None

    accession = best.get("adsh", "")
    company_name = _entity_name_from_display(best.get("display_names", []))
    most_recent = best.get("file_date", "unknown")
    filing_type = best.get("file_type", "unknown")

    # Filer verification: reject mention-only hits where a different company
    # filed a document that merely references the queried company by name.
    # legal_name is passed as secondary check for brand-name queries.
    if not _filer_matches_query(company_name, name_stripped, legal_name):
        return {
            "found": False,
            "verification_failed": True,
            "filer_entity": company_name,
            "note": (
                f"EDGAR has filings mentioning '{name_stripped}' but the top filer is "
                f"'{company_name}' — filer-verification rejected (name mismatch). "
                "Pass the company's SEC-registered legal name or ticker symbol to resolve."
            ),
        }

    name_match = "exact" if company_name.lower() == name_stripped.lower() else "partial"

    # Ambiguity guard: on a partial match, check for multiple distinct passing CIKs.
    if name_match == "partial":
        other_matching_ciks = {
            hit["_source"]["ciks"][0]
            for hit in primary_hits[1:10]
            if hit["_source"].get("ciks")
            and _filer_matches_query(
                _entity_name_from_display(hit["_source"].get("display_names", [])),
                name_stripped,
                legal_name,
            )
            and hit["_source"]["ciks"][0] != cik
        }
        if other_matching_ciks:
            all_ciks = sorted({cik} | other_matching_ciks)
            return {
                "found": False,
                "error": "ambiguous",
                "matching_ciks": all_ciks,
                "note": (
                    f"Multiple distinct SEC filers match '{name_stripped}' — cannot safely "
                    f"pick one. Matching CIKs: {all_ciks}. "
                    "Use the full legal company name (e.g. include 'Inc.', 'Corp.', 'LLC') "
                    "to resolve to a single filer."
                ),
            }

    result: Dict = {
        "found": True,
        "cik": cik,
        "company_name": company_name,
        "most_recent_filing": most_recent,
        "accession_no": accession,
        "filing_type": filing_type,
        "name_match": name_match,
        "note": (
            "CIK resolved from EFTS ciks[0]; primary filing document selected "
            f"(file_type={filing_type}). "
            "Disambiguation: exact-name match first, most-recent filing second. "
            f"Name match quality: {name_match}. "
            + ("Verify entity name matches the intended company." if name_match == "partial" else "")
        ),
    }
    # Backwards-compat alias: consumers that pre-date PR2 may read most_recent_10k.
    if filing_type == "10-K":
        result["most_recent_10k"] = most_recent
    return result


async def _cik_from_ticker_lookup(
    ticker: str,
    client: httpx.AsyncClient,
    cache: Optional[SourceCache] = None,
) -> Optional[str]:
    """Resolve a ticker symbol to a zero-padded CIK via SEC company_tickers.json.

    Downloads the full tickers file and caches the per-ticker result (not the
    3MB file) so subsequent calls hit the cache instead of re-downloading.
    TTL=None because CIK-ticker mappings are immutable once assigned.
    Returns None on network error or if the ticker is not found.
    """
    cache_params = {"op": "cik_from_ticker", "ticker": ticker.upper()}
    if cache:
        hit = cache.get("sec_edgar", cache_params)
        if hit:
            return json.loads(hit).get("cik")

    url = "https://www.sec.gov/files/company_tickers.json"
    try:
        resp = await _rate_limited_get(client, url, headers=_HEADERS)
        resp.raise_for_status()
    except Exception:
        return None

    ticker_upper = ticker.upper()
    cik: Optional[str] = None
    for entry in resp.json().values():
        if entry.get("ticker", "").upper() == ticker_upper:
            cik = _pad_cik(str(entry["cik_str"]))
            break

    if cache:
        cache.put("sec_edgar", cache_params, json.dumps({"cik": cik}),
                  url=url, ttl_seconds=None)
    return cik


async def _filing_info_from_submissions(
    cik: str,
    client: httpx.AsyncClient,
) -> Optional[Dict]:
    """Fetch entity name and most recent primary filing info from submissions API.

    Used after ticker-based CIK resolution to get filing details without an
    EFTS name-matching search. Returns None on network error or if no primary
    filing is found in the recent filings list.
    """
    padded = _pad_cik(cik)
    url = f"https://data.sec.gov/submissions/CIK{padded}.json"
    try:
        resp = await _rate_limited_get(client, url, headers=_HEADERS)
        resp.raise_for_status()
    except Exception:
        return None

    sub = resp.json()
    entity_name = sub.get("name", "")
    recent = sub.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    dates = recent.get("filingDate", [])

    # filings.recent is sorted most-recent-first; pick first primary form type.
    for form, acc, date in zip(forms, accessions, dates):
        if form in _PRIMARY_FORM_TYPES:
            return {
                "entity_name": entity_name,
                "filing_type": form,
                "accession_no": acc,
                "most_recent_filing": date,
                "cik": cik,
            }
    return None


async def edgar_find_company(
    name: str,
    cache: Optional[SourceCache] = None,
    ticker: Optional[str] = None,
    legal_name: Optional[str] = None,
) -> str:
    """Find a company's EDGAR CIK by name.

    Resolution cascade (PR3):
      Step a — ticker → CIK via company_tickers.json: direct lookup when a
               ticker symbol is known. Bypasses EFTS name matching entirely.
      Step b — legal_name → three-pass EFTS (10-K → 424B → S-1): use the
               SEC-registered legal name when it differs from the brand name.
               Filer verification passes because the legal name matches the
               EFTS display_names entry.
      Step c — brand name → three-pass EFTS (current PR2 behavior, fallback).

    The brand name (name) is used only for step c. Steps a and b are driven
    by classifier-provided identifiers that bypass the filer-name mismatch
    (e.g. "SpaceX" brand vs "SPACE EXPLORATION TECHNOLOGIES CORP" registered).

    Returns JSON: {found, cik, company_name, most_recent_filing, accession_no,
                   filing_type, name_match, note}
                  (most_recent_10k alias also set for 10-K results)
    """
    ticker_upper = (ticker or "").upper().strip()
    legal_stripped = (legal_name or "").strip()
    cache_params = {
        "op": "find_company",
        "name": name.strip(),
        "ticker": ticker_upper,
        "legal_name": legal_stripped.lower(),
    }
    if cache:
        hit = cache.get("sec_edgar", cache_params)
        if hit:
            return hit

    efts_url = "https://efts.sec.gov/LATEST/search-index"
    name_stripped = name.strip()

    async def _search(
        client: httpx.AsyncClient,
        forms_str: str,
        primary_types: frozenset,
        query: str,
        legal_name_for_verify: Optional[str] = None,
    ) -> Optional[Dict]:
        """One EFTS pass with an explicit query string.

        legal_name_for_verify is passed to _resolve_hits as the secondary
        filer-verification check — used when the brand name doesn't substring-
        match the SEC-registered legal name (e.g. "SpaceX" vs "SPACE EXPLORATION
        TECHNOLOGIES CORP").
        """
        params = {
            "q": f'"{query}"',
            "forms": forms_str,
            "dateRange": "custom",
            "startdt": "2015-01-01",
        }
        try:
            resp = await _rate_limited_get(
                client, efts_url, headers=_HEADERS, params=params
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                return {"found": False, "error": "rate_limited",
                        "note": "EDGAR rate limit reached."}
            return None
        except Exception:
            return None
        hits = resp.json().get("hits", {}).get("hits", [])
        return _resolve_hits(hits, primary_types, query, legal_name_for_verify)

    def _finish(result_dict: dict) -> str:
        body = json.dumps(result_dict)
        if cache:
            cache.put("sec_edgar", cache_params, body, url=efts_url, ttl_seconds=None)
        return body

    try:
        async with httpx.AsyncClient(timeout=15) as client:

            # Step a: ticker → CIK via company_tickers.json (exact, no name matching)
            if ticker_upper:
                cik_resolved = await _cik_from_ticker_lookup(ticker_upper, client, cache)
                if cik_resolved:
                    info = await _filing_info_from_submissions(cik_resolved, client)
                    if info:
                        result: Dict = {
                            "found": True,
                            "cik": cik_resolved,
                            "company_name": info["entity_name"],
                            "most_recent_filing": info["most_recent_filing"],
                            "accession_no": info["accession_no"],
                            "filing_type": info["filing_type"],
                            "name_match": "ticker",
                            "note": (
                                f"CIK resolved directly via ticker '{ticker_upper}' "
                                "from SEC company_tickers.json — no EFTS name matching required."
                            ),
                        }
                        if info["filing_type"] == "10-K":
                            result["most_recent_10k"] = info["most_recent_filing"]
                        return _finish(result)

            # Steps b + c: EFTS three-pass search.
            # Use legal_name first (step b) when it differs from the brand name;
            # fall through to brand name (step c) only if all legal_name passes fail.
            search_queries: list = []
            if legal_stripped and legal_stripped.lower() != name_stripped.lower():
                search_queries.append(legal_stripped)
            search_queries.append(name_stripped)

            verification_failed_note: Optional[str] = None
            # legal_name (when different from brand name) is passed to every
            # _search call as a secondary filer-verification check. This lets
            # a brand-name EFTS hit (e.g. SpaceX's own 424B4 found via "SpaceX")
            # pass verification even when the brand name doesn't substring-match
            # the registered name "SPACE EXPLORATION TECHNOLOGIES CORP".
            verify_legal = legal_stripped if legal_stripped else None

            for query in search_queries:
                # Pass 1: annual / periodic (10-K)
                r1 = await _search(client, "10-K", _ANNUAL_FORM_TYPES, query, verify_legal)
                if r1 is not None:
                    if r1.get("found") or r1.get("error") in ("ambiguous", "rate_limited"):
                        return _finish(r1)
                    if r1.get("verification_failed"):
                        verification_failed_note = r1.get("note")

                # Pass 2a: final prospectus forms (424B-series)
                # Searched separately from S-1 so that a recently-priced 424B4 is
                # selected over an older S-1/A amendment (EFTS relevance ranking
                # mixes them in combined queries and buries the 424B4 past page 1).
                r2a = await _search(client, "424B4,424B3,424B5", _PROSPECTUS_FORM_TYPES, query, verify_legal)
                if r2a is not None:
                    if r2a.get("found") or r2a.get("error") == "ambiguous":
                        return _finish(r2a)
                    if r2a.get("verification_failed"):
                        verification_failed_note = r2a.get("note")

                # Pass 2b: initial registration statement (S-1, S-1/A)
                r2b = await _search(client, "S-1,S-1/A", _REGISTRATION_STMT_FORM_TYPES, query, verify_legal)
                if r2b is not None:
                    if r2b.get("found") or r2b.get("error") == "ambiguous":
                        return _finish(r2b)
                    if r2b.get("verification_failed"):
                        verification_failed_note = r2b.get("note")

    except Exception as e:
        return json.dumps({"found": False, "error": str(e)})

    note = verification_failed_note or (
        f"No SEC filing found for '{name}' in EDGAR (searched 10-K, S-1, 424B forms). "
        "Company is likely private, non-US, or not SEC-reporting."
    )
    return _finish({"found": False, "cik": None, "company_name": None, "note": note})


# ── edgar_get_financials ──────────────────────────────────────────────────────

def _extract_most_recent_annual(units_list: list) -> Optional[dict]:
    """Return the most recent annual (10-K FY) observation from an XBRL fact list."""
    annual = [u for u in units_list if u.get("form") == "10-K" and u.get("fp") == "FY"]
    if not annual:
        annual = [u for u in units_list if u.get("form") == "10-K"]
    if not annual:
        return None
    annual.sort(key=lambda u: u.get("end", ""), reverse=True)
    return annual[0]


def _extract_two_most_recent_annual(
    units_list: list,
) -> Tuple[Optional[dict], Optional[dict]]:
    """Return (latest, prior) distinct annual FY observations from an XBRL fact list.

    The same fiscal period often appears in multiple 10-K filings (comparative data
    re-filed with the subsequent year's 10-K). Deduplicates by period end date,
    keeping the highest accession number per date (most recently filed, which captures
    any restatement). Returns (None, None) when no annual data is present; prior is
    None when only one distinct period exists.
    """
    annual = [u for u in units_list if u.get("form") == "10-K" and u.get("fp") == "FY"]
    if not annual:
        annual = [u for u in units_list if u.get("form") == "10-K"]
    if not annual:
        return None, None

    by_end: dict = {}
    for u in annual:
        end = u.get("end", "")
        existing = by_end.get(end)
        if existing is None or u.get("accn", "") > existing.get("accn", ""):
            by_end[end] = u

    distinct = sorted(by_end.values(), key=lambda u: u.get("end", ""), reverse=True)
    return (
        distinct[0] if len(distinct) >= 1 else None,
        distinct[1] if len(distinct) >= 2 else None,
    )


def _fmt_usd(val: int) -> str:
    if val >= 1_000_000_000:
        return f"${val / 1_000_000_000:.2f}B"
    if val >= 1_000_000:
        return f"${val / 1_000_000:.1f}M"
    return f"${val:,}"


async def edgar_get_financials(
    cik: str,
    cache: Optional[SourceCache] = None,
) -> str:
    """Fetch pre-processed annual financial facts from the EDGAR companyfacts API.

    Revenue fallback chain (tried in order):
      1. us-gaap.Revenues
      2. us-gaap.RevenueFromContractWithCustomerExcludingAssessedTax
      3. us-gaap.InterestAndDividendIncomeOperating + us-gaap.NoninterestIncome
         (bank / financial services companies)
      4. "unknown" — lists all attempted keys so the caller can diagnose

    Companyfacts aggregates across all filings; cached with 24h TTL.
    Returns compact JSON — revenue, net_income, fiscal_year, source_url,
    revenue_key_used, revenue_keys_attempted, accession_no.
    """
    padded = _pad_cik(cik)
    # v2: cache key bumped when prior_revenue/revenue_growth_pct were added to output
    cache_params = {"op": "companyfacts", "v": "2", "cik": padded}
    if cache:
        hit = cache.get("sec_edgar", cache_params)
        if hit:
            return hit

    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{padded}.json"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await _rate_limited_get(client, url, headers=_HEADERS)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            return json.dumps({"error": "rate_limited", "cik": cik})
        if e.response.status_code == 404:
            return json.dumps({"error": "cik_not_found", "cik": cik})
        return json.dumps({"error": f"HTTP {e.response.status_code}", "cik": cik})
    except Exception as e:
        return json.dumps({"error": str(e), "cik": cik})

    us_gaap = data.get("facts", {}).get("us-gaap", {})
    entity_name = data.get("entityName", "")

    # Detect brand-new registrants whose XBRL has not been aggregated yet.
    # S-1/424B filers often appear in companyfacts as entity={name} with
    # empty us-gaap and dei sections immediately after IPO.
    xbrl_available = bool(us_gaap)

    _REVENUE_KEYS_ATTEMPTED = [
        "us-gaap.Revenues",
        "us-gaap.RevenueFromContractWithCustomerExcludingAssessedTax",
        "us-gaap.InterestAndDividendIncomeOperating + us-gaap.NoninterestIncome",
    ]

    revenue_val: Optional[int] = None
    fiscal_year: Optional[str] = None
    period_end: Optional[str] = None
    accession_no: Optional[str] = None
    revenue_key_used: Optional[str] = None
    prior_revenue_val: Optional[int] = None
    prior_fiscal_year: Optional[str] = None

    # Chain steps 1 & 2: standard revenue keys.
    # Pick the key with the MOST RECENT annual observation (handles companies that
    # switched XBRL tags, e.g. Apple deprecated us-gaap.Revenues after FY2018).
    # _extract_two_most_recent_annual also returns the prior-year observation for
    # YoY growth computation.
    _best_obs: Optional[dict] = None
    _best_prior_obs: Optional[dict] = None
    _best_key: Optional[str] = None
    for xbrl_key in (
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
    ):
        facts = us_gaap.get(xbrl_key, {}).get("units", {}).get("USD", [])
        latest, prior = _extract_two_most_recent_annual(facts)
        if latest and (
            _best_obs is None
            or latest.get("end", "") > _best_obs.get("end", "")
        ):
            _best_obs = latest
            _best_prior_obs = prior
            _best_key = f"us-gaap.{xbrl_key}"

    if _best_obs is not None:
        revenue_val = _best_obs["val"]
        fiscal_year = str(_best_obs.get("fy") or _best_obs.get("end", "")[:4])
        period_end = _best_obs.get("end")
        accession_no = _best_obs.get("accn")
        revenue_key_used = _best_key
        if _best_prior_obs is not None:
            prior_revenue_val = _best_prior_obs.get("val")
            # Use end-date year, not fy: comparative data re-filed in a later 10-K
            # carries the filing year in fy (e.g. FY2024 data in FY2025 10-K has fy=2025).
            prior_fiscal_year = _best_prior_obs.get("end", "")[:4] or str(
                _best_prior_obs.get("fy", "")
            )

    # Chain step 3: bank / financial services fallback
    if revenue_val is None:
        interest_facts = us_gaap.get("InterestAndDividendIncomeOperating", {}).get("units", {}).get("USD", [])
        noninterest_facts = us_gaap.get("NoninterestIncome", {}).get("units", {}).get("USD", [])
        interest_latest, interest_prior = _extract_two_most_recent_annual(interest_facts)
        noninterest_latest, noninterest_prior = _extract_two_most_recent_annual(noninterest_facts)

        if interest_latest and noninterest_latest:
            revenue_val = interest_latest["val"] + noninterest_latest["val"]
            fiscal_year = str(interest_latest.get("fy") or interest_latest.get("end", "")[:4])
            period_end = interest_latest.get("end")
            accession_no = interest_latest.get("accn")
            revenue_key_used = (
                "us-gaap.InterestAndDividendIncomeOperating + us-gaap.NoninterestIncome"
            )
            if interest_prior and noninterest_prior:
                prior_revenue_val = interest_prior["val"] + noninterest_prior["val"]
                prior_fiscal_year = interest_prior.get("end", "")[:4] or str(
                    interest_prior.get("fy", "")
                )
        elif interest_latest:
            revenue_val = interest_latest["val"]
            fiscal_year = str(interest_latest.get("fy") or interest_latest.get("end", "")[:4])
            period_end = interest_latest.get("end")
            accession_no = interest_latest.get("accn")
            revenue_key_used = "us-gaap.InterestAndDividendIncomeOperating"
            if interest_prior:
                prior_revenue_val = interest_prior.get("val")
                prior_fiscal_year = interest_prior.get("end", "")[:4] or str(
                    interest_prior.get("fy", "")
                )

    # Net income
    net_income_val: Optional[int] = None
    ni_facts = us_gaap.get("NetIncomeLoss", {}).get("units", {}).get("USD", [])
    ni_obs = _extract_most_recent_annual(ni_facts)
    if ni_obs:
        net_income_val = ni_obs["val"]

    result: dict = {
        "cik": cik,
        "entity_name": entity_name,
        "source_url": url,
        "xbrl_available": xbrl_available,
        "revenue_key_used": revenue_key_used or "none_found",
        "revenue_keys_attempted": _REVENUE_KEYS_ATTEMPTED,
    }

    if revenue_val is not None:
        result["revenue"] = {
            "value_usd": revenue_val,
            "formatted": _fmt_usd(revenue_val),
            "fiscal_year": fiscal_year,
            "period_end": period_end,
            "accession_no": accession_no,
        }
        if prior_revenue_val is not None and prior_revenue_val != 0:
            result["prior_revenue"] = {
                "value_usd": prior_revenue_val,
                "formatted": _fmt_usd(prior_revenue_val),
                "fiscal_year": prior_fiscal_year,
            }
            result["revenue_growth_pct"] = (
                (revenue_val - prior_revenue_val) / prior_revenue_val * 100
            )
    else:
        result["revenue"] = None
        result["revenue_note"] = (
            "No recognized revenue line item found in XBRL facts. "
            f"Attempted: {', '.join(_REVENUE_KEYS_ATTEMPTED)}"
        )

    if net_income_val is not None:
        result["net_income"] = {
            "value_usd": net_income_val,
            "formatted": _fmt_usd(net_income_val),
        }
    else:
        result["net_income"] = None

    body = json.dumps(result)
    if cache:
        cache.put(
            "sec_edgar", cache_params, body, url=url, ttl_seconds=_COMPANYFACTS_TTL
        )
    return body


# ── edgar_get_filing_text ─────────────────────────────────────────────────────

async def edgar_get_filing_text(
    cik: str,
    accession_no: str,
    section: str = "risk_factors",
    cache: Optional[SourceCache] = None,
) -> str:
    """Fetch and extract text from a specific section of an EDGAR filing.

    Works for 10-K annual reports and S-1/424B registration / prospectus filings.

    Supported sections:
      'risk_factors' — Item 1A (10-K) or the RISK FACTORS heading (S-1/424B)
      'business'     — Item 1
      'mda'          — Item 7 (Management's Discussion and Analysis)

    Returns up to 8000 chars of section text plus filing_form_type and
    extraction_status ("extracted"|"section_not_found"|"fetch_failed").
    Caches forever — filings are immutable after submission to EDGAR.
    """
    padded = _pad_cik(cik)
    acc_clean = accession_no.replace("-", "")
    acc_dash = f"{acc_clean[:10]}-{acc_clean[10:12]}-{acc_clean[12:]}"

    cache_params = {
        "op": "filing_text", "cik": padded,
        "accession": acc_dash, "section": section,
    }
    if cache:
        hit = cache.get("sec_edgar", cache_params)
        if hit:
            return hit

    # Locate the primary document. Strategy (in order of reliability):
    #   1. Filing index JSON   — https://www.sec.gov/Archives/.../acc-index.json
    #      (exists for most filings, but brand-new submissions may 404)
    #   2. Submissions API     — https://data.sec.gov/submissions/CIK{padded}.json
    #      (always available; lists recent filings with primaryDocument + form)
    #
    # cik_int is the unpadded integer form used in EDGAR archive URL paths.
    cik_int = int(cik.lstrip("0") or "0")

    main_doc: Optional[str] = None
    filing_form_type: str = "unknown"

    async with httpx.AsyncClient(timeout=20) as client:
        # Strategy 1: filing index JSON
        index_url = (
            f"https://www.sec.gov/Archives/edgar/data/{cik_int}"
            f"/{acc_clean}/{acc_dash}-index.json"
        )
        try:
            resp = await _rate_limited_get(
                client, index_url, headers={**_HEADERS, "Accept": "*/*"}
            )
            resp.raise_for_status()
            index = resp.json()
            for item in index.get("directory", {}).get("item", []):
                if item.get("type") in _PRIMARY_FORM_TYPES and item.get("name", "").endswith(".htm"):
                    main_doc = item["name"]
                    filing_form_type = item.get("type", "unknown")
                    break
            if not main_doc:
                for item in index.get("directory", {}).get("item", []):
                    if item.get("name", "").endswith(".htm"):
                        main_doc = item["name"]
                        break
        except Exception:
            pass  # Fall through to strategy 2

        # Strategy 2: submissions API (brand-new registrants, 424B filings)
        if not main_doc:
            sub_url = f"https://data.sec.gov/submissions/CIK{padded}.json"
            try:
                sub_resp = await _rate_limited_get(client, sub_url, headers=_HEADERS)
                sub_resp.raise_for_status()
                sub_data = sub_resp.json()
                recent = sub_data.get("filings", {}).get("recent", {})
                accessions = recent.get("accessionNumber", [])
                docs = recent.get("primaryDocument", [])
                forms = recent.get("form", [])
                for acc_r, doc_r, form_r in zip(accessions, docs, forms):
                    if acc_r == acc_dash and doc_r.endswith(".htm"):
                        main_doc = doc_r
                        filing_form_type = form_r or "unknown"
                        break
            except Exception:
                pass

    if not main_doc:
        return json.dumps({
            "error": "Could not locate primary filing document (tried index.json and submissions API).",
            "accession_no": acc_dash,
        })

    doc_url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik_int}"
        f"/{acc_clean}/{main_doc}"
    )

    # Fetch the document with retry on transient connection failures.
    # Large filings (10-15 MB) can hit connection resets during parallel
    # pipeline runs; 3 attempts with exponential backoff resolve these.
    html: Optional[str] = None
    last_fetch_error: str = ""
    for _attempt in range(3):
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=30.0, read=90.0, write=30.0, pool=30.0)
            ) as _client:
                resp = await _rate_limited_get(
                    _client, doc_url, headers={**_HEADERS, "Accept": "*/*"}
                )
                resp.raise_for_status()
                html = resp.text
                break
        except Exception as e:
            last_fetch_error = str(e)
            if _attempt < 2:
                await asyncio.sleep(2 ** _attempt)

    if html is None:
        # Never cache a fetch failure — the filing is immutable and a retry will succeed.
        return json.dumps({
            "extraction_status": "fetch_failed",
            "error": f"Could not fetch filing after 3 attempts: {last_fetch_error}",
            "url": doc_url,
        })

    # Strip HTML tags
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    text_lower = text.lower()

    _SECTION_PATTERNS = {
        # Patterns are priority-ordered within each section key.
        # The finder stops at the first tier that yields a substantive match.
        #
        # Tier 1 — exact structural headers (10-K Item numbers).
        # Tier 2 — section-opener language unique to 424B/S-1 prose bodies.
        # Tier 3 — broad fallback (catches any remaining form layouts).
        "risk_factors": [
            r"item\s*1a[\s\.]+risk\s*factors",                             # 10-K tier 1
            r"risk\s*factors\s+(?:an?\s+investment|investing\s+in|the\s+following\s+risk)",  # 424B/S-1 tier 2
            r"risk\s*factors",                                              # broad tier 3
        ],
        "business": [
            r"item\s*1[\s\.]+business\b",
            r"our\s*business",
        ],
        "mda": [
            r"item\s*7[\s\.]+management",
            r"management.{0,30}discussion",
        ],
    }
    _NEXT_SECTION = {
        # 10-K next-section markers + S-1/424B equivalents (no Item 1B in prospectus)
        "risk_factors": (
            r"item\s*1b[\s\.]|item\s*2[\s\.]"
            r"|cautionary\s+statement\s+regarding"
            r"|use\s+of\s+proceeds"
            r"|dividend\s+policy"
        ),
        "business":     r"item\s*1a[\s\.]|risk\s*factors",
        "mda":          r"item\s*7a[\s\.]|item\s*8[\s\.]",
    }

    # Priority-tier section finder:
    #   1. For each pattern tier (in priority order), find all non-cross-ref
    #      occurrences and keep the one with the most content before the next
    #      real section terminator.
    #   2. If the best match in this tier yields ≥ _MIN_SECTION_CHARS, use it
    #      and stop (don't try lower-priority tiers).
    #   3. Fall through to the next tier if no match is substantive enough.
    #
    # Cross-reference filtering: SEC filings wrap referenced section names in
    # HTML smart-quote entities (&#8220; / &#8221;). Standalone headings never
    # have &#8220; in the 10 chars immediately before them, so checking for this
    # entity reliably distinguishes heading matches from prose references.
    # The same check is applied to terminator matches inside the section body
    # (e.g. "see &#8220;Cautionary Statement Regarding&#8221;" is skipped).
    end_pattern = _NEXT_SECTION.get(section)
    best_start = -1
    best_len = 0
    _HEADING_SKIP = 0    # terminator search starts at the heading match itself, not 100 chars in.
                         # With zero skip, a TOC line "Item 1A. Risk Factors 5 Item 1B." fires
                         # its terminator at ~24 chars — below threshold — and is rejected.
    _PRE_CONTEXT = 10    # chars to look behind for a cross-reference quote entity
    _MIN_SECTION_CHARS = 100  # below this, the match is a one-liner artifact

    def _is_cross_ref(pos: int) -> bool:
        """Return True when text immediately before pos contains &#8220; (open smart-quote)."""
        return "&#8220;" in text[max(0, pos - _PRE_CONTEXT):pos]

    def _real_end(search_from: int) -> int:
        """Find next standalone terminator (not a cross-reference), or end-of-text."""
        if not end_pattern:
            return len(text_lower)
        pos = search_from
        while True:
            em = re.search(end_pattern, text_lower[pos:])
            if not em:
                return len(text_lower)
            abs_pos = pos + em.start()
            if _is_cross_ref(abs_pos):
                pos = abs_pos + max(1, len(em.group()))
                continue
            return abs_pos

    for pattern in _SECTION_PATTERNS.get(section, []):
        tier_best_start = -1
        tier_best_len = 0
        for m in re.finditer(pattern, text_lower):
            start = m.start()
            if _is_cross_ref(start):
                continue  # skip prose cross-references like &#8220;Risk Factors&#8221;
            search_from = start + _HEADING_SKIP
            end_pos = _real_end(search_from)
            raw_len = end_pos - search_from
            if raw_len >= _MIN_SECTION_CHARS:
                # Pick the SHORTEST substantive match: the actual section heading is
                # adjacent to its terminator (only its own section before the next Item).
                # Cross-references appearing BEFORE the heading span additional content
                # (the whole section they precede plus the real section). References
                # AFTER the heading find no remaining terminator and extend to
                # end-of-document.  Both are longer than the real heading, so
                # shortest-above-threshold reliably selects the true heading.
                if tier_best_start == -1 or raw_len < tier_best_len:
                    tier_best_len = raw_len
                    tier_best_start = start
        if tier_best_start != -1:
            best_start = tier_best_start
            best_len = tier_best_len
            break  # first tier with substantive content wins

    if best_start == -1:
        result = json.dumps({
            "section": section,
            "extraction_status": "section_not_found",
            "note": (
                f"Could not locate a substantive '{section}' section in "
                f"{filing_form_type}. The filing may use non-standard headings."
            ),
            "source_url": doc_url,
            "filing_form_type": filing_form_type,
        })
    else:
        search_from = best_start + _HEADING_SKIP
        end_pos = _real_end(search_from)
        section_text = text[best_start:end_pos].strip()[:8000]
        result = json.dumps({
            "section": section,
            "extraction_status": "extracted",
            "text": section_text,
            "source_url": doc_url,
            "accession_no": acc_dash,
            "filing_form_type": filing_form_type,
        })

    if cache:
        cache.put(
            "sec_edgar", cache_params, result, url=doc_url, ttl_seconds=_FILING_TEXT_TTL
        )
    return result
