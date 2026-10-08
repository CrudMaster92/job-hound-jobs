# Public board coverage and capacity review — October 8, 2026

The old feed pinned 659 known sources from six collections. The current catalog
contains 733 monitors in nine collections. Catalog publication did not update
the collector lock, leaving 74 IDs absent and three admitted revisions outdated.

The proposed version-two lock preserves every previously admitted source and
its exact historical recipe unless it was freshly revalidated in this review.
It refreshes all collection memberships, admits 74 additional sources, and
updates Applied Materials, Broadcom and Intel to their current reviewed revisions.
There are 596 admitted sources and 137 explicit exclusions. All 733 catalog
monitor IDs are accounted for; no source is silently missing from the lock.

| Collection | Admitted | Excluded |
| --- | ---: | ---: |
| Advanced Technology, Science & Engineering | 27 | 0 |
| Big Law | 20 | 0 |
| Technology, Data & AI Consulting | 30 | 1 |

Fresh canonical public-only execution checked all 78 monitors in these collections.
Seventy-seven returned 28,884 job observations. Xebia Latin America returned a
complete empty listing and remains unverified/excluded. Counts are dated observations,
can overlap, and are not a guarantee of exhaustive employer coverage. No private
criteria, profiles, application history, credentials or personal database were used.
Employer ownership, representative titles and source URLs were reviewed.
[Catalog review and full public evidence](https://github.com/CrudMaster92/job-hound-presets/pull/26)
contain the individual observations and warnings. Partial listings never provide
absence or closure evidence.

The public collector now resumes supported JSON page/offset traversal beyond
old recipe page bounds. Request parameters, ownership rules, allowed hosts,
pacing, work budgets and explicit partial-listing metadata remain enforced.
Real old-pin smoke runs returned Adobe 406, AMD 1,298, NVIDIA 2,673 and PPG 175
jobs; provider metadata gaps and result windows remain visible as partial coverage.

The workflow publishes losslessly compressed detail shards. All 1,103 detail
pages of captured generation `20261008T125338-37780064388-1` round-tripped with
their hashes verified: 393,342,550 bytes became 133,157,244 bytes, preserving all
110,230 jobs and full descriptions. The supported 200 MB search and 990 MB
retained-publication budgets remain explicit. Optional snippets adapt; jobs are
never dropped to fit the budget. Readers no longer impose separate 500-page
or 200,000-row cutoffs.

An offline publication stress check merged the captured public generation with
the fresh validation results. It preserved all 110,230 original IDs and produced
138,602 stored jobs, including 28,372 additional unique IDs, across 555 search
pages. Both snapshots together used 849,823,319 API bytes, below 990 MB. The
new index used 199,961,744 bytes and full compressed details 154,250,883 bytes.
All publication schemas, hashes, generations and counts passed. The canonical
reader loaded every record, returned the last active result at offset 97,911,
and verified the final compressed detail page's full descriptions. These are
simulation results, not a claim that the production feed has already changed.

## Rollout order

1. Merge the catalog evidence review and retain its exact reviewed JSON hashes.
2. Publish [workspace preview 7](https://github.com/CrudMaster92/job-hound-workspace/pull/3).
   Rebuild/restart the canonical app and update installed workspace readers with
   their existing durable homes. Older installed readers cannot read gzip details.
3. Update the lock's catalog commit to the actual merged catalog state if merge
   strategy changes it; verify all individual hashes and source selections again.
4. Merge this collector/lock update. The main-branch workflow runs collection and
   publishes only after schema, hash, generation and byte-budget validation.
5. Inspect the successful new manifest, source timestamps, collection counts and
   representative jobs. A prepared PR or an empty source does not establish that
   new roles have already appeared in production.

The Site reader update is published at
[Job Hound](https://job-hound.realperson.chatgpt.site/jobs).
Canonical app tests, reader regressions, both production builds, exported-runtime
checks, 121 collector tests, fresh Windows workspace installation and GitHub Linux
workspace smoke passed. Fresh Muse-host lifecycle remains unobserved.
