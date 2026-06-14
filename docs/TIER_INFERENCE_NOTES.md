# Tier Inference: Operational Notes

This document records what we've learned about `_infer_tier()` coverage from real pipeline runs —
which domain categories hit the unknown bucket, why, and what the fix pattern is. It exists so
this knowledge doesn't live only in PR descriptions.

## How tier inference works

`_infer_tier(url)` maps a URL's domain to one of four tiers:

| Tier | Meaning | Examples |
|------|---------|---------|
| `primary_document` | Originating, authoritative source | SEC EDGAR, court records, official govt filings, official company documentation |
| `reputable_secondary` | Established editorial standards | Wire services, major newspapers, recognized trade press, law firm publications |
| `aggregator` | Compiles from other sources | Crunchbase, PitchBook, niche directories, data tools |
| `community` | User-generated or unmoderated | Reddit, Glassdoor, Fandom wikis, complaint sites |

Resolution order: (1) source registry, (2) explicit domain sets, (3) generic `*.gov` fallback,
(4) `UNKNOWN`.

Domain sets live in `src/synthesis/assembler.py`. The `_REPUTABLE_SECONDARY`, `_AGGREGATOR`,
and `_COMMUNITY` sets use subdomain matching: adding `fandom.com` catches
`starship-spacex.fandom.com` automatically.

## Why unknown coverage varies by target

**Short answer:** the domain tables were seeded against a handful of targets (SpaceX, Stripe), so
any new target brings its own source mix that wasn't anticipated.

**The four categories of unknown-bucket domains** (observed across SpaceX and OpenAI runs):

### Category 1 — The target company's own domain

The most predictable gap. When researching OpenAI, agents cite `openai.com`, `developers.openai.com`,
`help.openai.com`, `status.openai.com` — all unknown until that company was first run. The same
will happen for every new target: their own domain isn't in the table yet.

**Current fix:** add it as observed (e.g., `openai.com` added after the first OpenAI run).

**Permanent fix (planned, not yet built):** at assembly time, infer the target company's primary
domain from the classifier output or the `website` field in research output, and classify it as
`primary_document` automatically. This eliminates category 1 without manual additions per company.
See the PR note from v0.7.2 — tracked as a small post-P4 item.

### Category 2 — AI-adjacent niche aggregators

A cluster of low-editorial-standard sites that orbit AI topics: company profile tools,
social-media analytics platforms, AI-generated content sites, release trackers. Examples:
`highperformr.ai`, `crescendo.ai`, `releasebot.io`, `tweetstorm.ai`, `enterprise-ai.io`,
`amperly.com`.

These are consistently `aggregator` tier — they compile from other sources, have no original
journalism, and have weak or absent editorial standards. They show up on AI company runs
specifically because agents find them when searching for employee counts, follower lists, and
product release histories.

**Pattern:** when you see `*.ai` domains that aren't research institutions or established outlets,
default to `aggregator`. They're rarely reputable_secondary.

### Category 3 — Vertical B2B press

Legitimate trade publications with editorial standards that don't appear in mainstream news lists.
Examples from real runs:
- Payments: `pymnts.com`
- SaaS/startup: `saastr.com`
- Cloud-native/developer: `thenewstack.io`
- Law firm insights: `grellas.com`, `wsgr.com`, `nelsonmullins.com`

These are `reputable_secondary`. They have bylined articles, editorial review, and domain expertise.
The law firm publications are particularly valuable for legal and regulatory risk coverage.

**Pattern:** each new industry vertical will have 3–5 trade outlets that agents consistently
reach for. A fintech run will surface `pymnts.com` (already added) plus likely `coindesk.com`,
`theblockcrypto.com`, `ledgerinsights.com`. A healthcare run will surface STAT News, Fierce
Healthcare, etc. Add them as they appear.

### Category 4 — Regional and local media

Local TV stations and regional newspapers that appear when the target company has visible physical
operations in a specific geography. Examples: `expressnews.com`, `floridatoday.com`,
`valleycentral.com` — all Texas/Florida media that covered SpaceX's Starbase operations and
OSHA investigations.

These are `reputable_secondary` despite smaller reach — they're professionally edited news outlets
with editorial standards, often with exclusive coverage of local developments that national media
doesn't report.

**Pattern:** any company with manufacturing, construction, or regulatory activity in a specific
region will generate local media coverage. The local outlets will be unknown until that region
is first encountered.

## Coverage targets and measurement

**Goal:** under 20% unknown on any target, under 15% on repeatedly-run targets.

**Run history:**

| Version | Target    | Unknown before | Unknown after | Note |
|---------|-----------|----------------|---------------|------|
| v0.7.2  | SpaceX    | 19% (8/42)     | 0%            | seed target |
| v0.7.2  | OpenAI    | 59% (24/41)    | 0%            | seed target |
| v0.7.3  | Anthropic | 29% (29/99)\*  | 0%            | out-of-distribution verification |

\* 29% was the live-run figure before the expansion pass. After adding 13 domains from the Anthropic
run, all three targets show 0% unknown (verified by re-applying updated `_infer_tier()` to existing
report JSONs).

**Generalization verdict (v0.7.3):** Anthropic came in at 29.3% unknown on first run — under the
30% acceptable ceiling but over the 20% target, triggering a second expansion pass. After that pass,
the table covers all three targets at 0%. The dominant new categories were the target company's own
domain (`anthropic.com`, `claude.com`), AI-adjacent pricing/statistics aggregators, and one major
missing mainstream outlet (CNN). The table is not overfit to the seed targets.

## Adding new domains

**Rules:**
1. Only add domains that appeared in actual pipeline runs (not speculative).
2. When uncertain between two adjacent tiers, use the lower one (aggregator, not reputable_secondary).
3. Add a comment explaining the domain if it's not immediately recognizable.
4. One domain entry handles all subdomains via the `endswith` rule — don't add subdomains separately.

**Process:**
```python
# Pull unknown domains from a report JSON
python3 -c "
import json
from urllib.parse import urlparse
from collections import defaultdict

data = json.load(open('outputs/report_TARGET.json'))
unknowns = set()

def walk(obj):
    if isinstance(obj, dict):
        if obj.get('tier') == 'unknown' and 'url' in obj:
            unknowns.add(urlparse(obj['url']).netloc.lower().removeprefix('www.'))
        for v in obj.values(): walk(v)
    elif isinstance(obj, list):
        [walk(i) for i in obj]

walk(data)
print('\n'.join(sorted(unknowns)))
"
```

Then classify each against the four-tier taxonomy and add to the appropriate set in
`src/synthesis/assembler.py`. Run the parametrized tests to verify, and add new test cases
for each domain added.
