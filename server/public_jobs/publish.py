"""Build and verify immutable pages before switching the manifest pointer."""
from __future__ import annotations

import json
import gzip
import io
import os
import re
import shutil
from pathlib import Path

from .schema import FeedManifest, PublicJob, digest, json_bytes, timestamp

PAGE_SIZE = 250
DETAIL_PAGE_SIZE = 100
MAX_PUBLISHED_BYTES = 990_000_000
RETAINED_GENERATIONS = 2
MAX_SHARD_BYTES = 7_000_000
MAX_INDEX_BYTES = 200_000_000
MAX_SEARCH_TEXT_CHARS = 2000


def _groups(records: list[dict], count_limit: int):
    group, size = [], 256
    for record in records:
        record_size = len(json_bytes(record)) + 1
        if record_size + 256 > MAX_SHARD_BYTES:
            raise ValueError("Individual job exceeds the shard byte budget")
        if group and (len(group) >= count_limit or size + record_size > MAX_SHARD_BYTES):
            yield group
            group, size = [], 256
        group.append(record)
        size += record_size
    if group:
        yield group



def _fit_search_text(rows: list[dict], generation: str, *, byte_budget: int | None = None) -> tuple[int, int]:
    """Fit optional snippets to the actual UTF-8 JSON budget, never drop jobs.

    Full descriptions remain unchanged in detail pages. Metadata and detail
    references always survive; only the optional description search prefix is
    shortened uniformly when the collection grows.
    """
    texts = [row["search_text"] for row in rows]
    budget = MAX_INDEX_BYTES if byte_budget is None else min(MAX_INDEX_BYTES, byte_budget)

    def measure(limit: int) -> int:
        for row, text in zip(rows, texts):
            row["search_text"] = text[:limit]
        return sum(len(json_bytes({"generation": generation, "jobs": group}))
                   for group in _groups(rows, PAGE_SIZE))

    size = measure(MAX_SEARCH_TEXT_CHARS)
    if size <= budget:
        return MAX_SEARCH_TEXT_CHARS, size
    base_size = measure(0)
    if base_size > budget:
        raise ValueError(f"Required search metadata alone exceeds the {budget}-byte budget "
                         f"({base_size} bytes for {len(rows)} jobs); no jobs were dropped")
    low, high = 0, MAX_SEARCH_TEXT_CHARS - 1
    while low < high:
        middle = (low + high + 1) // 2
        if measure(middle) <= budget:
            low = middle
        else:
            high = middle - 1
    return low, measure(low)


def _write(path: Path, value: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json_bytes(value)
    if path.name.endswith(".json.gz"):
        content = gzip.compress(content, compresslevel=6, mtime=0)
    path.write_bytes(content)
    return digest(content)


def _generations(root: Path):
    return sorted((path for path in root.iterdir() if path.is_dir()),
                  key=lambda path: path.name, reverse=True) if root.exists() else []


def validate_publication(api: Path, manifest: dict) -> None:
    FeedManifest.model_validate(manifest)
    total = 0
    index_bytes = 0
    for ref in [*manifest["search_pages"], *manifest["detail_pages"]]:
        path = (api / ref["path"]).resolve()
        if not path.is_relative_to(api.resolve()):
            raise ValueError("Feed artifact escaped publication root")
        content = path.read_bytes()
        if len(content) > MAX_SHARD_BYTES:
            raise ValueError("Feed shard exceeds the seven MB byte budget")
        if ref in manifest["search_pages"]:
            index_bytes += len(content)
        if digest(content) != ref["sha256"]:
            raise ValueError("Feed artifact hash mismatch")
        if path.name.endswith(".json.gz"):
            with gzip.GzipFile(fileobj=io.BytesIO(content)) as stream:
                content = stream.read(MAX_SHARD_BYTES + 1)
            if len(content) > MAX_SHARD_BYTES:
                raise ValueError("Expanded feed shard exceeds the seven MB byte budget")
        page = json.loads(content)
        if page["generation"] != manifest["generation"] or len(page["jobs"]) != ref["count"]:
            raise ValueError("Feed artifact generation/count mismatch")
        if ref in manifest["detail_pages"]:
            for job in page["jobs"]:
                PublicJob.model_validate(job)
            total += len(page["jobs"])
    if total != manifest["total_jobs"]:
        raise ValueError("Manifest job count mismatch")
    if index_bytes > MAX_INDEX_BYTES:
        raise ValueError(f"Search index exceeds the {MAX_INDEX_BYTES}-byte budget")
    # Staging also contains older generations. Budget only the current and
    # previous generation that will be uploaded, without pruning
    # anything before the new generation has passed validation.
    snapshots = api / "snapshots"
    retained = set(_generations(snapshots)[:RETAINED_GENERATIONS])
    published_bytes = sum(path.stat().st_size for path in api.rglob("*") if path.is_file()
                          and (not path.is_relative_to(snapshots)
                               or snapshots / path.relative_to(snapshots).parts[0] in retained))
    if published_bytes > MAX_PUBLISHED_BYTES:
        raise ValueError(f"Publication exceeds its {MAX_PUBLISHED_BYTES}-byte safety budget")


def publish(state: dict, lock: dict, output: Path, *, generation: str, now, compress_details: bool = False) -> dict:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", generation):
        raise ValueError("Invalid generation identifier")
    api = output / "api" / "v1"
    generation_path = api / "snapshots" / generation
    # Published generations are immutable; retry by using a new build ID.
    if generation_path.exists():
        raise ValueError("Generation already exists")
    jobs = [PublicJob.model_validate(entry["job"]).model_dump(mode="json") for _, entry in sorted(state["jobs"].items())]
    detail_refs, search_rows = [], []
    for page, group in enumerate(_groups(jobs, DETAIL_PAGE_SIZE), 1):
        relative = f"snapshots/{generation}/details-{page:04}.json" + (".gz" if compress_details else "")
        # Optional nulls deserialize to the same contract defaults. Omit their
        # repeated field names without shortening descriptions or dropping jobs.
        compact = [{key: value for key, value in job.items() if value is not None} for job in group]
        ref = {"path": relative, "sha256": _write(api / relative, {"generation": generation, "jobs": compact}), "count": len(group)}
        detail_refs.append(ref)
        for job in group:
            # Optional absent values carry no search information. Consumers
            # already accept missing optional fields. Details preserve every
            # populated value, including full descriptions.
            row = {key: value for key, value in job.items() if key != "description" and value is not None}
            row["search_text"] = job["description"][:MAX_SEARCH_TEXT_CHARS]
            row["detail_ref"] = {"path": ref["path"], "sha256": ref["sha256"]}
            search_rows.append(row)
    def make_manifest(search_refs):
        return FeedManifest(
            generation=generation, generated_at=timestamp(now), catalog_commit=lock["catalog_commit"],
            total_jobs=len(jobs), active_jobs=sum(job["status"] == "active" for job in jobs),
            collections=lock["collections"], sources=list(state["sources"].values()),
            search_pages=search_refs, detail_pages=detail_refs,
        ).model_dump(mode="json")

    # Keep the previous immutable snapshot, full descriptions and every job.
    # Fit only this generation's optional snippets to the remaining hosting
    # capacity. Reserve both new manifest copies before replacing either one.
    anticipated_refs = [{"path": f"snapshots/{generation}/search-{page:04}.json",
                         "sha256": "0" * 64, "count": PAGE_SIZE}
                        for page, _ in enumerate(_groups(search_rows, PAGE_SIZE), 1)]
    manifest_reserve = 2 * len(json_bytes(make_manifest(anticipated_refs)))
    snapshots = api / "snapshots"
    retained = set(_generations(snapshots)[:RETAINED_GENERATIONS])
    existing_bytes = sum(path.stat().st_size for path in api.rglob("*") if path.is_file()
                         and path.name not in {"manifest.pending.json"}
                         and path != api / "manifest.json"
                         and (not path.is_relative_to(snapshots)
                              or snapshots / path.relative_to(snapshots).parts[0] in retained))
    detail_bytes = sum((api / ref["path"]).stat().st_size for ref in detail_refs)
    # Share capacity across retained snapshots instead of letting today's
    # optional snippets consume the space tomorrow's required metadata needs.
    snapshot_budget = (MAX_PUBLISHED_BYTES - manifest_reserve) // RETAINED_GENERATIONS
    index_budget = min(MAX_INDEX_BYTES, MAX_PUBLISHED_BYTES - existing_bytes - manifest_reserve,
                       snapshot_budget - detail_bytes - manifest_reserve // 2)
    snippet_chars, index_bytes = _fit_search_text(search_rows, generation, byte_budget=index_budget)
    report = {"generation": generation, "stage": "index_built", "jobs": len(jobs),
              "jobs_with_descriptions": sum(bool(job["description"].strip()) for job in jobs),
              "search_index_bytes": index_bytes, "search_index_budget_bytes": index_budget,
              "search_text_max_chars": snippet_chars,
              "detail_bytes": detail_bytes, "snapshot_budget_bytes": snapshot_budget}
    _write(output / "build-report.json", report)
    print(json.dumps({"publication": report}), flush=True)
    search_refs = []
    for page, group in enumerate(_groups(search_rows, PAGE_SIZE), 1):
        relative = f"snapshots/{generation}/search-{page:04}.json"
        search_refs.append({"path": relative, "sha256": _write(api / relative, {"generation": generation, "jobs": group}), "count": len(group)})
    manifest = make_manifest(search_refs)
    validate_publication(api, manifest)
    _write(generation_path / "manifest.json", manifest)
    pending = api / "manifest.pending.json"
    _write(pending, manifest)
    os.replace(pending, api / "manifest.json")
    report["stage"] = "validated"
    _write(output / "build-report.json", report)
    return manifest


def retain_generations(output: Path, keep: int = RETAINED_GENERATIONS) -> None:
    """Keep current and prior snapshots so in-flight clients can finish."""
    root = (output / "api" / "v1" / "snapshots").resolve()
    if not root.exists():
        return
    generations = _generations(root)
    for path in generations[keep:]:
        resolved = path.resolve()
        if resolved.parent != root or path.is_symlink():
            raise ValueError("Unsafe generation retention target")
        shutil.rmtree(resolved)
