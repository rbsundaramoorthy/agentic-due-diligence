"""
Verify USPTO PatentsView API connectivity.

Usage:
    python scripts/verify_uspto.py <company_name>

No API key required. Queries the PatentsView API v2 and prints patent count
and up to 3 recent patents for the given company.

Exits 0 on success, 1 on any unexpected failure.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


async def run(company_name: str) -> bool:
    from src.sources.uspto import uspto_search_patents

    print(f"── USPTO PatentsView lookup — {company_name} ─────────────")
    result = json.loads(await uspto_search_patents(company_name))

    if result.get("error"):
        print(f"  FAIL — error: {result.get('error')}")
        detail = result.get("detail")
        if detail:
            print(f"          detail: {detail}")
        return False

    if result.get("found") is True:
        count = result.get("patent_count", 0)
        print(f"  FOUND — {count:,} patents assigned to '{company_name}'")
        for p in result.get("patents", [])[:3]:
            print(f"          [{p.get('date')}] {p.get('patent_id')} — {p.get('title', '')[:70]}")
        if count > 3:
            print(f"          ... and {count - 3:,} more")
    else:
        print(f"  NOT FOUND — no USPTO patents for '{company_name}'")
        print(f"              Note: {result.get('note', '')[:80]}")

    return True


def main():
    if len(sys.argv) != 2:
        print("Usage: python scripts/verify_uspto.py <company_name>")
        sys.exit(1)

    company_name = sys.argv[1]

    print("USPTO PatentsView connectivity check")
    print("=" * 50)
    print(f"Company: {company_name}")
    print()

    passed = asyncio.run(run(company_name))

    print()
    print("=" * 50)
    if passed:
        print("Check passed.")
        sys.exit(0)
    else:
        print("Check failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
