"""Build and verify immutable pages before switching the manifest pointer."""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from .schema import FeedManifest, PublicJob, digest, json_bytes, timestamp

PAGE_SIZE = 250
DETAIL_PAGE_SIZE = 100
MAX_PUBLISHED_BYTES = 800_000_000
MAX_SHARD_BYTES = 7_000_000
MAX_INDEX_BYTES = 60_000_000


def _groups(records: list[dict], count_limit: int):
    group, size = [], 100
    for record in records:
        record_size = len(json_bytes(record)) + 1
        if record_size + 100 > MAX_SHARD_BYTES:
            raise ValueError("Individual job exceeds the shard byte budget")
        if group and (len(group) >= count_limit or size + record_size > MAX_SHARD_BYTES):
            yield group
            group, size = [], 100
        group.append(record)
        size += record_size
    if group:
        yield group


def _write(path: Path, value: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json_bytes(value)
    path.write_bytes(content)
    return digest(content)


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
        raise ValueError("Search index exceeds the 60 MB byte budget")
    if sum(path.stat().st_size for path in api.rglob("*") if path.is_file()) > MAX_PUBLISHED_BYTES:
        raise ValueError("Publication exceeds its 800 MB safety budget")


def publish(state: dict, lock: dict, output: Path, *, generation: str, now) -> dict:
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
        relative = f"snapshots/{generation}/details-{page:04}.json"
        ref = {"path": relative, "sha256": _write(api / relative, {"generation": generation, "jobs": group}), "count": len(group)}
        detail_refs.append(ref)
        for job in group:
            row = {key: value for key, value in job.items() if key != "description"}
            row["search_text"] = job["description"][:2000]
            row["detail_ref"] = {"path": ref["path"], "sha256": ref["sha256"]}
            search_rows.append(row)
    search_refs = []
    for page, group in enumerate(_groups(search_rows, PAGE_SIZE), 1):
        relative = f"snapshots/{generation}/search-{page:04}.json"
        search_refs.append({"path": relative, "sha256": _write(api / relative, {"generation": generation, "jobs": group}), "count": len(group)})
    manifest = FeedManifest(
        generation=generation, generated_at=timestamp(now), catalog_commit=lock["catalog_commit"],
        total_jobs=len(jobs), active_jobs=sum(job["status"] == "active" for job in jobs),
        collections=lock["collections"], sources=list(state["sources"].values()),
        search_pages=search_refs, detail_pages=detail_refs,
    ).model_dump(mode="json")
    validate_publication(api, manifest)
    _write(generation_path / "manifest.json", manifest)
    pending = api / "manifest.pending.json"
    _write(pending, manifest)
    os.replace(pending, api / "manifest.json")
    return manifest


def retain_generations(output: Path, keep: int = 3) -> None:
    """Keep current and prior snapshots so in-flight clients can finish."""
    root = (output / "api" / "v1" / "snapshots").resolve()
    if not root.exists():
        return
    generations = sorted((path for path in root.iterdir() if path.is_dir()),
                         key=lambda path: path.name, reverse=True)
    for path in generations[keep:]:
        resolved = path.resolve()
        if resolved.parent != root or path.is_symlink():
            raise ValueError("Unsafe generation retention target")
        shutil.rmtree(resolved)
