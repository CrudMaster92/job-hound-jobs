"""Recover incomplete Workday entries from explicitly configured public pages."""
from __future__ import annotations

import json
import time
from urllib.parse import quote

from bs4 import BeautifulSoup
from pydantic import ValidationError

from . import adapters
from .http import ScraperNetworkError, ScraperYieldError, bounded_request
from .models import RequestConfig


def recover(recipe, client, state) -> tuple[int, list[str]]:
    pending = state.get("missing_postings", {})
    template = recipe.metadata.get("workday_missing_posting_url")
    pages, warnings = 0, []
    # A second source cannot silently replace a recipe's ownership predicates.
    if not isinstance(template, str) or "{source_id}" not in template or recipe.source_filter:
        return pages, warnings
    for identifier, entry in pending.items():
        if entry.get("resolved") or time.time() < entry.get("retry_after", 0):
            continue
        url = template.replace("{source_id}", quote(identifier, safe=""))
        try:
            response = bounded_request(RequestConfig(
                url=url, timeout_seconds=30, max_response_bytes=5_000_000,
            ), recipe.allowed_hosts, client)
            pages += 1
            soup = BeautifulSoup(response.text, "html.parser")
            match = None
            for script in soup.select('script[type="application/ld+json"]'):
                try:
                    payload = json.loads(script.string or script.get_text())
                except (ValueError, TypeError):
                    continue
                for raw in adapters._json_ld_nodes(payload):
                    source_id = raw.get("identifier")
                    if isinstance(source_id, dict):
                        source_id = source_id.get("value")
                    employer = raw.get("hiringOrganization")
                    if isinstance(employer, dict):
                        employer = employer.get("name")
                    if str(source_id) == identifier and str(employer).casefold() == recipe.company.casefold() and raw.get("title"):
                        match = {**raw, "url": str(response.url)}
                        break
                if match:
                    break
            if match is None:
                entry.update(status="public_metadata_unavailable", retry_after=time.time() + 300)
                continue
            # A source's explicit missing-value sentinel is not a salary currency.
            salary = match.get('baseSalary')
            if isinstance(salary, dict) and str(salary.get('currency', '')).casefold() in {'unavailable', 'n/a', 'not available'}:
                match['baseSalary'] = {**salary, 'currency': None}
            document = '<script type="application/ld+json">' + json.dumps(match) + '</script>'
            job = adapters.json_ld(document, recipe)[0].model_copy(update={"source": "workday"})
            state["jobs"][identifier] = job.model_dump(mode="json")
            entry.update(resolved=True, status="recovered", url=str(response.url))
        except ScraperYieldError:
            state['paused'] = True
            warnings.append("Missing posting metadata lookup paused; its checkpoint resumes next run.")
            break
        except (ScraperNetworkError, ValidationError, ValueError, IndexError):
            entry.update(status="lookup_failed", retry_after=time.time() + 300)
    return pages, warnings
