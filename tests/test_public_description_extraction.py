import json

import httpx

from server.scrapers.detail_extraction import enrich
from server.scrapers.models import JobRecord, ScraperRecipe
from server.scrapers.runtime import run_scraper
from server.scrapers.normalize import normalize_description, make_job


def recipe():
    return ScraperRecipe.model_validate({
        "company": "Example", "careers_url": "https://example.com/jobs",
        "strategy": "generic_html", "allowed_hosts": ["example.com"],
        "request": {"url": "https://example.com/jobs"},
        "selectors": {"item": ".job", "title": "h2", "link": "a"},
        "metadata": {"detail_fetch_limit": 1},
    })


def job():
    return JobRecord(source_id="one", title="Engineer", company="Example",
                     canonical_url="https://example.com/jobs/one", source_url="https://example.com/jobs/one", source="generic_html")


def html(*postings):
    return '<script type="application/ld+json">' + json.dumps({"@graph": list(postings)}).replace("</", "<\\/") + '</script>'


def posting(**changes):
    return {"@type": "JobPosting", "title": "Engineer",
            "url": "https://example.com/jobs/one", "description": "<p>Build reliable tools.</p>", **changes}


def test_extracts_matching_structured_posting_not_navigation():
    result = enrich(job(), '<nav>Account login</nav>' + html(posting()), recipe())
    assert result.description == "Build reliable tools."


def test_description_preserves_source_blocks_and_inline_text():
    content = '<h3>About the role</h3><p>Build <strong>reliable</strong> tools.</p><p>Work with us.<br>Remote welcome.</p><ul><li>Python</li><li>SQL</li></ul><!-- private --><script>bad()</script>'
    expected = 'About the role\n\nBuild reliable tools.\n\nWork with us.\nRemote welcome.\n\n• Python\n• SQL'
    assert normalize_description(content) == expected
    assert enrich(job(), html(posting(description=content)), recipe()).description == expected
    normalized = make_job(company='Example', source='json_ld', source_id='one', title='Engineer', location='Remote', url='https://example.com/jobs/one', base_url='https://example.com', description=content)
    assert normalized.description == expected


def test_plain_and_entity_encoded_descriptions_retain_paragraphs():
    assert normalize_description('First paragraph.\r\n\r\nSecond paragraph.\n• One\n• Two') == 'First paragraph.\n\nSecond paragraph.\n• One\n• Two'
    assert normalize_description('&lt;p&gt;First &amp;amp; second.&lt;/p&gt;&lt;p&gt;Next.&lt;/p&gt;') == 'First & second.\n\nNext.'


def test_rejects_other_roles_and_ambiguous_same_title():
    for content in [html(posting(title="Designer")), html(posting(url="https://example.com/jobs/two")),
                    html(posting(url=None), posting(url=None)), '<main>Login required</main>']:
        assert not enrich(job(), content, recipe()).description


def test_localized_structured_url_requires_page_identity_and_exact_posting():
    canonical = '<link rel="canonical" href="https://example.com/jobs/one">'
    content = html(posting(url='https://example.com/en/jobs/one', description='<p>First paragraph.</p><p>Second paragraph.</p>'))
    assert enrich(job(), canonical + content, recipe()).description == 'First paragraph.\n\nSecond paragraph.'
    assert not enrich(job(), content, recipe()).description
    for url in ['https://other.example/en/jobs/one', 'https://example.com/en/jobs/two',
                'https://example.com/en/jobs/one?role=two', 'https://example.com/account/jobs/one']:
        assert not enrich(job(), canonical + html(posting(url=url)), recipe()).description
    assert not enrich(job(), canonical + canonical + content, recipe()).description
    assert not enrich(job(), canonical.replace('/jobs/one', '/jobs/two') + content, recipe()).description


def test_localized_same_title_recommendations_remain_ambiguous():
    canonical = '<link rel="canonical" href="https://example.com/jobs/one">'
    content = html(posting(url='https://example.com/en/jobs/one'), posting(url='https://example.com/fr/jobs/one'))
    assert not enrich(job(), canonical + content, recipe()).description


def test_generic_details_use_bounded_runner_cache_and_host_allowlist():
    seen = []
    def handler(request):
        seen.append(str(request.url))
        if request.url.path == "/jobs":
            return httpx.Response(200, text='<div class="job"><h2>Engineer</h2><a href="/jobs/one">View</a></div>')
        return httpx.Response(200, text=html(posting()))
    cache = {}
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = run_scraper(recipe(), client=client, detail_cache=cache)
        second = run_scraper(recipe(), client=client, detail_cache=cache)
    assert first.jobs[0].description == second.jobs[0].description == "Build reliable tools."
    assert seen.count("https://example.com/jobs/one") == 1


def test_labelled_html_detail_requires_matching_canonical_and_title():
    page = ('<link rel="canonical" href="https://example.com/jobs/one">'
            '<h2>Engineer</h2><article><h3>Description &amp; Requirements</h3>'
            '<div class="article__content">Build tools.</div></article>'
            '<aside>Share and login</aside>')
    assert enrich(job(), page, recipe()).description == "Build tools."
    assert not enrich(job(), page.replace('/jobs/one', '/jobs/two'), recipe()).description
    assert not enrich(job(), page.replace('Engineer', 'Designer'), recipe()).description
    assert not enrich(job(), page + '<h2>Engineer</h2>', recipe()).description
