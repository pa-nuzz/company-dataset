# Company Dataset

A curated dataset of **22,682+ verified companies** hiring through Greenhouse, Lever, Ashby, SmartRecruiters, or Breezy HR — built from public datasets and vendor APIs.

## Current Stats (2026-11-05)

| Metric | Count |
|--------|-------|
| Verified companies | 22,682 |
| Incomplete (missing fields) | 6,652 |
| Unresolved boards | 1,827 |
| **Total unique companies** | **29,334** |
| Confirmed ATS boards | 30,340 |

**By ATS (verified):** Greenhouse 7,049 • SmartRecruiters 6,369 • Ashby 4,261 • Breezy 3,053 • Lever 1,950

Data exported to `data/exports/final_2026-11-05/` (not committed to git).

## What "Verified" Means

A company is **verified** when:
1. Its ATS board responds to the vendor API (live)
2. Identity is confirmed (vendor record + cross-reference)
3. All four required fields present: `name`, `description`, `ats_system`, `ats_code`

Optional fields (`website_url`, `linkedin_url`, `careersite_url`) are included where available.

## Data Files (per export)

| File | Description |
|------|-------------|
| `companies_verified.csv` | Complete records (7 columns) |
| `companies_incomplete.csv` | Live boards with gaps + `missing_fields` |
| `unresolved_boards.csv` | Boards with no confirmed identity |
| `evidence.csv` | Field-level provenance for every value |
| `qa_report.json` | Counts, coverage, rejection reasons |
| `sources.json` | Source manifests with licenses/checksums |
| `SHA256SUMS` | Integrity hashes |

Review artifacts: `data/review/` (duplicates, uncertain, dead boards, demoted rows).

## Sources

**Board discovery (slugs):**

| Source | License |
|--------|---------|
| latmay/ats-career-page-urls (HF) | CC BY 4.0 |
| LastRound AI ATS directory | CC BY 4.0 |
| Headcount Labs job postings (HF) | CC BY 4.0 |
| kalebconfer job-board-directory | CC0 |
| moonie0201/ats-directory | CC0 |
| Feashliaa/job-board-aggregator | MIT |
| veresk06/open-ats-feed | MIT |
| Common Crawl (41 crawls, 2020–2026) | CC Terms |

**Company details:**

| Source | License | Used for |
|--------|---------|----------|
| BigPicture free (2023 Q4) | ODC-By 1.0 | LinkedIn handle, HQ country, website lookup |

## Methodology

- **No fabrication**: Every board was queried live against the vendor API. Empty fields stay empty — companies with gaps go to `incomplete`.
- **Deduplication**: Boards merged by shared website, or shared LinkedIn + matching name. Name-only matches kept separate (e.g., 3 unrelated "Patch" companies).
- **Descriptions**: Homepage meta description (any language) → Greenhouse board intro. Recruiting copy, cookie banners, placeholders filtered. Truncated at sentence boundary.
- **Verification tiers**: A (open jobs), B (Lever region mismatch), C (zero jobs + independent evidence).

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Requires Python 3.11+, `requests`, `duckdb`.

## Pipeline

The pipeline operates over `data/store.db` in four stages:

```bash
# 1. Discovery (resumable, writes data/raw/<source>/tokens.csv)
python -m company_dataset.sources.wayback_cdx --raw data/raw
python -m company_dataset.sources.commoncrawl_indexer --raw data/raw
python -m company_dataset.sources.slug_prober --raw data/raw --db data/store.db --limit-per-vendor 5000

# 2. Ingest
company-dataset ingest --db data/store.db --raw data/raw --only wayback_cdx_2026_11_03

# 3. Enrich + Quality
company-dataset enrich --db data/store.db --raw data/raw --run myrun --out data/runs/myrun --per-ats 100000 --only-undecided
company-dataset repair descriptions-cached --db data/store.db --run mydesc --from-runs myrun
company-dataset repair final-checks --db data/store.db --run myqa --from-runs myrun,mydesc

# 4. Export
company-dataset export --db data/store.db --runs myrun,mydesc,myqa --fresh-origin myqa --out data/exports/final_YYYY-MM-DD
```

Each value has a row in `field_evidence` with its source. Corrections append under new run labels — no history rewriting.

## Why Not 30,000+?

Merging all public sources yields ~31,800 distinct board slugs. ~50% no longer exist when queried against vendor APIs (company moved ATS, shut down, or slug was wrong). Common Crawl, GitHub lists, and domain-based slug guessing each added only hundreds — sources overlap heavily (>80% of live boards appear in 2+ sources). The remainder would require paid data or brute-force API enumeration.

## License

This dataset aggregates public sources. Each source retains its original license (see `sources.json` per export). When redistributing, include `source_manifest.csv` for attribution.

---

**Last updated:** 2026-11-05  
**Export:** `data/exports/final_2026-11-05/`