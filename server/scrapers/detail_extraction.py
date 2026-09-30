"""Extract descriptions only from identifiable public JobPosting structured data."""
from __future__ import annotations

import json
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .models import JobRecord, ScraperRecipe, ScraperStrategy
from .normalize import plain_text


def supports(recipe: ScraperRecipe) -> bool:
    return recipe.strategy in {
        ScraperStrategy.GENERIC_HTML, ScraperStrategy.GENERIC_JSON,
        ScraperStrategy.JSON_LD, ScraperStrategy.JOBVITE,
    }


def _postings(value):
    if isinstance(value, list):
        for item in value:
            yield from _postings(item)
    elif isinstance(value, dict):
        kinds = value.get("@type", [])
        kinds = kinds if isinstance(kinds, list) else [kinds]
        if any(str(kind).rsplit("/", 1)[-1] == "JobPosting" for kind in kinds):
            yield value
        for key in ("@graph", "mainEntity", "itemListElement", "item"):
            if key in value:
                yield from _postings(value[key])


def _url_key(value: str):
    parts = urlsplit(value)
    return (parts.hostname, parts.path.rstrip("/"), parts.query)


def enrich(job: JobRecord, text: str, recipe: ScraperRecipe) -> JobRecord:
    """Never take navigation, recommendations, or another role as a description."""
    soup = BeautifulSoup(text, "html.parser")
    candidates = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            candidates.extend(_postings(json.loads(script.get_text())))
        except (ValueError, RecursionError):
            continue
    matches = []
    for posting in candidates:
        title = plain_text(posting.get("title")).casefold()
        if title != plain_text(job.title).casefold():
            continue
        url = posting.get("url")
        if url and (not isinstance(url, str) or _url_key(url) != _url_key(job.canonical_url)):
            continue
        description = posting.get("description")
        if isinstance(description, str) and plain_text(description):
            matches.append(plain_text(description))
    # A listing or recommendation page can contain multiple same-title roles.
    # Only a uniquely identified posting is safe to enrich.
    if len(matches) != 1:
        return job
    return job.model_copy(update={"description": matches[0]})
