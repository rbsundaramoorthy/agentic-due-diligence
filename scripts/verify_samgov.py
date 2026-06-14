"""
Verify SAM.gov API connectivity and key validity.

Usage:
    export SAM_GOV_API_KEY=your-key
    python scripts/verify_samgov.py <company_name>

Tests two cases in order:
  1. Key absent  — confirm graceful degradation
  2. Key present — search for the given company name and print results

Exits 0 on success, 1 on any unexpected failure.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


async def run(company_name: str):
    from src.sources.samgov import samgov_search_contracts

    api_key = os.environ.get("SAM_GOV_API_KEY")
    ok = True

    # ── Case 1: no API key ────────────────────────────────────────
    print("── Case 1: no API key ──────────────────────────────────")
    saved_key = os.environ.pop("SAM_GOV_API_KEY", None)
    result = json.loads(await samgov_search_contracts(company_name))
    if result.get("no_api_key") is True and result.get("found") is False:
        print("  PASS — returned no_api_key=true, graceful degradation confirmed")
    else:
        print(f"  FAIL — unexpected response: {result}")
        ok = False
    if saved_key:
        os.environ["SAM_GOV_API_KEY"] = saved_key

    if not api_key:
        print()
        print("SAM_GOV_API_KEY is not set — skipping live API test.")
        print("Set the key and re-run to test actual SAM.gov connectivity.")
        return ok

    # ── Case 2: live search for the given company ─────────────────
    print()
    print(f"── Case 2: SAM.gov lookup — {company_name} ─────────────")
    result = json.loads(await samgov_search_contracts(company_name))
    if result.get("found") is True:
        print(f"  FOUND — {result.get('entity_name')}")
        print(f"          UEI:    {result.get('uei')}")
        print(f"          CAGE:   {result.get('cage_code')}")
        print(f"          Status: {result.get('registration_status')}")
        naics = result.get("naics_codes", [])
        if naics:
            print(f"          NAICS:  {naics[0]}")
    elif result.get("found") is False and not result.get("error"):
        print(f"  NOT FOUND — no SAM.gov registration for '{company_name}'")
        print(f"              Note: {result.get('note', '')[:80]}")
    else:
        print(f"  FAIL — error: {result.get('error')}")
        if result.get("detail"):
            print(f"          detail: {result['detail']}")
        ok = False

    return ok


def main():
    if len(sys.argv) != 2:
        print("Usage: python scripts/verify_samgov.py <company_name>")
        sys.exit(1)

    company_name = sys.argv[1]

    print("SAM.gov connectivity check")
    print("=" * 50)
    print(f"Company: {company_name}")
    print()

    passed = asyncio.run(run(company_name))

    print()
    print("=" * 50)
    if passed:
        print("All checks passed.")
        sys.exit(0)
    else:
        print("One or more checks failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
