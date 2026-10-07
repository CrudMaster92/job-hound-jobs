import httpx
import pytest
from pydantic import ValidationError

from server.scrapers import ScraperRecipe, run_scraper


def recipe(**updates):
    return ScraperRecipe.model_validate({
        "company": "Public employer", "careers_url": "https://employer.test/jobs",
        "strategy": "generic_json", "allowed_hosts": ["employer.test"],
        "request": {"url": "https://employer.test/api/jobs", "method": "POST"},
        "mapping": {"items": "Results.Jobs", "source_id": "id", "title": "title", "url": "slug"},
        "metadata": {"detail_fetch_limit": 0}, **updates,
    })


def test_nested_mapped_listing_continues_and_stops_at_short_page():
    pages = []
    def respond(request):
        import json
        page = json.loads(request.content)["Page"]
        pages.append(page)
        jobs = [{"id": str(page), "title": "Engineer", "slug": f"/job/{page}"}] if page < 3 else []
        return httpx.Response(200, json={"Results": {"Jobs": jobs}}, request=request)
    result = run_scraper(recipe(pagination={"kind": "page", "parameter": "Page", "page_size": 1, "max_pages": 4}),
                         client=httpx.Client(transport=httpx.MockTransport(respond)))
    assert pages == [1, 2, 3]
    assert [job.source_id for job in result.jobs] == ["1", "2"]
    assert result.complete


def test_posting_slug_prefix_produces_direct_url_and_encodes_path_controls():
    raw = {"Results": {"Jobs": [{"id": "1", "title": "Nurse", "slug": "1-nurse?x=/other#fragment"}]}}
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=raw, request=request)))
    result = run_scraper(recipe(metadata={"posting_url_prefix": "https://employer.test/fr/annonce/", "detail_fetch_limit": 0}), client=client)
    assert result.jobs[0].source_url == "https://employer.test/fr/annonce/1-nurse%3Fx%3D%2Fother%23fragment"


@pytest.mark.parametrize("prefix", ["https://outside.test/jobs", "http://employer.test/jobs", "https://user:pass@employer.test/jobs", "https://employer.test/jobs?x=1", 42])
def test_posting_prefix_cannot_escape_recipe_host_boundary(prefix):
    with pytest.raises(ValidationError, match="posting_url_prefix"):
        recipe(metadata={"posting_url_prefix": prefix})
