# Reviewed public source admission — October 3, 2026

The prior feed lock admitted 433 sources from a catalog pin predating Fortune 500
and several working AI/Gaming/Marketing verifications. This update admits 78
additional sources after fresh canonical runtime execution and HTTPS posting URL
validation. All 433 existing source pins retain identical recipe hashes and
revisions; no existing source is removed. The resulting lock admits 511 sources.

The normalized catalog is pinned to main commit
`6888898b190440a81c5ba86d75bd80788b601ac8`. The adjacent JSON contains exact
recipe hashes, run dates, counts, pages, completeness, warnings, filters and sample
job URLs for all 80 admission candidates. AGCO remains excluded after zero jobs;
Packaging Corp. of America remains excluded after a malformed posting URL.
Browser sources and detail-phase ownership predicates remain excluded. Partial
listing flags retain closure protections. Full descriptions may remain incomplete.

The latest production build failed after successful scraping: the new snapshot's
148.6 MB index plus 292.4 MB details exceeded the total 800 MB budget when added
to the prior 409.9 MB snapshot. The canonical publisher now reduces only the new
index's optional description snippets to the remaining total hosting capacity.
Every job, full detail description and the prior immutable snapshot is retained.
Required metadata or detail overflow still fails and preserves publication.

This is a selective canonical publication patch exported through the existing
25-file release allowlist. Only `publish.py` and its collector regression tests
advance; the other released runtime modules stay pinned. The separate community
contribution infrastructure and automatic admission rollout remain unchanged.

Validation: exact lock content/identity checks for 511 monitors, generated runtime
integrity and 39 public-runtime tests, including shared snapshot capacity and
atomic failure behavior. Canonical application tests and web build are also run.
Successful catalog deployment and the subsequent public generation are separate
release gates; merged sources are not reported as published before observation.
