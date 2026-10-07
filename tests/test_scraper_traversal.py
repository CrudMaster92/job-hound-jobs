"""All-role traversal must prove exhaustion and survive work slices."""
import json
import time
from datetime import datetime, timezone
from datetime import timedelta

import httpx
import pytest

from server.scrapers.http import ScraperYieldError
from server.scrapers.models import ScraperRecipe
from server.scrapers.runtime import run_scraper
from server.scrapers.traversal import CACHE_KEY
from server.public_jobs.collector import collect
from server.scrapers import workday_inventory
from urllib.parse import parse_qs


def recipe(strategy="workday", **changes):
    value = {"company": "Acme", "careers_url": "https://acme.example/Careers",
             "strategy": strategy, "allowed_hosts": ["acme.example"], "coverage_mode": "all",
             "request": {"url": "https://acme.example/wday/cxs/acme/Careers/jobs", "method": "POST"},
             "pagination": {"kind": "offset", "parameter": "offset", "page_size_parameter": "limit",
                            "page_size": 500, "max_pages": 1},
             "metadata": {"detail_fetch_limit": 0}}
    value.update(changes)
    return ScraperRecipe.model_validate(value)


def posting(index):
    return {"title": f"Engineer {index}", "externalPath": f"/job/Toronto/Engineer_R{index}",
            "bulletFields": [f"R{index}"], "description": "Public role description"}


def test_seeded_parent_evidence_does_not_stop_a_repair_on_a_known_page():
    cache, offsets, paused = {}, [], False
    config = recipe(pagination={'kind': 'offset', 'parameter': 'offset',
        'page_size_parameter': 'limit', 'page_size': 2})
    def handler(request):
        nonlocal paused
        offset = json.loads(request.content)['offset']
        offsets.append(offset)
        if offset == 2 and not paused:
            paused = True
            raise ScraperYieldError('work slice ended')
        return httpx.Response(200, json={'total': 5,
            'jobPostings': [posting(i) for i in range(offset, min(offset + 2, 5))]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = run_scraper(config, client=client, detail_cache=cache)
        assert first.continuation_ready
        # A repair inherits these observations from its parent, but has not
        # fetched them through this query's own pagination yet.
        cache[CACHE_KEY]['queue'][0]['seen'].extend(['req:R2', 'req:R3'])
        second = run_scraper(config, client=client, detail_cache=cache)
    assert second.complete and len(second.jobs) == 5
    assert offsets == [0, 2, 2, 4]


def test_proven_child_scope_skips_redundant_searches_but_keeps_other_scopes():
    cache, calls = {}, []
    config = recipe(metadata={'detail_fetch_limit': 0, 'workday_search_terms': '["first", "unused"]'})
    def handler(request):
        body = json.loads(request.content)
        filters, term, offset = body.get('appliedFacets', {}), body.get('searchText', ''), body['offset']
        calls.append((filters, term, offset))
        if not filters:
            return httpx.Response(200, json={'total': 2000, 'jobPostings': [posting(i) for i in range(500)],
                'facets': [{'facetParameter': 'jobFamilyGroup', 'values': [
                    {'id': 'large', 'count': 2300}, {'id': 'other', 'count': 8000}]}]})
        if filters['jobFamilyGroup'] == ['other']:
            raise ScraperYieldError('next scope waits for another slice')
        assert term != 'unused'
        if term == 'first':
            return httpx.Response(200, json={'total': 1800, 'jobPostings': [
                posting(i) for i in range(500 + offset, min(1000 + offset, 2300))]})
        return httpx.Response(200, json={'total': 2000, 'jobPostings': [posting(i) for i in range(500)],
            'facets': [{'facetParameter': 'timeType', 'values': [{'id': 'full', 'count': 2300}]}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run_scraper(config, client=client, detail_cache=cache)
    assert not result.complete and result.continuation_ready and len(result.jobs) == 2300
    assert not cache[CACHE_KEY].get('whole_source_verified')
    assert any(group.get('verified') for group in cache[CACHE_KEY]['search_groups'].values())
    assert cache[CACHE_KEY]['queue'][0]['filters']['jobFamilyGroup'] == ['other']
    assert not any(term == 'unused' for _, term, _ in calls)


@pytest.mark.parametrize('changed_count', [False, True])
def test_repeated_complete_query_reuses_evidence_only_after_matching_fresh_count(changed_count):
    cache, offsets, second_query = {}, [], False
    config = recipe(pagination={'kind': 'offset', 'parameter': 'offset',
        'page_size_parameter': 'limit', 'page_size': 2})
    def handler(request):
        offset = json.loads(request.content)['offset']
        offsets.append(offset)
        total = 6 if second_query and changed_count else 5
        return httpx.Response(200, json={'total': total, 'jobPostings': [
            posting(i) for i in range(offset, min(offset + 2, total))]})
    def progress(event):
        nonlocal second_query
        state = cache.get(CACHE_KEY, {})
        if event.get('stage') == 'listing' and not state.get('queue') and not second_query:
            second_query = True
            state['jobs']['R4']['scraped_at'] = '2020-01-01T00:00:00Z'
            state['queue'] = [{'filters': {}, 'offset': 0, 'expected': 0, 'seen': []}]
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run_scraper(config, client=client, detail_cache=cache, progress=progress)
    assert result.complete and len(result.jobs) == (6 if changed_count else 5)
    assert offsets == ([0, 2, 4, 0, 2, 4] if changed_count else [0, 2, 4, 0])
    if not changed_count:
        jobs = {job.source_id: job for job in result.jobs}
        assert jobs['R4'].scraped_at == datetime(2020, 1, 1, tzinfo=timezone.utc)


def test_capped_query_is_fetched_again_instead_of_reused_as_complete():
    cache, offsets, repeated = {}, [], False
    def handler(request):
        offset = json.loads(request.content)['offset']
        offsets.append(offset)
        return httpx.Response(200, json={'total': 2000, 'jobPostings': [
            posting(i) for i in range(offset, min(offset + 500, 2000))]})
    def progress(event):
        nonlocal repeated
        state = cache.get(CACHE_KEY, {})
        if event.get('stage') == 'listing' and not state.get('queue') and not repeated:
            repeated = True
            state['queue'] = [{'filters': {}, 'offset': 0, 'expected': 0, 'seen': []}]
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run_scraper(recipe(), client=client, detail_cache=cache, progress=progress)
    assert not result.complete and len(result.jobs) == 2000
    assert offsets == [0, 500, 1000, 1500, 0, 500, 1000, 1500]


def inventory_recipe():
    return recipe(company='Accenture', careers_url='https://accenture.wd103.myworkdayjobs.com/AccentureCareers',
        allowed_hosts=['accenture.wd103.myworkdayjobs.com', 'www.accenture.com'],
        request={'url': 'https://accenture.wd103.myworkdayjobs.com/wday/cxs/accenture/AccentureCareers/jobs', 'method': 'POST'},
        pagination={'kind': 'offset', 'parameter': 'offset', 'page_size_parameter': 'limit', 'page_size': 20},
        metadata={'detail_fetch_limit': 0, 'workday_accenture_inventory': True})


def inventory_payload(ids, count=None, extra=()):
    return {'data': [{'requisitionId': identifier, 'title': 'Inventory title is not authoritative'} for identifier in ids],
        'aggregations': [{'fieldName': 'employeeType.keyword', 'items': [{'term': 'Full-time', 'count': count if count is not None else len(ids)}]}, *extra]}


def test_public_inventory_only_queues_exact_confirmations_without_creating_jobs():
    state = {'jobs': {}, 'identities': ['req:R1'], 'expected': 2, 'queue': []}
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=inventory_payload(['R1', 'R2'])))) as client:
        pages, pending, warnings = workday_inventory.prepare(inventory_recipe(), client, state, lambda *args: None)
    assert pages == 1 and not pending and not warnings
    assert state['jobs'] == {} and state['identities'] == ['req:R1']
    assert state['queue'][0]['search_text'] == '"R2"'
    assert state['inventory']['requested_ids'] == ['R2']


def test_inventory_requisition_cannot_inject_search_syntax():
    state = {'jobs': {}, 'identities': [], 'expected': 1, 'queue': []}
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=inventory_payload(['R1" OR anything'])))) as client:
        _, pending, warnings = workday_inventory.prepare(inventory_recipe(), client, state, lambda *args: None)
    assert pending and warnings and not state['identities'] and not state['queue']
    assert state['inventory']['ids'] == []


def test_inventory_metadata_requires_prior_workday_confirmation_and_exact_public_url():
    state = {'jobs': {}, 'identities': ['req:R2'], 'expected': 2, 'queue': [],
        'missing_postings': {'R2': {'resolved': False}, 'R3': {'resolved': False}}}
    payload = inventory_payload(['R2', 'R3'])
    for item in payload['data']:
        item.update(title='Recovered engineer', location=['Mumbai'], employeeType='FullTime', remoteType='Remote', jobDescriptionClean='Public description',
            qualificationClean='Public qualification', jobDetailUrl=f"https://www.accenture.com/{{0}}/careers/jobdetails?id={item['requisitionId']}_en")
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))) as client:
        _, pending, _ = workday_inventory.prepare(inventory_recipe(), client, state, lambda *args: None)
    assert not pending and set(state['jobs']) == {'R2'}
    assert state['identities'] == ['req:R2']  # Metadata creates no additional coverage evidence.
    assert state['missing_postings']['R2']['resolved']
    assert not state['missing_postings']['R3']['resolved']
    assert state['jobs']['R2']['source_url'] == 'https://www.accenture.com/in-en/careers/jobdetails?id=R2_en'
    assert 'Public qualification' in state['jobs']['R2']['description']
    assert state['jobs']['R2']['employment_type'] == 'Full-time' and state['jobs']['R2']['remote_mode'] == 'remote'


def test_inventory_metadata_rejects_foreign_hosts_and_mismatched_requisitions():
    state = {'jobs': {}, 'identities': ['req:R2'], 'expected': 2, 'queue': [],
        'missing_postings': {'R2': {'resolved': False}}}
    for url in ('https://other.example/in-en/careers/jobdetails?id=R2_en',
                'https://www.accenture.com/{0}/careers/jobdetails?id=R3_en'):
        workday_inventory.recover_metadata(inventory_recipe(), state, [
            {'requisitionId': 'R2', 'title': 'Wrong metadata', 'jobDetailUrl': url}])
    assert not state['jobs'] and not state['missing_postings']['R2']['resolved']


def test_regional_metadata_discovers_advertised_profile_and_resumes_its_lookup():
    state = {'jobs': {}, 'identities': ['req:R2'], 'expected': 1, 'queue': [],
        'missing_postings': {'R2': {'resolved': False}}}
    calls, api_calls = [], 0
    def handler(request):
        nonlocal api_calls
        calls.append(str(request.url))
        if request.method == 'GET':
            site = request.url.path.split('/')[1]
            return httpx.Response(200, text=(
                '<a href="https://www.accenture.com/br-pt/careers/jobsearch">Brazil</a>'
                f'<div class="rad-job-search__filters-and-cards" data-countrycode="{ "India" if site == "in-en" else "Brasil" }" '
                f'data-language-code="{ "en" if site == "in-en" else "pt-br" }" data-countryselector="{site}"></div>'))
        body = parse_qs(request.content.decode())
        if body['countrySite'] == ['in-en']:
            return httpx.Response(200, json={'data': []})
        api_calls += 1
        if api_calls == 1:
            raise ScraperYieldError('slice ended')
        assert json.loads(body['jobFilters'][0])[0]['items'] == ['R2']
        return httpx.Response(200, json={'data': [{'requisitionId': 'R2', 'title': 'Engenheiro',
            'jobDetailUrl': 'https://www.accenture.com/{0}/careers/jobdetails?id=R2_pt-br'}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        _, pending, _ = workday_inventory.recover_regional_metadata(inventory_recipe(), client, state, lambda *args: None)
        assert pending and state['paused'] and state['metadata_inventory']['queue'][0]['config']['language'] == 'pt-br'
        state['paused'] = False
        _, pending, _ = workday_inventory.recover_regional_metadata(inventory_recipe(), client, state, lambda *args: None)
    assert not pending and state['missing_postings']['R2']['resolved']
    assert len([url for url in calls if '/br-pt/careers/jobsearch' in url]) == 1
    assert state['jobs']['R2']['source_url'] == 'https://www.accenture.com/br-pt/careers/jobdetails?id=R2_pt-br'
    assert state['identities'] == ['req:R2']


def test_regional_metadata_failure_retains_cursor_without_automatic_retry():
    state = {'jobs': {}, 'identities': ['req:R2'], 'expected': 1, 'queue': [],
        'missing_postings': {'R2': {'resolved': False}}, 'metadata_inventory': {
            'discovered': True, 'queue': [{'site': 'in-en', 'offset': 0, 'ids': ['R2'],
                'config': {'country': 'India', 'language': 'en', 'site': 'in-en'}}]}}
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503))) as client:
        _, pending, warnings = workday_inventory.recover_regional_metadata(inventory_recipe(), client, state, lambda *args: None)
    assert pending and warnings and not state.get('paused')
    assert state['metadata_inventory']['queue'][0]['offset'] == 0
    assert not state['jobs'] and not state['missing_postings']['R2']['resolved']


def test_inventory_does_not_broaden_a_configured_workday_search_scope():
    scoped = inventory_recipe()
    scoped.request.json_body = {'searchText': 'Java'}
    assert not workday_inventory.supported(scoped)


def test_inventory_rejects_another_company_or_missing_allowed_host():
    state = {'jobs': {}, 'identities': [], 'expected': 1, 'queue': []}
    listing = inventory_recipe().model_copy(update={'company': 'Other'})
    with httpx.Client(transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(AssertionError('must not request')))) as client:
        _, pending, warnings = workday_inventory.prepare(listing, client, state, lambda *args: None)
    assert pending and warnings and 'inventory' not in state


def test_inventory_partitions_a_capped_query_and_preserves_overlapping_ids():
    state = {'jobs': {}, 'identities': [], 'expected': 11001, 'queue': []}
    calls = []
    def handler(request):
        body = parse_qs(request.content.decode())
        filters = json.loads(body['jobFilters'][0])
        calls.append(filters)
        if not filters:
            return httpx.Response(200, json=inventory_payload(['R1'], 11001, [
                {'fieldName': 'yearsOfExperience.keyword', 'items': [{'term': 'Junior', 'count': 6000}, {'term': 'Senior', 'count': 5001}]}]))
        return httpx.Response(200, json=inventory_payload(['R1', 'R2'] if filters[0]['items'] == ['Junior'] else ['R2', 'R3']))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        pages, pending, warnings = workday_inventory.prepare(inventory_recipe(), client, state, lambda *args: None)
    assert pages == 3 and not pending and not warnings
    assert len(calls) == 3 and state['inventory']['ids'] == ['R1', 'R2', 'R3']
    assert not state['identities'] and not state['jobs']
    assert not state.get('complete')  # Summed inventory buckets never establish Workday coverage.


def test_inventory_budget_resume_keeps_listing_and_requires_workday_confirmation():
    cache, listings, inventory_requests, confirmations = {}, 0, 0, 0
    def handler(request):
        nonlocal listings, inventory_requests, confirmations
        if request.url.host == 'www.accenture.com':
            inventory_requests += 1
            if inventory_requests == 1:
                raise ScraperYieldError('slice ended')
            return httpx.Response(200, json=inventory_payload(['R1', 'R2']))
        body = json.loads(request.content)
        if body.get('searchText') == '"R2"':
            confirmations += 1
            return httpx.Response(200, json={'total': 1, 'jobPostings': [posting(2)]})
        listings += 1
        return httpx.Response(200, json={'total': 2, 'jobPostings': [posting(1)]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = run_scraper(inventory_recipe(), client=client, detail_cache=cache)
        assert not first.complete and first.continuation_ready and len(first.jobs) == 1
        second = run_scraper(inventory_recipe(), client=client, detail_cache=cache)
    assert listings == 1 and inventory_requests == 2 and confirmations == 1
    assert second.complete and {job.source_id for job in second.jobs} == {'R1', 'R2'}
    assert all(job.title.startswith('Engineer') for job in second.jobs)


def test_inventory_ids_absent_from_workday_do_not_fill_coverage():
    cache = {}
    def handler(request):
        if request.url.host == 'www.accenture.com':
            return httpx.Response(200, json=inventory_payload(['R1', 'R2']))
        body = json.loads(request.content)
        return httpx.Response(200, json={'total': 0, 'jobPostings': []} if body.get('searchText') else
            {'total': 2, 'jobPostings': [posting(1)]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run_scraper(inventory_recipe(), client=client, detail_cache=cache)
    assert not result.complete and not result.continuation_ready
    assert [job.source_id for job in result.jobs] == ['R1']
    assert cache[CACHE_KEY]['identities'] == ['req:R1']


def test_inventory_failure_preserves_roles_without_busy_retry():
    cache, listings, inventory_requests = {}, 0, 0
    def handler(request):
        nonlocal listings, inventory_requests
        if request.url.host == 'www.accenture.com':
            inventory_requests += 1
            return httpx.Response(503) if inventory_requests == 1 else httpx.Response(200, json=inventory_payload(['R1', 'R2']))
        if json.loads(request.content).get('searchText') == '"R2"':
            return httpx.Response(200, json={'total': 1, 'jobPostings': [posting(2)]})
        listings += 1
        return httpx.Response(200, json={'total': 2, 'jobPostings': [posting(1)]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run_scraper(inventory_recipe(), client=client, detail_cache=cache)
        assert not result.complete and not result.continuation_ready and len(result.jobs) == 1
        assert cache[CACHE_KEY]['inventory']['queue']
        assert any('inventory failed' in warning for warning in result.warnings)
        second = run_scraper(inventory_recipe(), client=client, detail_cache=cache)
    assert second.complete and listings == 1 and inventory_requests == 2


def test_complete_workday_listing_does_not_depend_on_inventory_availability():
    def handler(request):
        assert request.url.host != 'www.accenture.com'
        return httpx.Response(200, json={'total': 1, 'jobPostings': [posting(1)]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run_scraper(inventory_recipe(), client=client)
    assert result.complete and len(result.jobs) == 1


def test_inventory_repairs_capped_facet_union_before_expensive_keyword_queries():
    listing = inventory_recipe()
    listing.metadata['workday_search_terms'] = '["Missing"]'
    calls, cache = [], {}
    def handler(request):
        if request.url.host == 'www.accenture.com':
            return httpx.Response(200, json=inventory_payload([f'R{i}' for i in range(2002)]))
        body = json.loads(request.content)
        search = body.get('searchText', '')
        calls.append(search)
        selected = body.get('appliedFacets', {}).get('jobFamilyGroup', [])
        if search:
            values = [int(identifier[1:]) for identifier in __import__('re').findall(r'"(R\d+)"', search)]
        elif selected == ['small']:
            values = [2001]
        elif selected == ['large']:
            values = list(range(2001))
        else:
            values = list(range(2002))
        offset = body['offset']
        groups = [{'facetParameter': 'timeType', 'values': [{'id': 'full', 'count': len(values)}]}]
        if not search:
            groups.append({'facetParameter': 'jobFamilyGroup', 'values': [
                {'id': 'large', 'count': 2001}, {'id': 'small', 'count': 1}]})
        return httpx.Response(200, json={'total': min(len(values), 2000), 'facets': groups,
            'jobPostings': [posting(i) for i in values[offset:offset + 20]]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run_scraper(listing, client=client, detail_cache=cache)
    assert result.complete and len(result.jobs) == 2002
    assert 'Missing' not in calls
    assert CACHE_KEY not in cache  # Complete described snapshots release their checkpoint.


def test_missing_workday_posting_is_recovered_with_exact_id_and_employer():
    listing = recipe(pagination={"kind": "none"}, metadata={"detail_fetch_limit": 0,
        "workday_missing_posting_url": "https://acme.example/careers/job?id={source_id}"})
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"total": 2, "jobPostings": [posting(1), {"bulletFields": ["R2", "Toronto"]}]})
        raw = {"@type": "JobPosting", "title": "Recovered engineer", "identifier": {"value": "R2"},
               "hiringOrganization": {"name": "Acme"}, "description": "Recovered public description",
               "baseSalary": {"currency": "unavailable", "value": {"value": "unavailable", "unitText": "unavailable"}}}
        return httpx.Response(200, text='<script type="application/ld+json">'+json.dumps(raw)+'</script>')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run_scraper(listing, client=client)
    assert result.complete and len(result.jobs) == 2
    job = next(job for job in result.jobs if job.source_id == "R2")
    assert job.title == "Recovered engineer" and job.source == "workday"
    assert job.canonical_url == "https://acme.example/careers/job?id=R2"
    assert job.salary_currency is None
    assert job.salary_min is None and job.salary_max is None


def test_bare_entry_is_resolved_when_a_later_listing_supplies_its_metadata():
    def handler(request):
        body = json.loads(request.content)
        selected = body.get('appliedFacets', {}).get('jobFamilyGroup', [])
        values = list(range(1000)) if selected == ['small'] else list(range(1000, 2500)) if selected else list(range(2500))
        offset = body['offset']
        rows = [posting(i) for i in values[offset:offset + 500]] if selected else [posting(1), {'bulletFields': ['R2']}]
        return httpx.Response(200, json={'total': min(len(values), 2000), 'jobPostings': rows,
            'facets': [{'facetParameter': 'timeType', 'values': [{'id': 'full', 'count': len(values)}]},
                {'facetParameter': 'jobFamilyGroup', 'values': [{'id': 'small', 'count': 1000}, {'id': 'large', 'count': 1500}]}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run_scraper(recipe(), client=client)
    assert result.complete and len(result.jobs) == 2500
    assert not any('metadata is still missing' in warning for warning in result.warnings)


def test_missing_metadata_is_retained_without_fabricating_a_role():
    cache = {}
    def handler(request):
        return httpx.Response(200, json={"total": 2, "jobPostings": [posting(1), {"bulletFields": ["R2"]}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run_scraper(recipe(pagination={"kind": "none"}), client=client, detail_cache=cache)
    assert not result.complete and len(result.jobs) == 1
    assert cache[CACHE_KEY]['missing_postings']['R2']['raw']['bulletFields'] == ['R2']
    assert any('metadata is still missing for 1' in warning for warning in result.warnings)


def test_unavailable_metadata_does_not_freeze_future_listing_discovery():
    cache, listings = {}, 0
    def handler(request):
        nonlocal listings
        listings += 1
        rows = [posting(1),{'bulletFields':['R2']}] if listings == 1 else [posting(index) for index in range(1,4)]
        return httpx.Response(200,json={'total':len(rows),'jobPostings':rows})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = run_scraper(recipe(pagination={'kind':'none'}),client=client,detail_cache=cache)
        assert not first.complete and not first.continuation_ready
        assert cache[CACHE_KEY]['refresh_listing_next_run']
        second = run_scraper(recipe(pagination={'kind':'none'}),client=client,detail_cache=cache)
    assert listings == 2 and second.complete and len(second.jobs) == 3


def test_missing_metadata_lookup_resumes_after_a_work_slice():
    cache, listings, details = {}, 0, 0
    listing = recipe(pagination={"kind": "none"}, metadata={"detail_fetch_limit": 0,
        "workday_missing_posting_url": "https://acme.example/careers/job?id={source_id}"})
    def handler(request):
        nonlocal listings, details
        if request.method == 'POST':
            listings += 1
            return httpx.Response(200, json={"total": 1, "jobPostings": [{"bulletFields": ["R2"]}]})
        details += 1
        if details == 1:
            raise ScraperYieldError('slice ended')
        raw = {"@type": "JobPosting", "title": "Recovered", "identifier": "R2", "hiringOrganization": "Acme", "description": "Full description"}
        return httpx.Response(200, text='<script type="application/ld+json">'+json.dumps(raw)+'</script>')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = run_scraper(listing, client=client, detail_cache=cache)
        second = run_scraper(listing, client=client, detail_cache=cache)
    assert not first.complete and not second.complete and len(second.jobs) == 1
    # Finishing metadata from a prior snapshot cannot prove a fresh absence.
    assert listings == 1 and details == 2


def test_missing_metadata_rejects_another_employer_or_requisition():
    listing = recipe(pagination={"kind": "none"}, metadata={"detail_fetch_limit": 0,
        "workday_missing_posting_url": "https://acme.example/careers/job?id={source_id}"})
    def handler(request):
        if request.method == 'POST':
            return httpx.Response(200, json={"total": 1, "jobPostings": [{"bulletFields": ["R2"]}]})
        rows = [{"@type": "JobPosting", "title": "Wrong employer", "identifier": "R2", "hiringOrganization": "Other"},
                {"@type": "JobPosting", "title": "Wrong ID", "identifier": "R3", "hiringOrganization": "Acme"}]
        return httpx.Response(200, text='<script type="application/ld+json">'+json.dumps(rows)+'</script>')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run_scraper(listing, client=client)
    assert not result.complete and result.jobs == []


def test_requisition_search_partitions_recover_an_indivisible_window():
    calls = []
    listing = recipe(metadata={"detail_fetch_limit": 0, "workday_search_prefixes": '["R"]'})
    def handler(request):
        body = json.loads(request.content)
        prefix, offset = body.get('searchText', ''), body['offset']
        calls.append((prefix, offset))
        values = range(2500) if not prefix or prefix == 'R' else [i for i in range(2500) if str(i).startswith(prefix[1:])]
        values = list(values)
        count = len(values)
        return httpx.Response(200, json={'total': min(count, 2000),
            'facets': [{'facetParameter': 'timeType', 'values': [{'id': 'full-time', 'count': count}]}] if count else [],
            'jobPostings': [posting(i) for i in values[offset:offset+500]]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run_scraper(listing, client=client)
    assert result.complete and len(result.jobs) == 2500
    assert ('R1', 0) in calls and ('R2', 0) in calls
    assert calls[-1] == ('', 0)


def test_missing_requisition_prefix_cannot_prove_full_coverage():
    listing = recipe(metadata={"detail_fetch_limit": 0, "workday_search_prefixes": '["R1"]'})
    def handler(request):
        body = json.loads(request.content)
        values = list(range(2500)) if not body.get('searchText') else [i for i in range(2500) if str(i).startswith('1')]
        count, offset = len(values), body['offset']
        return httpx.Response(200, json={'total': min(count, 2000),
            'facets': [{'facetParameter': 'timeType', 'values': [{'id': 'full-time', 'count': count}]}],
            'jobPostings': [posting(i) for i in values[offset:offset+500]]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run_scraper(listing, client=client)
    assert not result.complete and len(result.jobs) < 2500
    assert any('Requisition search partitions could not establish' in warning for warning in result.warnings)


def test_old_global_records_cannot_hide_an_incomplete_search_partition():
    cache, paused = {}, False
    listing = recipe(metadata={"detail_fetch_limit": 0, "workday_search_prefixes": '["R1"]'})
    def handler(request):
        nonlocal paused
        body = json.loads(request.content)
        if body.get('searchText') and not paused:
            paused = True
            raise ScraperYieldError('slice ended')
        values = list(range(2500)) if not body.get('searchText') else [i for i in range(2500) if str(i).startswith('1')]
        count, offset = len(values), body['offset']
        return httpx.Response(200, json={'total': min(count, 2000),
            'facets': [{'facetParameter': 'timeType', 'values': [{'id': 'full-time', 'count': count}]}],
            'jobPostings': [posting(i) for i in values[offset:offset+500]]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        run_scraper(listing, client=client, detail_cache=cache)
        # Old observations must not fill a hole in a current leaf's search proof.
        cache[CACHE_KEY]['identities'].extend(posting(i)['externalPath'] for i in range(3000,5000))
        result = run_scraper(listing, client=client, detail_cache=cache)
    assert len(cache[CACHE_KEY]['identities']) >= 2500
    assert not result.complete


def test_stub_and_later_workday_path_are_one_observed_posting():
    cache = {}
    listing = recipe(pagination={"kind": "offset", "parameter": "offset", "page_size_parameter": "limit", "page_size": 1})
    def handler(request):
        offset = json.loads(request.content)['offset']
        row = {'bulletFields': ['R1']} if offset == 0 else posting(1)
        return httpx.Response(200, json={'total': 2, 'jobPostings': [row]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run_scraper(listing, client=client, detail_cache=cache)
    assert len(result.jobs) == 1 and not result.complete
    assert len(cache[CACHE_KEY]['identities']) == 1
    assert cache[CACHE_KEY]['missing_postings']['R1']['resolved']


def test_changed_workday_title_path_cannot_inflate_coverage():
    cache = {}
    listing = recipe(pagination={'kind':'offset','parameter':'offset','page_size':1,'page_size_parameter':'limit'})
    def handler(request):
        offset = json.loads(request.content)['offset']
        row = {**posting(1), 'externalPath':f'/job/Toronto/Changed-title-{offset}_R1'}
        return httpx.Response(200,json={'total':2,'jobPostings':[row]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run_scraper(listing,client=client,detail_cache=cache)
    assert not result.complete and len(result.jobs) == 1
    assert cache[CACHE_KEY]['identities'] == ['req:R1']


def test_legacy_workday_paths_migrate_without_losing_jobs_or_resumption():
    from server.scrapers.traversal import upgrade_workday_identities
    state = {'jobs': {'R1':{'source_url':'https://acme.example/Careers/job/Toronto/Engineer_R1'}},
             'identities':['/job/Toronto/Engineer_R1','stub:R1','stub:R2','/job/unmapped-old-title'],
             'queue':[{'offset':500,'seen':['/job/Toronto/Engineer_R1']}],
             'search_groups':{'root':{'seen':['/job/Toronto/Engineer_R1','stub:R2']}}}
    upgrade_workday_identities(state)
    assert state['identities'] == ['req:R1','req:R2']
    assert state['queue'] == [{'offset':500,'seen':['req:R1']}]
    assert state['search_groups']['root']['seen'] == ['req:R1','req:R2']
    assert 'R1' in state['jobs']


def test_large_workday_facet_tree_and_overlap_are_complete():
    def handler(request):
        body = json.loads(request.content)
        filters, offset = body.get("appliedFacets", {}), body["offset"]
        if not filters:
            data = {"total": 2000, "jobPostings": [posting(i) for i in range(500)],
                    "facets": [{"facetParameter": "jobFamilyGroup", "values": [
                        {"id": "engineering", "count": 1500}, {"id": "data", "count": 1500}]}]}
        else:
            start = 0 if filters["jobFamilyGroup"] == ["engineering"] else 1000
            # Deliberate overlap means only 2500 unique roles from 3000 advertised.
            data = {"total": 1500, "jobPostings": [posting(i) for i in range(start+offset, start+min(offset+500, 1500))]}
        return httpx.Response(200, json=data)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run_scraper(recipe(), client=client)
    assert len(result.jobs) == 2500
    assert not result.complete
    assert any("2500/3000" in warning for warning in result.warnings)


@pytest.mark.parametrize('previous_root_verification', [False, True])
def test_large_workday_exhaustive_partitions(previous_root_verification):
    cache, root_proof = {}, []
    def handler(request):
        body = json.loads(request.content)
        filters, offset = body.get("appliedFacets", {}), body["offset"]
        if not filters:
            return httpx.Response(200, json={"total": 2000, "jobPostings": [posting(i) for i in range(500)],
                "facets": [{"facetParameter": "jobFamilyGroup", "values": [
                    {"id": "a", "count": 1500}, {"id": "b", "count": 1500}]}]})
        start = 0 if filters["jobFamilyGroup"] == ["a"] else 1500
        return httpx.Response(200, json={"total": 1500, "jobPostings": [
            posting(i) for i in range(start+offset, start+min(offset+500, 1500))]})
    def progress(event):
        root_proof.append(bool(cache.get(CACHE_KEY, {}).get('whole_source_verified')))
        if previous_root_verification:
            for group in cache.get(CACHE_KEY, {}).get('facet_groups', {}).values():
                if not group['filters'] and len(group['seen']) >= group['expected']:
                    # A persisted parent verification still needs the current
                    # root check before it can finish redundant child work.
                    group['verified'] = True
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run_scraper(recipe(), client=client, detail_cache=cache, progress=progress)
    assert result.complete and len(result.jobs) == 3000
    assert any(root_proof)
    assert result.pages_fetched == 8  # scoped union plus a fresh root count


def test_interrupted_listing_resumes_without_losing_jobs_or_details():
    cache, offsets = {}, []
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ScraperYieldError("slice ended")
        offset = json.loads(request.content)["offset"]
        offsets.append(offset)
        return httpx.Response(200, json={"total": 1200, "jobPostings": [
            posting(i) for i in range(offset, min(offset+500, 1200))]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = run_scraper(recipe(), client=client, detail_cache=cache)
        assert not first.complete and len(first.jobs) == 500
        cache["R1000"] = {"job": {}, "attempted_at": time.time()}
        second = run_scraper(recipe(), client=client, detail_cache=cache)
    assert second.complete and len(second.jobs) == 1200
    assert offsets == [0, 500, 1000]


def test_all_details_continue_after_slice_instead_of_becoming_failures():
    cache, fetched = {}, []
    listing = recipe(metadata={"detail_fetch_limit": 1}, pagination={"kind": "none"})
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise ScraperYieldError("slice ended")
        if request.method == "POST":
            values = [posting(i) for i in range(3)]
            for value in values:
                value.pop("description")
            return httpx.Response(200, json={"total": 3, "jobPostings": values})
        fetched.append(str(request.url))
        return httpx.Response(200, json={"jobPostingInfo": {"jobDescription": "Complete description"}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = run_scraper(listing, client=client, detail_cache=cache)
        assert sum(bool(job.description) for job in first.jobs) == 1
        assert len(cache) == 2  # checkpoint plus one success, no spurious failed attempt
        second = run_scraper(listing, client=client, detail_cache=cache)
    assert all(job.description for job in second.jobs)
    assert len(fetched) == 3
    assert CACHE_KEY not in cache
    assert not second.complete  # a cached snapshot cannot prove a fresh absence


def test_collector_continues_all_mode_within_build_budget():
    attempts = []
    from server.scrapers.models import ScrapeResult
    monitor = {"id": "acme", "company_id": "acme", "company_name": "Acme", "revision": 1,
               "collection_ids": []}
    def runner(value, *, client, detail_cache):
        attempts.append(value.metadata.get("detail_fetch_limit"))
        if len(attempts) == 1:
            detail_cache[CACHE_KEY] = {"queue": [1]}
        else:
            detail_cache.pop(CACHE_KEY)
        return ScrapeResult(jobs=[], strategy=value.strategy, complete=len(attempts) > 1,
                           continuation_ready=len(attempts) == 1)
    results = collect([(monitor, recipe(metadata={}))], now=datetime.now(timezone.utc), runner=runner)
    assert attempts == [None, None]  # collector does not reinstate its 20-role quota
    assert len(results) == 1 and results[0]["source"]["complete"]


def test_collector_does_not_restart_a_terminal_metadata_gap_within_one_build():
    from server.scrapers.models import ScrapeResult
    attempts = []
    monitor = {'id':'acme','company_id':'acme','company_name':'Acme','revision':1,'collection_ids':[]}
    def runner(value, *, client, detail_cache):
        attempts.append(1)
        assert len(attempts) == 1
        detail_cache[CACHE_KEY] = {'queue':[],'refresh_listing_next_run':True}
        return ScrapeResult(jobs=[],strategy=value.strategy,complete=False,continuation_ready=False)
    results = collect([(monitor,recipe())],now=datetime.now(timezone.utc),runner=runner)
    assert len(attempts) == 1 and results[0]['source']['status'] == 'partial'


def test_nested_shared_json_counts_raw_pages_and_filters_ownership():
    listing = recipe('generic_json',pagination={'kind':'offset','parameter':'offset','page_size':2},
        mapping={'items':'data.roles','source_id':'id'},
        source_filter={'predicates':[{'path':'employer','values':['Acme']}]})
    calls = []
    def handler(request):
        offset = json.loads(request.content)['offset']
        calls.append(offset)
        rows = [{'id':str(index),'title':'Engineer','url':f'https://acme.example/job/{index}',
                 'employer':'Acme' if index%2 else 'Other'} for index in range(offset,min(offset+2,4))]
        return httpx.Response(200,json={'total':4,'data':{'roles':rows}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run_scraper(listing,client=client)
    assert result.complete and {job.source_id for job in result.jobs} == {'1','3'}
    assert calls == [0,2]


def test_invalid_or_missing_json_collection_is_not_an_empty_complete_source():
    for payload in ({'error':'temporarily unavailable'},None):
        def handler(request):
            return httpx.Response(200,json=payload) if payload is not None else httpx.Response(200,text='not-json')
        cache = {}
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = run_scraper(recipe(),client=client,detail_cache=cache)
        assert not result.complete and not result.jobs
        assert cache[CACHE_KEY]['queue'][0]['offset'] == 0
        assert any('incomplete' in warning for warning in result.warnings)


def test_explicit_empty_nested_json_collection_proves_empty_source():
    listing = recipe('generic_json',mapping={'items':'data.roles'},pagination={'kind':'none'})
    with httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'data':{'roles':[]}}))) as client:
        result = run_scraper(listing,client=client)
    assert result.complete and not result.jobs


def test_workday_public_board_url_and_detail_path_are_distinct():
    listing = recipe(pagination={'kind':'none'},metadata={},careers_url='https://acme.example/en-US/Careers')
    calls = []
    def handler(request):
        calls.append(request.url.path)
        if request.method == 'POST':
            row = posting(1)
            row.pop('description')
            return httpx.Response(200,json={'total':1,'jobPostings':[row]})
        assert request.url.path == '/wday/cxs/acme/Careers/job/Toronto/Engineer_R1'
        return httpx.Response(200,json={'jobPostingInfo':{'jobDescription':'Complete public description'}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run_scraper(listing,client=client)
    assert result.complete and result.jobs[0].canonical_url == 'https://acme.example/en-US/Careers/job/Toronto/Engineer_R1'
    assert result.jobs[0].description == 'Complete public description'


def test_saturated_workday_without_count_witness_is_not_complete():
    def handler(request):
        offset = json.loads(request.content)["offset"]
        return httpx.Response(200, json={"total": 2000, "jobPostings": [
            posting(i) for i in range(offset, offset+500)]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run_scraper(recipe(), client=client)
    assert len(result.jobs) == 2000 and not result.complete


def test_repeated_pages_without_total_cannot_claim_source_exhaustion():
    def handler(request):
        return httpx.Response(200, json={"jobPostings": [posting(i) for i in range(500)]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run_scraper(recipe(), client=client)
    assert result.pages_fetched == 2 and not result.complete


def test_network_failure_preserves_checkpoint_for_retry():
    cache = {}
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise httpx.ReadTimeout("temporary source failure", request=request)
        offset = json.loads(request.content)["offset"]
        return httpx.Response(200, json={"total": 700, "jobPostings": [
            posting(i) for i in range(offset, min(offset+500, 700))]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = run_scraper(recipe(), client=client, detail_cache=cache)
        assert not first.complete and len(first.jobs) == 500
        assert cache[CACHE_KEY]['queue'][0]['offset'] == 500
        second = run_scraper(recipe(), client=client, detail_cache=cache)
    assert second.complete and len(second.jobs) == 700


def test_cached_listing_does_not_refresh_role_freshness():
    from server.public_jobs.lifecycle import apply_results, empty_state
    from server.public_jobs.schema import timestamp
    from server.scrapers.models import JobRecord
    from server.public_jobs.collector import normalize_job
    observed = datetime(2026, 10, 1, tzinfo=timezone.utc)
    now = observed + timedelta(days=2)
    monitor = {"id": "acme", "company_id": "acme", "company_name": "Acme", "revision": 1,
               "collection_ids": []}
    record = JobRecord(company="Acme", source_id="1", title="Engineer", source="workday",
                       canonical_url="https://acme.example/job/1", source_url="https://acme.example/job/1")
    job = normalize_job(record, monitor, now)
    result = {"source": {**monitor, "status": "partial", "complete": False}, "jobs": [job],
              "observed_at": {job["id"]: timestamp(observed)}}
    state = apply_results(empty_state(), [result], generation="resume", now=now)
    assert state["jobs"][job["id"]]["job"]["last_seen_at"] == timestamp(observed)
    assert state["jobs"][job["id"]]["observations"]["acme"]["last_seen_at"] == timestamp(observed)


def test_shrinking_facet_count_does_not_expand_an_uncapped_leaf():
    requested = []
    def handler(request):
        body = json.loads(request.content)
        selected = body.get('appliedFacets', {})
        requested.append(selected)
        if not selected:
            return httpx.Response(200, json={'total': 2000, 'jobPostings': [], 'facets': [
                {'facetParameter': 'jobFamilyGroup', 'values': [
                    {'id': 'a', 'count': 1500}, {'id': 'b', 'count': 1500}]}]})
        assert 'locations' not in selected
        start = 0 if selected['jobFamilyGroup'] == ['a'] else 1500
        offset = body['offset']
        return httpx.Response(200, json={'total': 1499, 'jobPostings': [
            posting(index) for index in range(start+offset, start+min(offset+500,1499))], 'facets': [
                {'facetParameter': 'locations', 'values': [
                    {'id': 'x', 'count': 1000}, {'id': 'y', 'count': 1000}]}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run_scraper(recipe(), client=client)
    assert len(requested) == 9 and len(result.jobs) == 2998
    assert not result.complete  # root count still requires reconciliation


def test_workday_later_pages_without_totals_keep_the_count_witness():
    cache = {}
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise ScraperYieldError('checkpoint probe')
        offset = json.loads(request.content)['offset']
        payload = {'jobPostings': [posting(index) for index in range(offset, min(offset+500, 1500))]}
        if not offset:
            payload['total'] = 1500
        return httpx.Response(200, json=payload)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = run_scraper(recipe(), client=client, detail_cache=cache)
        assert not first.complete
        assert cache[CACHE_KEY]['queue'][0]['expected'] == 1500
        second = run_scraper(recipe(), client=client, detail_cache=cache)
    assert second.complete and len(second.jobs) == 1500


def test_counted_workday_exhaustion_does_not_request_a_repeated_extra_page():
    calls = []
    def handler(request):
        offset = json.loads(request.content)['offset']
        calls.append(offset)
        assert offset < 1000
        payload = {'jobPostings':[posting(index) for index in range(offset,offset+500)]}
        if not offset:
            payload['total'] = 1000
        return httpx.Response(200,json=payload)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run_scraper(recipe(),client=client)
    assert result.complete and len(result.jobs) == 1000
    assert calls == [0,500]


def test_selected_count_facet_ignores_unselected_bucket_counts():
    from server.scrapers.traversal import query_counts
    assert query_counts({'jobFamilyGroup': [{'id': 'a', 'count': 3000},
                                             {'id': 'b', 'count': 10000}]},
                        {'jobFamilyGroup': ['a']}) == [3000]


def test_final_root_refresh_picks_up_new_categories():
    root_reads = 0
    def handler(request):
        nonlocal root_reads
        body = json.loads(request.content)
        chosen = body.get('appliedFacets', {})
        if not chosen:
            root_reads += 1
            values = [{'id': 'a', 'count': 1500}, {'id': 'b', 'count': 1500}]
            if root_reads > 1:
                values.append({'id': 'c', 'count': 10})
            return httpx.Response(200, json={'total': 2000, 'jobPostings': [],
                'facets': [{'facetParameter': 'jobFamilyGroup', 'values': values}]})
        key = chosen['jobFamilyGroup'][0]
        start, total = {'a': (0,1500), 'b': (1500,1500), 'c': (3000,10)}[key]
        offset = body['offset']
        return httpx.Response(200, json={'total': total, 'jobPostings': [posting(index)
            for index in range(start+offset, start+min(offset+500,total))]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run_scraper(recipe(), client=client)
    assert result.complete and len(result.jobs) == 3010
    assert root_reads >= 3


def test_researched_keywords_repair_the_scoped_union_without_numeric_expansion():
    listing = recipe(metadata={'detail_fetch_limit':0,'workday_search_terms':'["SAP","Java"]'})
    calls = []
    def handler(request):
        body = json.loads(request.content)
        term, chosen, offset = body.get('searchText',''), body.get('appliedFacets',{}), body['offset']
        calls.append(term)
        assert term in ('','SAP','Java')
        values = list(range(3000))
        if chosen:
            values = list(range(1500))
        elif term == 'SAP':
            values = list(range(1500,2500))
        elif term == 'Java':
            values = list(range(2500,3000))
        count = len(values)
        available = [{'facetParameter':'timeType','values':[{'id':'full','count':count}]}]
        if not chosen and not term:
            available.append({'facetParameter':'workerSubType','values':[{'id':'a','count':1500},{'id':'b','count':1500}]})
        return httpx.Response(200,json={'total':min(count,2000),'facets':available,
            'jobPostings':[posting(index) for index in values[offset:offset+500]]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run_scraper(listing,client=client)
    assert result.complete and len(result.jobs) == 3000
    assert 'SAP' in calls and 'Java' in calls and calls[-1] == ''


def test_overlapping_facets_trigger_search_repair_and_survive_a_pause():
    cache, paused, calls = {}, False, []
    listing = recipe(metadata={'detail_fetch_limit': 0, 'workday_search_prefixes': '["R"]'})
    def handler(request):
        nonlocal paused
        body = json.loads(request.content)
        chosen, prefix, offset = body.get('appliedFacets', {}), body.get('searchText', ''), body['offset']
        calls.append((chosen, prefix, offset))
        if prefix and not paused:
            paused = True
            raise ScraperYieldError('work slice')
        values = list(range(3000))
        if chosen:
            values = list(range(1500))  # two overlapping buckets omit half the source
        elif prefix and prefix != 'R':
            values = [index for index in values if str(index).startswith(prefix[1:])]
        count = len(values)
        facets = [{'facetParameter': 'timeType', 'values': [{'id': 'full', 'count': count}]}]
        if not chosen:
            facets.append({'facetParameter': 'workerSubType', 'values': [{'id': 'a', 'count': 1500}, {'id': 'b', 'count': 1500}]})
        return httpx.Response(200, json={'total': min(count, 2000), 'facets': facets,
            'jobPostings': [posting(index) for index in values[offset:offset+500]]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = run_scraper(listing, client=client, detail_cache=cache)
        assert not first.complete and len(first.jobs) == 1500
        second = run_scraper(listing, client=client, detail_cache=cache)
    assert second.complete and len(second.jobs) == 3000
    assert all(not chosen for chosen, prefix, offset in calls if prefix)


def test_old_global_observations_do_not_fill_a_facet_union_gap():
    cache, paused = {}, False
    def handler(request):
        nonlocal paused
        body = json.loads(request.content)
        if body.get('appliedFacets') and not paused:
            paused = True
            raise ScraperYieldError('work slice')
        count = 1500 if body.get('appliedFacets') else 3000
        offset = body['offset']
        return httpx.Response(200, json={'total': min(count,2000), 'jobPostings': [posting(index) for index in range(offset,min(offset+500,1500))],
            'facets': [{'facetParameter': 'jobFamilyGroup', 'values': [{'id':'a','count':1500},{'id':'b','count':1500}]}] if not body.get('appliedFacets') else []})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        run_scraper(recipe(), client=client, detail_cache=cache)
        cache[CACHE_KEY]['identities'].extend(posting(index)['externalPath'] for index in range(1500,3000))
        result = run_scraper(recipe(), client=client, detail_cache=cache)
    assert len(cache[CACHE_KEY]['identities']) >= 3000 and not result.complete
    assert any('Facet unions could not establish' in warning for warning in result.warnings)
