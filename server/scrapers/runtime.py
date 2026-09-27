"""Public orchestration entry points for deterministic scraping and validation."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

from bs4 import BeautifulSoup
from urllib.parse import quote, urlsplit

import httpx
from pydantic import ValidationError

from . import adapters
from .progress import report_progress
from .http import ScraperNetworkError, _validate_target, bounded_request
from .models import JobRecord, RequestConfig, ScrapeResult, ScraperRecipe, ScraperStrategy, ValidationReport


DEFAULT_DETAIL_FETCH_LIMIT = 100
MAX_DETAIL_FETCH_LIMIT = 250
MAX_PAGED_RESPONSE_BYTES = 25_000_000


class ScraperExecutionError(RuntimeError):
    pass


def _parse_response(response: httpx.Response, recipe: ScraperRecipe) -> Any:
    if recipe.strategy in {
        ScraperStrategy.ASHBY, ScraperStrategy.GREENHOUSE, ScraperStrategy.LEVER, ScraperStrategy.SMARTRECRUITERS,
        ScraperStrategy.WORKDAY, ScraperStrategy.GENERIC_JSON,
    }:
        try:
            return response.json()
        except ValueError as exc:
            raise ScraperExecutionError("expected a JSON response") from exc
    return response.text


def _adapt(payload: Any, recipe: ScraperRecipe):
    strategy = recipe.strategy
    if strategy == ScraperStrategy.ASHBY:
        return adapters.ashby(payload, recipe)
    if strategy == ScraperStrategy.GREENHOUSE:
        return adapters.greenhouse(payload, recipe)
    if strategy == ScraperStrategy.LEVER:
        return adapters.lever(payload, recipe)
    if strategy == ScraperStrategy.SMARTRECRUITERS:
        return adapters.smartrecruiters(payload, recipe)
    if strategy == ScraperStrategy.WORKDAY:
        return adapters.workday(payload, recipe)
    if strategy == ScraperStrategy.JSON_LD:
        return adapters.json_ld(payload, recipe)
    if strategy == ScraperStrategy.GENERIC_JSON:
        return adapters.generic_json(payload, recipe)
    if strategy == ScraperStrategy.JOBVITE:
        return adapters.generic_html(payload, recipe, source="jobvite")
    if strategy == ScraperStrategy.GENERIC_HTML:
        return adapters.generic_html(payload, recipe)
    raise ScraperExecutionError(f"strategy {strategy.value} requires browser execution")


def _paged_payloads(recipe: ScraperRecipe, client: httpx.Client | None):
    pagination = recipe.pagination
    if pagination.kind == "none":
        yield bounded_request(recipe.request, recipe.allowed_hosts, client)
        return
    total_response_bytes = 0
    for page in range(pagination.max_pages):
        offset = page * pagination.page_size
        params = dict(recipe.request.params)
        body = dict(recipe.request.json_body or {})
        target = body if recipe.request.method == "POST" else params
        target[pagination.parameter or ("offset" if pagination.kind == "offset" else "page")] = offset if pagination.kind == "offset" else page + 1
        if pagination.page_size_parameter:
            target[pagination.page_size_parameter] = pagination.page_size
        response = bounded_request(recipe.request, recipe.allowed_hosts, client, params=params, json_body=body or None)
        total_response_bytes += len(response.content)
        if total_response_bytes > MAX_PAGED_RESPONSE_BYTES:
            raise ScraperExecutionError("paginated responses exceeded the 25 MB total limit")
        yield response
        payload = _parse_response(response, recipe)
        if isinstance(payload, dict):
            records = next((value for key in ("content", "jobPostings", "jobs")
                            if isinstance((value := payload.get(key)), list)), [])
            count = len(records)
            total = payload.get("total", payload.get("totalCount", payload.get("totalFound", payload.get("hits"))))
        elif isinstance(payload, list):
            count, total = len(payload), None
        elif isinstance(payload, str):
            count, total = len(_adapt(payload, recipe)), None
        else:
            count, total = 0, None
        if count < pagination.page_size or (isinstance(total, int) and total > 0 and offset + count >= total):
            return
    if recipe.metadata.get("partial_listing"):
        return
    raise ScraperExecutionError(
        f"pagination reached the configured {pagination.max_pages}-page limit before the feed ended"
    )


def _detail_url(job: JobRecord, recipe: ScraperRecipe) -> str | None:
    if recipe.strategy == ScraperStrategy.SMARTRECRUITERS:
        return f"{recipe.request.url.rstrip('/')}/{quote(job.source_id, safe='')}"
    if recipe.strategy == ScraperStrategy.WORKDAY:
        path = urlsplit(job.canonical_url).path
        base = recipe.request.url.removesuffix("/jobs")
        return f"{base}{path}" if path else None
    return None


def _enrich_details(
    jobs: list[JobRecord], recipe: ScraperRecipe, client: httpx.Client | None,
    detail_cache: dict | None = None,
) -> tuple[list[JobRecord], int, list[str]]:
    """Fetch bounded ATS details only when compact list responses omit descriptions."""
    if recipe.strategy not in {ScraperStrategy.SMARTRECRUITERS, ScraperStrategy.WORKDAY}:
        return jobs, 0, []
    configured = recipe.metadata.get("detail_fetch_limit", DEFAULT_DETAIL_FETCH_LIMIT)
    try:
        limit = min(MAX_DETAIL_FETCH_LIMIT, max(0, int(configured)))
    except (TypeError, ValueError):
        limit = DEFAULT_DETAIL_FETCH_LIMIT
    detail_predicates = [
        item for item in (recipe.source_filter.predicates if recipe.source_filter else [])
        if item.phase == "detail"
    ]
    signatures = {}
    cached = set()
    if detail_cache is not None:
        for index, job in enumerate(jobs):
            signature = hashlib.sha256(json.dumps(job.model_dump(mode="json", exclude={"scraped_at"}), sort_keys=True).encode()).hexdigest()
            signatures[job.source_id] = signature
            prior = detail_cache.get(job.source_id)
            if prior and prior.get("signature") == signature:
                try:
                    jobs[index] = JobRecord.model_validate(prior["job"]).model_copy(update={"scraped_at": job.scraped_at})
                    cached.add(index)
                except (ValueError, KeyError):
                    pass
    missing = list(range(len(jobs))) if detail_predicates else [
        index for index, job in enumerate(jobs) if not job.description
    ]
    missing = [index for index in missing if index not in cached]
    selected = missing[:limit]
    warnings: list[str] = []
    if len(missing) > limit:
        warnings.append(
            f"description detail enrichment skipped {len(missing) - limit} jobs "
            f"after the configured {limit}-posting limit"
        )
    fetched = 0
    failed = 0
    enriched = list(jobs)
    for position, index in enumerate(selected):
        report_progress("details", f"Reading role details {position}/{len(selected)}", completed=position, total=len(selected), unit="roles")
        detail_url = _detail_url(enriched[index], recipe)
        if not detail_url:
            failed += 1
            continue
        request = RequestConfig(
            url=detail_url, method="GET", timeout_seconds=10, max_response_bytes=2_000_000,
        )
        try:
            response = bounded_request(request, recipe.allowed_hosts, client)
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("posting detail response was not an object")
            detail_matches = all if not recipe.source_filter or recipe.source_filter.predicate_match == "all" else any
            if detail_predicates and not detail_matches(_predicate_matches(payload, item) for item in detail_predicates):
                enriched[index] = None
                fetched += 1
                continue
            if recipe.strategy == ScraperStrategy.SMARTRECRUITERS:
                enriched[index] = adapters.enrich_smartrecruiters(enriched[index], payload, recipe)
            else:
                enriched[index] = adapters.enrich_workday(enriched[index], payload, recipe)
            fetched += 1
            if not enriched[index].description:
                failed += 1
        except (ScraperNetworkError, ValueError, ValidationError):
            failed += 1
    if selected:
        report_progress("details", f"Read {len(selected)} role details", completed=len(selected), total=len(selected), unit="roles")
    if failed:
        warnings.append(f"description detail enrichment was incomplete for {failed} jobs")
    if detail_cache is not None:
        # Cache attempted unchanged listings, including unavailable details. This
        # lets a bounded batch move on to later jobs rather than retrying the same
        # first postings forever. A changed listing is eligible again.
        for index in selected:
            if enriched[index] is not None:
                identifier = jobs[index].source_id
                detail_cache[identifier] = {"signature": signatures[identifier], "job": enriched[index].model_dump(mode="json")}
    return [job for job in enriched if job is not None], fetched, warnings


def _at_path(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return current


def _predicate_matches(value: Any, predicate) -> bool:
    candidate = _at_path(value, predicate.path)
    expected = {item.casefold() for item in predicate.values}
    if predicate.operator == "html_label_equals_ci":
        text = BeautifulSoup(str(candidate or ""), "html.parser").get_text("\n", strip=True)
        match = re.search(rf"(?im)^\s*{re.escape(predicate.label or '')}\s*:\s*(.+?)\s*$", text)
        candidate = match.group(1) if match else None
    if predicate.operator == "array_object_field_equals_ci":
        if not isinstance(candidate, list):
            return False
        field = predicate.label or ""
        values = [item.get(field) for item in candidate if isinstance(item, dict)]
    else:
        values = candidate if isinstance(candidate, list) else [candidate]
    normalized = [" ".join(str(item).split()).casefold() for item in values if item is not None]
    if predicate.operator in {"equals_ci", "html_label_equals_ci", "array_object_field_equals_ci"}:
        return any(item in expected for item in normalized)
    return any(any(term in item for term in expected) for item in normalized)


def _filter_listing_payload(payload: Any, recipe: ScraperRecipe) -> Any:
    predicates = [
        item for item in (recipe.source_filter.predicates if recipe.source_filter else [])
        if item.phase == "listing"
    ]
    if not predicates:
        return payload
    filtered = copy.deepcopy(payload)
    matches = all if recipe.source_filter.predicate_match == "all" else any
    if isinstance(filtered, list):
        return [item for item in filtered if matches(_predicate_matches(item, rule) for rule in predicates)]
    if not isinstance(filtered, dict):
        return filtered
    items_path = recipe.mapping.get("items") if recipe.strategy == ScraperStrategy.GENERIC_JSON else None
    key = items_path if items_path and "." not in items_path else next(
        (name for name in ("content", "jobPostings", "jobs") if isinstance(filtered.get(name), list)), None
    )
    if key and isinstance(filtered.get(key), list):
        filtered[key] = [item for item in filtered[key] if matches(_predicate_matches(item, rule) for rule in predicates)]
    return filtered


def run_scraper(
    recipe_or_company: ScraperRecipe | dict | Any,
    recipe: ScraperRecipe | dict | None = None,
    *,
    client: httpx.Client | None = None,
    progress=None,
    detail_cache: dict | None = None,
) -> ScrapeResult | dict[str, Any]:
    """Run a recipe.

    The one-argument form is the typed runtime API.  The two-argument form is a
    compatibility facade for the scheduler (`company, recipe`) and returns a
    JSON-serializable dictionary.
    """
    scheduler_call = recipe is not None
    selected = recipe if scheduler_call else recipe_or_company
    if not isinstance(selected, ScraperRecipe):
        selected = ScraperRecipe.model_validate(selected)
    _notify(progress, "fetching", "Connecting to careers source", 15)
    if selected.strategy == ScraperStrategy.PLAYWRIGHT:
        result = _run_playwright(selected)
        return _scheduler_payload(result, selected) if scheduler_call else result
    jobs = []
    pages = 0
    try:
        for response in _paged_payloads(selected, client):
            pages += 1
            payload = _filter_listing_payload(_parse_response(response, selected), selected)
            normalized = _apply_source_filter(_adapt(payload, selected), selected)
            jobs.extend(normalized)
            _notify(progress, "listing", f"Read {pages} pages · {len(jobs)} roles found", min(90, 15 + pages * 10), pages=pages, roles_found=len(jobs))
    except (ScraperNetworkError, ValidationError) as exc:
        raise ScraperExecutionError(str(exc)) from exc
    unique = {job.source_id: job for job in jobs}
    warnings = []
    if len(unique) < len(jobs):
        warnings.append(f"removed {len(jobs) - len(unique)} duplicate source IDs")
    enriched, detail_pages, detail_warnings = _enrich_details(
        list(unique.values()), selected, client, detail_cache,
    )
    pages += detail_pages
    warnings.extend(detail_warnings)
    result = ScrapeResult(
        jobs=enriched, strategy=selected.strategy, pages_fetched=pages,
        warnings=warnings,
        complete=not bool(selected.metadata.get("partial_listing")),
    )
    _notify(progress, "complete", f"Found {len(result.jobs)} jobs", 100)
    return _scheduler_payload(result, selected) if scheduler_call else result


def _scheduler_payload(result: ScrapeResult, recipe: ScraperRecipe) -> dict[str, Any]:
    return {
        "jobs": [job.model_dump(mode="json") for job in result.jobs],
        "recipe": recipe.model_dump(mode="json"),
        "full_run": result.complete,
        "source": result.strategy.value,
        "pages_fetched": result.pages_fetched,
        "warnings": result.warnings,
    }


def _notify(callback, stage: str, message: str, percent: int, **detail) -> None:
    report_progress("saving" if stage == "complete" else stage, message, **detail)
    if callback is None:
        return
    event = {"stage": stage, "message": message, "percent": percent}
    try:
        callback(event)
    except TypeError:
        try:
            callback(stage=stage, message=message, percent=percent)
        except TypeError:
            callback(message)


def _apply_source_filter(jobs: list, recipe: ScraperRecipe) -> list:
    if not recipe.source_filter:
        return jobs
    if not recipe.source_filter.title_contains_any:
        return jobs
    terms = tuple(value.casefold() for value in recipe.source_filter.title_contains_any)
    return [job for job in jobs if any(term in job.title.casefold() for term in terms)]


def _run_playwright(recipe: ScraperRecipe) -> ScrapeResult:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ScraperExecutionError("Playwright fallback is unavailable; install the optional playwright dependency") from exc
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            def confined(route):
                try:
                    _validate_target(route.request.url, recipe.allowed_hosts)
                except ScraperNetworkError:
                    route.abort()
                else:
                    route.continue_()
            page.route("**/*", confined)
            page.goto(recipe.request.url, wait_until="domcontentloaded", timeout=int(recipe.request.timeout_seconds * 1000))
            wait_selector = recipe.metadata.get("wait_selector")
            if isinstance(wait_selector, str) and wait_selector:
                page.wait_for_selector(wait_selector, timeout=int(recipe.request.timeout_seconds * 1000))
            jobs = []
            pages_fetched = 0
            next_selector = recipe.metadata.get("next_selector")
            for _ in range(recipe.pagination.max_pages):
                html = page.content()
                if len(html.encode("utf-8")) > recipe.request.max_response_bytes:
                    raise ScraperExecutionError("rendered page exceeds recipe size limit")
                jobs.extend(_apply_source_filter(adapters.generic_html(html, recipe, source="playwright"), recipe))
                pages_fetched += 1
                if recipe.pagination.kind == "none" or not isinstance(next_selector, str):
                    break
                next_button = page.locator(next_selector).first
                if not next_button.is_visible() or next_button.is_disabled():
                    break
                prior_id = (page.locator(wait_selector).first.get_attribute("data-job-id")
                            if isinstance(wait_selector, str) and wait_selector else None)
                next_button.click()
                if prior_id and isinstance(wait_selector, str):
                    page.wait_for_function(
                        "value => document.querySelector(value.selector)?.getAttribute('data-job-id') !== value.id",
                        arg={"selector": wait_selector, "id": prior_id},
                        timeout=int(recipe.request.timeout_seconds * 1000),
                    )
                else:
                    page.wait_for_timeout(500)
                if isinstance(wait_selector, str) and wait_selector:
                    page.wait_for_selector(wait_selector, timeout=int(recipe.request.timeout_seconds * 1000))
            browser.close()
        unique = {job.source_id: job for job in jobs}
        return ScrapeResult(
            jobs=list(unique.values()), strategy=recipe.strategy, pages_fetched=pages_fetched,
            complete=not bool(recipe.metadata.get("partial_listing")),
        )
    except Exception as exc:
        raise ScraperExecutionError(f"Playwright execution failed: {exc}") from exc


def rich_field_coverage(jobs: list[JobRecord]) -> dict[str, int]:
    return {
        "jobs": len(jobs),
        "description": sum(bool(job.description) for job in jobs),
        "employment_type": sum(bool(job.employment_type) for job in jobs),
        "remote_mode": sum(job.remote_mode.value != "unknown" for job in jobs),
        "salary": sum(job.salary_min is not None or job.salary_max is not None for job in jobs),
        "salary_currency": sum(bool(job.salary_currency) for job in jobs),
        "salary_period": sum(bool(job.salary_period) for job in jobs),
    }


def rich_field_warnings(coverage: dict[str, int]) -> list[str]:
    jobs = coverage["jobs"]
    if not jobs:
        return []
    warnings = []
    for key, label in (
        ("employment_type", "employment types"),
        ("remote_mode", "work modes"),
        ("salary", "salary ranges"),
    ):
        if coverage[key] < jobs:
            warnings.append(
                f"rich-field coverage: {label} available for {coverage[key]}/{jobs} jobs"
            )
    for key, label in (("salary_currency", "currency"), ("salary_period", "pay cadence")):
        if coverage[key] < coverage["salary"]:
            warnings.append(
                f"rich-field coverage: {label} available for {coverage[key]}/{coverage['salary']} salaried jobs"
            )
    if coverage["description"] < jobs:
        warnings.append(
            f"rich-field coverage: descriptions available for {coverage['description']}/{jobs} jobs"
        )
    return warnings


def validate_recipe(recipe: ScraperRecipe | dict, *, client: httpx.Client | None = None, require_jobs: bool = True) -> ValidationReport:
    try:
        parsed = recipe if isinstance(recipe, ScraperRecipe) else ScraperRecipe.model_validate(recipe)
    except ValidationError as exc:
        return ValidationReport(valid=False, errors=[error["msg"] for error in exc.errors()])
    try:
        result = run_scraper(parsed, client=client)
    except (ScraperExecutionError, ValidationError) as exc:
        return ValidationReport(valid=False, errors=[str(exc)])
    errors = []
    if require_jobs and not result.jobs:
        errors.append("recipe returned zero valid jobs")
    coverage = rich_field_coverage(result.jobs)
    warnings = [*result.warnings, *rich_field_warnings(coverage)]
    return ValidationReport(
        valid=not errors, errors=errors, warnings=warnings,
        job_count=len(result.jobs), coverage=coverage,
    )
