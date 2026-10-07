# JobHound public jobs

A free-to-browse anonymous jobs feed built from reviewed public company sources.
The public Sites board and local JobHound Global Jobs page read the same versioned
feed at `https://crudmaster92.github.io/job-hound-jobs/api/v1/manifest.json`.

The scheduled build runs twice daily at 05:17 and 17:17 UTC, with a manual
workflow trigger for maintainers. Four workers collect at most one concurrent
request per host. Each source has a 180-second/60-request budget; the entire
collection has a 40-minute budget. No AI, credentials or browser automation run.
Workday/SmartRecruiters details use a persistent listing-signature cache and a
20-new-or-changed-posting budget per source/run. Later batches advance through
previously unenriched listings. Sources with failed details remain honest about
description coverage; collection completeness describes listing coverage.

## Reliability and lifecycle

The catalog lock pins one reviewed catalog commit and exact monitor revisions
and hashes. Verified non-browser sources are eligible, including explicitly
partial sources. Excluded and failing sources remain visible in source health.
New revisions require an explicit lock update and deployment.

Jobs have stable company-and-source-URL IDs across overlapping monitors and
collections. Two complete successful absences close a job. Failed and partial
reads never count as closure evidence. Unseen jobs expire after seven days;
expiry does not assert the employer closed the role. Unavailable sources become
stale after 36 hours. Closed/expired public records are removed after 30 days
unseen; a consumer can retain its own favourite snapshot independently.

Builds verify all schemas, hashes, generations and byte limits before replacing
the current manifest. Search pages omit descriptions; details are lazy hashed
pages. Search snippets adapt from at most 2,000 characters to the actual UTF-8
JSON budget and remaining hosting capacity after retaining full details and the
prior immutable snapshot. All jobs, metadata and detail references
remain present; full descriptions are retained unchanged in the detail pages.
Each page is at most seven MB, aggregate search is at most 200 MB, and the
retained publication has a 950 MB safety ceiling, below GitHub Pages' 1 GB limit.
Each new snapshot receives a share of that capacity so optional snippets cannot
crowd out the next build's required metadata. If required metadata alone
cannot fit, publication fails explicitly and preserves the previous generation.
The workflow summary and three-day diagnostic artifact report description
coverage, index bytes, chosen snippet length and publication failure details. Two generations remain so
in-flight readers can finish. All-source failure preserves the published feed.

The dedicated `feed-state` branch persists anonymous state and the last two
published generations; disposable Actions caches are never the source of truth.
It contains only a single root commit to avoid permanently accumulating scraped
content in Git history. Only that generated branch is replaced, with an exact
lease. `main` remains ordinary reviewed source history. State is gzip-compressed
in independently compressed 32 MiB parts so growing lifecycle state cannot
exceed Git's individual-file limit. Ordered parts are verified by size and
SHA-256 before restoration; existing single-file state remains readable.
State is not served from Pages. Pages deployments are atomic.

## Human and agent interfaces

The same manifest and hashed page contracts are available to browsers and
agents without an account or API key. The public board exposes structured
browsing; local agents use the JobHound MCP tools. Public feed data contains no
preferences, application tracking, saved favourites or personal documents.
See `server/public_jobs/schema.py` for exact version-one fields. All artifact
paths resolve relative to `api/v1/manifest.json`; reject unsafe paths, hash
mismatches or generation mismatches. Consumers apply expiry/freshness against
their current clock as well as the recorded publication time.

## Development

```sh
python -m pip install -r requirements-dev.txt
python -m server.public_jobs.export --destination . --check
python -m pytest tests -q
git clone https://github.com/CrudMaster92/job-hound-presets.git .catalog
# Check out catalog-lock.json's catalog_commit before building.
python -m server.public_jobs build --catalog .catalog --lock catalog-lock.json --state .feed-state/state.json --output _site
```

To review an intentional catalog update, check out the desired catalog commit,
run `python -m server.public_jobs lock --catalog .catalog --output catalog-lock.json`,
review the exact sources/revisions, then test and commit the new lock.

The runtime is generated from the canonical JobHound application using its
`scripts/export_public_jobs.py`; `runtime-manifest.json` records file hashes.
This repository does not grant new rights to existing JobHound code or employer
content. Existing notices remain applicable; company trademarks belong to their
owners. GitHub usage limits and source availability still apply. A scheduled
build may be delayed; use manifest/source timestamps to assess freshness.
