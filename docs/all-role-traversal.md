# All-role scraper coverage

`ScraperRecipe.coverage_mode` is `bounded` for existing recipes and `all` for
recipes that request source exhaustion. In all mode `pagination.max_pages`
may be null and the historical page and aggregate-response quotas do not
truncate the listing. Offset and page traversal stop on source exhaustion,
detect repeated pages, and retain honest incomplete results. Browser and
next-link recipes cannot opt into this mode yet.

Workday searches may expose only 2,000 results while their facets advertise
many more. The runtime enumerates public facet searches across all available
role families and regions, recursively splits oversized searches, and
deduplicates posting IDs. Completion requires the union of observed posting
IDs to cover the advertised independent count. Changed titles or URL paths do
not create additional roles. Legacy path checkpoints migrate conservatively
without dropping saved jobs. Missing coverage, a saturated
window without a count witness, and source errors cannot prove absence.
Facet searches are traversal partitions, not user search filters.
Workday's public links include the careers-board segment. Its CXS detail
requests use the separate `/job/` path; saved legacy listing links migrate to
the correct public board without inventing titles or changing requisition IDs.
Each split also tracks its own descendant union and refreshes the parent count.
Summing overlapping bucket counts is insufficient proof. If the descendant
union falls short, configured requisition searches repair that parent query;
unrelated or older global observations cannot fill its coverage gap.

An explicitly researched Workday recipe can configure
`metadata.workday_search_prefixes` as a JSON list of requisition search prefixes.
When a leaf still exceeds the source window, the runtime searches these prefixes
and recursively appends digits to saturated prefixes. Searches may overlap;
their deduplicated union must cover the leaf's independently advertised count.
A fresh leaf check and root check run after traversal. Missing prefixes cannot
produce a complete result merely because their individual searches finished.
The fallback does not replace a recipe's pre-existing search text.
Sources whose search engine does not support requisition prefixes instead use
an explicitly researched JSON list in `metadata.workday_search_terms`. These
finite keyword searches retain native facet subdivision and do not assume that
appending digits narrows their results. An incomplete facet parent's scoped
observations seed its repair, preserving evidence already obtained for that
exact query. A root union may finish remaining redundant partitions only after
a fresh root count verifies its coverage; unscoped global cached jobs cannot
fill that proof. Completeness describes the dated crawl snapshot; third-party
sources can change during traversal.
Repair pagination tracks its own fetched IDs separately from inherited parent
evidence, so a page containing known roles does not stop the repair early.
An independently rechecked child union can finish its remaining overlapping
searches. Other scopes remain queued, and only a verified root union establishes
whole-source coverage.
Completed queries below the source window are memoized within that crawl
generation. An identical query first reads a fresh first page and count;
only a matching count can reuse its proven ID set. Changed counts, capped
queries and incomplete queries are traversed again. Cached jobs keep their
original observation timestamps, and recipe changes or a new listing generation
discard the memoized evidence.

Bare Workday entries sometimes expose only a requisition ID and location. They
remain in the checkpoint and keep coverage incomplete instead of being silently
dropped. `metadata.workday_missing_posting_url` can configure an official public
detail URL containing `{source_id}`. Recovery requires a JobPosting with the
exact ID and hiring employer, runs through the existing host/DNS/size protections,
and resumes after work slices. Missing public metadata remains an explicit gap;
the runtime never invents a title or application link. Additional ownership
predicates require their own evidence and cannot be replaced by this fallback.

All mode fetches every eligible description rather than applying the historical
100/250-posting ceiling. Explicit `metadata.detail_fetch_limit: 0` remains a
listing-only validation seam. Source failures use the existing retry/backoff
cache and preserve previously obtained descriptions. A successful listing
does not guarantee that every third-party detail endpoint succeeds.

The public collector's request/time budgets are work slices. Listing checkpoints
live under `__jobhound_listing_v1__` inside its existing per-source private
cache, alongside description retry state. Successful slices resume behind
other monitors within the build budget; the final checkpoint persists through
the existing state writer for subsequent builds. A recipe change invalidates
the listing checkpoint. A failed continuation retains the results already
obtained in the build. Partial listings never confirm unseen jobs closed.

Description or missing-metadata continuations from a prior listing snapshot are marked partial;
they cannot count as a fresh absence check. Once enrichment is finished, the
checkpoint is cleared so the next run fetches a fresh listing. Local human,
API, MCP and scheduled runs use the same typed recipe and `run_scraper` backend.
Direct runtime callers supply `detail_cache` when they want resumable slices.

Local human/API/MCP and scheduled runs persist the same source checkpoint and
description cache in the application's private SQLite database. A typed
`ScrapeResult.continuation_ready` marks a work-budget pause. The scheduler saves
and ingests that slice, then queues the next behind existing work until source
exhaustion. It does not turn source errors or retry backoff into busy loops.
Pausing or archiving the monitor prevents automatic continuation. Run progress
contains `continuation_run_id`; MCP status supplies the next polling step.
Cached rows retain their actual source observation time, and SQLite absence
checks use an indexed temporary set rather than one parameter per role.
Once an unavailable metadata entry exhausts its eligible work, the next normal
run starts a fresh listing rather than freezing discovery on that entry. The
public collector uses the same typed continuation signal, so a terminal gap
does not restart whole-source scans repeatedly within one build.

HTTP host confinement, DNS/public-IP checks, TLS rules, per-response size
limits, employer ownership checks, host pacing and source diagnostics remain
in force. These protect requests; they do not set a maximum number of roles.
The application remains the authored runtime. Export `traversal.py`, `missing_postings.py`,
`workday_inventory.py`, their tests,
and the updated preset schema through `scripts/export_public_jobs.py` before
publishing consumers that use all-mode recipes. Older runtime versions reject
the new recipe field rather than silently applying older coverage ceilings.

## Public requisition inventory fallback

Accenture's all-role recipe can enable `metadata.workday_accenture_inventory`.
The runtime checkpoints the official India careers index, partitions its
10,000-result window by experience and location, and deduplicates requisition
IDs. It confirms missing IDs through quoted Workday searches in batches of 20.
Batch sizes control request size; they never cap the number of roles.
Unconfirmed inventory records and inventory counts do not create jobs, prove Workday coverage, or
close absent jobs. Only confirmed company-owned Workday postings contribute to
the source union. Other countries still use Workday traversal. Unsupported
configuration, malformed inventory responses and unavailable IDs remain
incomplete; work-budget pauses resume the inventory checkpoint.
For an already confirmed bare Workday entry, a matching public index record
can recover its title and description. The public detail URL must match the
exact requisition and official host. Unconfirmed inventory IDs remain hints;
metadata recovery never adds an ID to the Workday coverage proof.
Remaining bare entries are looked up by exact requisition ID through country
search pages advertised on Accenture's official site. Country and language
parameters come from each page's published configuration; the matching public
detail link must use that locale and language suffix. This phase checkpoints
its country cursor and ID batches. It does not broaden a configured Workday
search or use private criteria. Unavailable regional metadata stays incomplete.
