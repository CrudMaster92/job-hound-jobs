"""Write compact public build diagnostics without job bodies or user data."""
import json
import os
from collections import Counter
from pathlib import Path

root = Path(__file__).resolve().parents[1]
report_path = root / "_site/build-report.json"
report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
states = {}
error = None
log_path = root / "public-build.log"
if log_path.exists():
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
            if isinstance(value, dict) and "id" in value and "status" in value:
                states[value["id"]] = value["status"]
        except ValueError:
            if line.startswith(("ValueError:", "RuntimeError:", "No sources succeeded")):
                error = line[:1000]
counts = Counter(states.values())
coverage_path = root / '_site/catalog-coverage.json'
coverage = json.loads(coverage_path.read_text('utf-8')) if coverage_path.exists() else None
if coverage:
    report['catalog_coverage'] = {key: len(coverage[key]) for key in ('not_pinned', 'outdated_pins', 'bounded_pagination')}
    report['missing_collections'] = [item['id'] for item in coverage['collections'] if not item['present_in_feed']]
report.update(collection_outcome=os.environ.get("COLLECTION_OUTCOME", "unknown"),
              source_counts=dict(counts), error=error)
folder = root / "diagnostics"
folder.mkdir(exist_ok=True)
(folder / "build-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
lines = ["## Public job feed build", "", f"Collection outcome: {report['collection_outcome']}",
         f"Publication stage: {report.get('stage', 'not reached')}",
         f"Sources: {dict(counts)}"]
for key in ("generation", "jobs", "jobs_with_descriptions", "search_index_bytes", "search_index_budget_bytes",
            "search_text_max_chars", "detail_bytes"):
    if key in report:
        lines.append(f"{key}: {report[key]}")
if error:
    lines.extend(["", f"Failure: {error}", "The last published feed remains available."])
if coverage:
    lines.extend(['', f"Catalog coverage: {report['catalog_coverage']}", f"Missing collections: {report['missing_collections']}"])
    (folder / 'catalog-coverage.json').write_text(json.dumps(coverage, indent=2) + '\n', encoding='utf-8')
else:
    lines.append('Catalog coverage comparison unavailable; absence is not zero missing sources.')
summary = "\n\n".join(lines) + "\n"
print(summary)
if os.environ.get("GITHUB_STEP_SUMMARY"):
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as destination:
        destination.write(summary)
