# Public jobs feed

The canonical JobHound application authors `server/` and the exported collector
tests. Those are generated files: update the app, run
`scripts/export_public_jobs.py --destination <this repository>`, and commit the
reviewed export with `runtime-manifest.json`. Never fork scraper/domain rules.

`catalog-lock.json` explicitly pins reviewed public catalog recipes by commit,
monitor revision and hash. Regenerate only as an intentional reviewed source
update. Never activate unknown/browser/unverified sources automatically.

No user documents, search preferences, personal database, credentials, browser
cookies or local application state belong here. `feed-state` is exclusively a
generated publication branch. The publisher replaces that branch with a root
commit using an exact lease, preserving only two public snapshot generations
and the current public lifecycle state. Never place authored files there.

Run `python -m server.public_jobs.export --destination . --check` and
`python -m pytest tests -q` before publishing. Real collection is a bounded
network operation, not a unit test. Failed sources preserve records until
expiry; never describe incomplete listings as all openings at a company.
