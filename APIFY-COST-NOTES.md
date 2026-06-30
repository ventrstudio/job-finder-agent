---
title: "Apify Cost Audit — Job Finder"
date: 2026-06-27
last_modified: 2026-06-27
tags: [apify, cost, job-finder, vendor-migration]
project: Job Finder
---

# Apify Cost Audit — Job Finder

Audited 06-27-2026. The job scout was running on the **CoreIndustries** Apify
account by mistake. Core is wound down, so this needs to move. This doc holds the
numbers so the decision survives the session.

## Account facts (at audit)

- **Account:** `CoreIndustries` / admin@coreindustries.io
- **Plan:** STARTER, **$29/mo flat** (usage = prepaid credits inside the $29, not billed on top)
- **Billing cycle:** 06-10-2026 → **07-09-2026** (downgrade/cancel takes effect 07-09; keep using until then)
- **Cycle usage at audit:** $14.65 of $29 (17 days in ≈ $0.86/day)

## What runs

- App: this Job Finder agent, daily GitHub Actions cron `0 12 * * *`
- Actor in use: **`borderline/indeed-scraper`** (`config.py` → `APIFY_ACTOR_ID`)
- Two actor calls per daily run **by design** (local PSL scope + wider scope) — NOT duplicate scrapes

## Cost finding (the important part)

| Path | Est. monthly cost |
|---|---|
| Old: Core STARTER + `borderline/indeed-scraper` | **$29** (job alone ≈ $26/mo, eats the whole plan) |
| Move account only (VENTR or personal STARTER) | $29 — **no savings**, just changes the payer |
| **Swap actor → `memo23/apify-indeed-cheerio-ppr`, maxJobs=80, free account** | **~$3.4/mo** (fits the $5 free-tier credit) |

`borderline/indeed-scraper`: $0.40–1.26 per run.

### memo23 real cost — MEASURED 06-30-2026 (the earlier "$0.008/run" was wrong)

The old note said memo23 = $0.008/run. That does NOT hold for our config. memo23
is the "bypass 25-cap" scraper: it pages the **full** result set of each search
URL, then trims to `maxJobs`. So cost scales with `maxJobs`, not with the number
of URLs. Measured, production conditions (`fromage=1`, `expandToCities=False`,
RESIDENTIAL proxy):

| maxJobs | jobs returned | run cost | ≈ /mo (daily) |
|---|---|---|---|
| 150 | 150 | $0.1945 | ~$5.8 (over free tier) |
| 80 | ~100 (soft cap, 18 URLs) | ~$0.14 | ~$4.2 (thin margin) |
| 30 | 30 | $0.0445 | ~$1.3 |
| **60 (chosen)** | ~75 | **~$0.10** | **~$3.1 (free tier, good margin)** |

Model: **~$0.0014 per job returned.** `maxJobs` caps the crawl (the cost), not
just the output — confirmed by the 150-vs-30 runs. **Caveat:** with many
`startUrls` it's a SOFT cap — a live 18-URL CI run at maxJobs=80 returned ~100
raw items (it finishes each URL's current page batch, ~+25% overshoot). So size
maxJobs ~20% under the item count you actually want. `config.py`
`APIFY_MAX_ROWS_GLOBAL` = memo23 `maxJobs`, set to **60** (≈75 actual items).

**Data-quality: VERIFIED equivalent.** Every field we save (positionName→title,
company, jobDescription→description, jobId→job_id, location, salaryMin/Max,
remote→is_remote, jobUrl, datePublished, jobType) is populated ≥99% of items.
Only `level` degrades (borderline had a clean attributes array; memo23 doesn't) —
now best-effort parsed from title/requirements text. The ≥50-char description
filter survives (1/150 items short).

**The actor swap is the only real savings. The account move is bookkeeping.**

## Waste check — mostly clean

- No recurring duplicate scrapes. The 2 back-to-back daily runs are intentional (two search scopes).
- One-off dev churn on two debug days: 06-16 (4 runs) and 06-19 (6 runs, incl 1 FAILED + a $2.04 outlier). ~$3–4 burned, already over. Not structural.

## State correction (06-30-2026)

The Core Apify account is **not** actually shut down. The token still
authenticates — account `CoreIndustries` / admin@coreindustries.io, still on
**STARTER**, plan cancels at cycle end **07-09-2026**. So the cron is NOT broken
yet; it has a ~9-day window. After 07-09 a cancelled STARTER reverts to the FREE
plan ($5/mo credits), not deletion — so even on the Core account the cheap actor
would keep running. The reason to still move accounts is hygiene: it's tied to a
wound-down entity whose email now only forwards.

## Migration steps

1. ✅ **DONE 06-30** — Swapped `APIFY_ACTOR_ID` → `memo23/apify-indeed-cheerio-ppr`
   in `config.py`, set `APIFY_MAX_ROWS_GLOBAL=80`, rewrote `scraper.py` run_input
   (`startUrls`/`maxJobs`/proxy) and `_map_apify_item` for memo23's output shape.
   Quality verified equivalent, cost model measured (see table above).
2. ⏳ **Account (needs Otis — Apify signup can't be automated):** create a free
   Apify account (personal vs VENTR = Otis's call; this is a personal job tool).
   Generate an API token.
3. ⏳ Replace `APIFY_TOKEN` in `.env` AND in the GitHub Actions secret
   (`gh secret set APIFY_TOKEN -R ventrstudio/job-finder-agent`).
4. ⏳ Confirm the CoreIndustries STARTER cancellation lands 07-09 (or let it lapse
   to free). Nothing to keep paying for once the token is swapped.

## Change Log

| Date | Change |
|------|--------|
| 2026-06-27 | Initial audit. Core STARTER $29, cycle ends 07-09. Found `memo23` actor ~100x cheaper than `borderline`. Account move = no savings; actor swap = the savings. |
| 2026-06-30 | Actor swap BUILT + measured. Corrected the cost model: memo23 is NOT $0.008/run for our config — it's ~$0.0014/job, so cost = the `maxJobs` cap. Chose maxJobs=80 ≈ $3.4/mo (fits free tier). Quality verified equivalent. Core account still alive (STARTER until 07-09). Remaining: free account + token swap (manual). |
