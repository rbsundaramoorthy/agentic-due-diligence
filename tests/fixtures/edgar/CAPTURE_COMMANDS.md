# EDGAR Fixture Capture Commands

All fixtures in this directory must be captured from live API responses.
**Do not hand-author EFTS fixtures** — the EFTS `_source` schema has fields that
differ from what one might expect (e.g. `adsh` not `accession_no`, `ciks` not a
parsed CIK, `display_names` not `entity_name`). A hand-authored fixture encoding
the wrong field names will pass unit tests while production silently fails.
This was the root cause of the Apple CIK resolution bug discovered 2026-06-11.

Convention: after any SEC API schema change, re-run the commands below and
commit the updated fixtures alongside the code change. The guard test
`test_efts_fixture_schema_uses_real_fields` will catch stale invented fields.

---

## search_apple_ambiguous.json

Two primary 10-K docs for different companies both named "Apple Something" —
tests that edgar_find_company("Apple") returns found=False with error="ambiguous"
instead of silently picking one. Uses the same real hits as the disambiguation
fixture but combined into a single response representing a bare-name query.

```bash
# Two separate real hits combined for the ambiguity test fixture:
# Hit 1: Apple Inc. (CIK 0000320193) — from search_aapl.json primary 10-K
# Hit 2: Apple REIT Nine (CIK 0001418121) — from search_apple_disambiguation.json
# Both appear as primary 10-K docs. Query "Apple" is a substring of both
# entity names, so _filer_matches_query passes for both → ambiguous.
# No live capture needed: the fixture is built from two already-captured real hits.
```

Note: to find a current real ambiguous case, try:
```bash
curl -s "https://efts.sec.gov/LATEST/search-index?q=%22Apple%22&forms=10-K&dateRange=custom&startdt=2012-01-01" \
  -H "User-Agent: AgenteAuditBot/1.0 contact@example.com" \
  | python3 -c "
import json, sys
data = json.load(sys.stdin)
hits = data['hits']['hits']
primary = [h for h in hits if h['_source'].get('file_type') == '10-K']
ciks = {}
for h in primary[:20]:
    cik = h['_source'].get('ciks', [None])[0]
    entity = h['_source'].get('display_names', ['?'])[0].split('  (')[0]
    if cik not in ciks:
        ciks[cik] = {'entity': entity, 'date': h['_source'].get('file_date')}
print('Distinct CIKs:', json.dumps(ciks, indent=2))
"
```

## search_spacex_mention_only.json

Iridium Communications 10-K that mentions "Space Exploration Technologies" —
used to test that the filer verification rejects mention-only matches.

```bash
curl -s "https://efts.sec.gov/LATEST/search-index?q=%22Space+Exploration+Technologies%22&forms=10-K" \
  -H "User-Agent: AgenteAuditBot/1.0 contact@example.com" \
  -H "Accept: application/json" \
  | python3 -c "
import json, sys
data = json.load(sys.stdin)
hits = data['hits']['hits']
# First primary 10-K doc (Iridium, not SpaceX — the mention-only trap)
primary = next(h for h in hits if h['_source'].get('file_type') == '10-K')
data['hits']['hits'] = [primary]
print(json.dumps(data, indent=2))
" > tests/fixtures/edgar/search_spacex_mention_only.json
```

## search_spacex_s1.json

SpaceX S-1/A primary document — used to verify CIK 0001181412 resolves correctly
when filer entity contains the query substring.

```bash
curl -s "https://efts.sec.gov/LATEST/search-index?q=%22Space+Exploration+Technologies%22&forms=S-1%2CS-1%2FA&dateRange=custom&startdt=2025-01-01" \
  -H "User-Agent: AgenteAuditBot/1.0 contact@example.com" \
  -H "Accept: application/json" \
  | python3 -c "
import json, sys
data = json.load(sys.stdin)
hits = data['hits']['hits']
# First primary S-1/A doc (SpaceX, CIK 0001181412)
primary = next(h for h in hits if h['_source'].get('file_type') in ('S-1','S-1/A'))
data['hits']['hits'] = [primary]
print(json.dumps(data, indent=2))
" > tests/fixtures/edgar/search_spacex_s1.json
```

## search_aapl.json

Captures an Apple Inc. 10-K EFTS search with both a primary filing document
and an exhibit, to test that the primary-doc filter selects correctly.

```bash
# Fetch top hits — includes EX-23.1 exhibit (higher score) + primary 10-K
curl -s "https://efts.sec.gov/LATEST/search-index?q=%22Apple+Inc.%22&forms=10-K&dateRange=custom&startdt=2022-01-01" \
  -H "User-Agent: AgenteAuditBot/1.0 contact@example.com" \
  -H "Accept: application/json" \
  | python3 -c "
import json, sys
data = json.load(sys.stdin)
hits = data['hits']['hits']
# Keep one exhibit + one primary 10-K doc
exhibit = next(h for h in hits if h['_source'].get('file_type') != '10-K')
primary = next(h for h in hits if h['_source'].get('file_type') == '10-K')
data['hits']['hits'] = [exhibit, primary]
print(json.dumps(data, indent=2))
" > tests/fixtures/edgar/search_aapl.json
```

Key assertions this fixture enables:
- `test_find_company_aapl`: CIK resolves to 0000320193
- `test_find_company_filters_to_primary_docs_not_exhibits`: exhibit is filtered out

---

## search_apple_disambiguation.json

Two 10-K filers both named "Apple Something" — one is Apple Inc. (correct),
one is Apple REIT Nine / Apple Hospitality REIT (decoy).

```bash
# Broader "Apple" search returns multiple Apple-named filers
curl -s "https://efts.sec.gov/LATEST/search-index?q=%22Apple%22&forms=10-K&dateRange=custom&startdt=2012-01-01" \
  -H "User-Agent: AgenteAuditBot/1.0 contact@example.com" \
  -H "Accept: application/json" \
  | python3 -c "
import json, sys
data = json.load(sys.stdin)
hits = data['hits']['hits']
primary = [h for h in hits if h['_source'].get('file_type') == '10-K']
# Find Apple Inc. (CIK 0000320193) and a different Apple filer
apple_inc  = next((h for h in primary if '0000320193' in h['_source'].get('ciks',[])), None)
apple_decoy = next((h for h in primary if '0000320193' not in h['_source'].get('ciks',[])), None)
# Combine: decoy first (higher relevance score in raw results) to test sort
data['hits']['hits'] = [apple_decoy, apple_inc]
print(json.dumps(data, indent=2))
" > tests/fixtures/edgar/search_apple_disambiguation.json
# Note: decoy is listed first in the fixture (as EFTS returns it)
# to verify that our sort logic, not array order, drives selection.
```

Key assertion: `test_find_company_disambiguation_picks_most_recent_exact_match` →
CIK must be 0000320193 (Apple Inc.), never 0001418121 (Apple REIT).

---

## companyfacts_aapl.json

Minimal Apple XBRL fixture — only the facts edgar_get_financials reads.
Re-capture when Apple files a new 10-K (annually, ~end of October).

Important: Apple uses us-gaap.RevenueFromContractWithCustomerExcludingAssessedTax
from FY2019 onward (NOT us-gaap.Revenues, which was last used in FY2018).
The revenue chain picks the key with the most-recent annual data — confirmed by
the live Apple run: FY2025 revenue = 416,161,000,000.

```bash
python3 - <<'EOF'
import asyncio, httpx, json, os

HEADERS = {
    'User-Agent': os.getenv('EDGAR_USER_AGENT', 'AgenteAuditBot/1.0 contact@example.com'),
    'Accept': 'application/json',
}

async def main():
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            'https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json',
            headers=HEADERS
        )
        data = r.json()
    us_gaap = data['facts']['us-gaap']

    def all_10k(entries, n=6):
        k = [u for u in entries if u.get('form') == '10-K']
        return sorted(k, key=lambda u: u['end'], reverse=True)[:n]

    minimal = {
        'entityName': data['entityName'],
        'facts': {'us-gaap': {
            'Revenues': {
                'label': us_gaap['Revenues']['label'],
                'description': us_gaap['Revenues']['description'],
                'units': {'USD': all_10k(us_gaap['Revenues']['units']['USD'], 3)},
            },
            'RevenueFromContractWithCustomerExcludingAssessedTax': {
                'label': us_gaap['RevenueFromContractWithCustomerExcludingAssessedTax']['label'],
                'description': us_gaap['RevenueFromContractWithCustomerExcludingAssessedTax']['description'],
                'units': {'USD': all_10k(us_gaap['RevenueFromContractWithCustomerExcludingAssessedTax']['units']['USD'], 6)},
            },
            'NetIncomeLoss': {
                'label': us_gaap['NetIncomeLoss']['label'],
                'description': us_gaap['NetIncomeLoss']['description'],
                'units': {'USD': all_10k(us_gaap['NetIncomeLoss']['units']['USD'], 6)},
            },
        }}
    }
    print(json.dumps(minimal, indent=2))

asyncio.run(main())
EOF
> tests/fixtures/edgar/companyfacts_aapl.json
```

After re-capture, update test assertions for the new FY year in test_edgar_tools.py:
- test_get_financials_aapl_uses_revenue_from_contract_key
- test_get_financials_most_recent_annual_is_selected
- test_edgar_apple_end_to_end_merge

---

## companyfacts_jpm.json

JPMorgan Chase XBRL companyfacts — used to test the bank/financial-services
revenue fallback chain (InterestAndDividendIncomeOperating + NoninterestIncome).

```bash
curl -s "https://data.sec.gov/api/xbrl/companyfacts/CIK0000019617.json" \
  -H "User-Agent: AgenteAuditBot/1.0 contact@example.com" \
  -H "Accept: application/json" \
  > tests/fixtures/edgar/companyfacts_jpm.json
```
```

---

## search_spacex_424b4.json (PR2 — 2026-06-13)

SpaceX 424B4 (priced prospectus) filed 2026-06-12 — the most recent primary
filing for CIK 0001181412. Used to test that edgar_find_company returns
filing_type='424B4' for a company whose only EDGAR presence is an IPO prospectus.

**Re-capture when:** SpaceX files a 10-K (first annual report, likely late 2027).
At that point the test expectation for filing_type will change from '424B4' to '10-K'.

```bash
curl -s "https://efts.sec.gov/LATEST/search-index?q=%22Space+Exploration+Technologies%22&forms=424B4" \
  -H "User-Agent: AgenteAuditBot/1.0 contact@example.com" \
  -H "Accept: application/json" \
  | python3 -c "
import json, sys
data = json.load(sys.stdin)
hits = data['hits']['hits']
spacex = next(h for h in hits if '0001181412' in (h['_source'].get('ciks') or []))
data['hits']['hits'] = [spacex]
print(json.dumps(data, indent=2))
" > tests/fixtures/edgar/search_spacex_424b4.json
```

Key assertions this fixture enables:
- `test_find_company_spacex_resolves_via_424b4`: CIK 0001181412, filing_type='424B4'
- `test_efts_fixture_schema_uses_real_fields`: 424B4 fixture has real EFTS fields

---

## companyfacts_spacex_empty.json (PR2 — 2026-06-13)

SpaceX XBRL companyfacts immediately after IPO. Returns HTTP 200 with entity
metadata but empty us-gaap and dei sections — XBRL aggregation has not happened
yet for this brand-new registrant. This is the "succeeded-with-gaps" scenario:
the company IS an SEC filer, but no XBRL financial data is available yet.

**Re-capture when:** SpaceX's XBRL data appears in companyfacts (typically
weeks after the first XBRL-tagged filing). At that point, the fixture will need
to include revenue/net_income data and the test assertion must change to
`xbrl_available=True`.

```bash
curl -s "https://data.sec.gov/api/xbrl/companyfacts/CIK0001181412.json" \
  -H "User-Agent: AgenteAuditBot/1.0 contact@example.com" \
  -H "Accept: application/json" \
  | python3 -c "
import json, sys
data = json.load(sys.stdin)
minimal = {'entityName': data['entityName'], 'cik': data['cik'], 'facts': data['facts']}
print(json.dumps(minimal, indent=2))
" > tests/fixtures/edgar/companyfacts_spacex_empty.json
```

Key assertions this fixture enables:
- `test_get_financials_empty_xbrl_not_error`: xbrl_available=False, revenue=None
- `test_companyfacts_spacex_empty_fixture_integrity`: entity metadata intact
