"""Public requisition discovery supplements, never substitutes for Workday proof."""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlsplit
from pydantic import ValidationError
from bs4 import BeautifulSoup

from .http import ScraperNetworkError, ScraperYieldError, bounded_request
from .models import RequestConfig
from .normalize import make_job

URL = 'https://www.accenture.com/api/accenture/elastic/findjobs'
ID_PATTERN = re.compile(r'^[A-Za-z0-9-]{1,100}$')


def recover_metadata(recipe, state, data, *, site='in-en', language='en'):
    """Fill bare, already confirmed postings from matching public records."""
    for item in data:
        identifier = item['requisitionId']
        entry = state.get('missing_postings', {}).get(identifier)
        if not entry or entry.get('resolved') or 'req:' + identifier not in state['identities']:
            continue
        if identifier in state['jobs']:
            entry.update(resolved=True, status='already_observed')
            continue
        try:
            url = item.get('jobDetailUrl', '').replace('{0}', site)
            parsed = urlsplit(url)
            if (parsed.scheme != 'https' or parsed.hostname != 'www.accenture.com' or
                parsed.username or parsed.password or parsed.path != f'/{site}/careers/jobdetails' or
                parse_qs(parsed.query).get('id') != [identifier + '_' + language]):
                continue
            location = item.get('location') or []
            description = '\n\n'.join(value for value in (
                item.get('jobDescriptionClean'), item.get('qualificationClean')) if isinstance(value, str))
            job = make_job(company=recipe.company, source_id=identifier, title=item['title'],
                location=(', '.join(location) if isinstance(location, list) else location) or 'Unspecified',
                description=description, url=url, base_url=recipe.careers_url, source='workday',
                employment_type=item.get('employeeType'), remote_mode=item.get('remoteType'))
            state['jobs'][identifier] = job.model_dump(mode='json')
            entry.update(resolved=True, status='public_inventory_metadata_recovered', url=url)
        except (ValidationError, ValueError, TypeError, KeyError, AttributeError):
            continue


def enabled(recipe) -> bool:
    return bool(recipe.metadata.get('workday_accenture_inventory'))


def supported(recipe) -> bool:
    target = urlsplit(recipe.request.url)
    body = recipe.request.json_body or {}
    return (recipe.company.casefold() == 'accenture' and
        target.hostname == 'accenture.wd103.myworkdayjobs.com' and
        target.path == '/wday/cxs/accenture/AccentureCareers/jobs' and
        'www.accenture.com' in recipe.allowed_hosts and not recipe.source_filter
        and not body.get('searchText') and not body.get('appliedFacets'))


def recover_regional_metadata(recipe, client, state, progress) -> tuple[int, bool, list[str]]:
    """Read advertised country search pages to resolve confirmed bare IDs."""
    if not enabled(recipe) or not supported(recipe):
        return 0, False, []
    pending = lambda: sorted(identifier for identifier, entry in state.get('missing_postings', {}).items()
        if not entry.get('resolved') and 'req:' + identifier in state['identities'])
    if not pending() or state.get('metadata_inventory', {}).get('done'):
        return 0, False, []
    lookup = state.setdefault('metadata_inventory', {'queue': [{'site': 'in-en', 'offset': 0}], 'discovered': False})
    pages, warnings = 0, []
    while lookup['queue'] and pending():
        node = lookup['queue'][0]
        try:
            if not node.get('config'):
                page = bounded_request(RequestConfig(url=f"https://www.accenture.com/{node['site']}/careers/jobsearch",
                    timeout_seconds=30, max_response_bytes=5_000_000), recipe.allowed_hosts, client)
                pages += 1
                if not lookup['discovered']:
                    sites = set(re.findall(r'https://www\.accenture\.com/([a-z]{2}-[a-z]{2})/careers/jobsearch', page.text))
                    preferred = ['be-en', 'br-pt', 'sa-en', 'us-en']
                    sites.discard(node['site'])
                    ordered = [site for site in preferred if site in sites] + sorted(sites - set(preferred), key=lambda site: (not site.endswith('-en'), site))
                    lookup['queue'].extend({'site': site, 'offset': 0} for site in ordered)
                    lookup['discovered'] = True
                data = BeautifulSoup(page.text, 'html.parser').select_one('.rad-job-search__filters-and-cards')
                country = data.get('data-countrycode', '') if data else ''
                language = data.get('data-language-code', '') if data else ''
                site = data.get('data-countryselector', '') if data else ''
                if not country or len(country) > 300 or not re.fullmatch(r'[a-zA-Z]{2}(?:-[a-zA-Z]{2})?', language) or not re.fullmatch(r'[a-z]{2}-[a-z]{2}', site):
                    warnings.append(f"Public country metadata configuration unavailable for {node['site']}.")
                    lookup['queue'].pop(0)
                    continue
                node['config'] = {'country': country, 'language': language, 'site': site}
                node['ids'] = pending()
            fixed = node['ids'][node['offset']:node['offset'] + 20]
            requested = [identifier for identifier in fixed if identifier in pending()]
            if not fixed:
                lookup['queue'].pop(0)
                continue
            if requested:
                config = node['config']
                response = bounded_request(RequestConfig(url=URL, method='POST', timeout_seconds=30,
                    max_response_bytes=20_000_000), recipe.allowed_hosts, client, form_body={
                    'startIndex': '0', 'maxResultSize': '200', 'jobKeyword': '', 'jobCountry': config['country'],
                    'jobLanguage': config['language'], 'countrySite': config['site'], 'sortBy': '1',
                    'searchType': 'vectorSearch', 'jobFilters': json.dumps([
                        {'fieldName': 'requisitionId.keyword', 'items': requested, 'multiSelect': True}])})
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
                    raise ValueError('missing metadata records')
                rows = [item for item in payload['data'] if isinstance(item, dict) and item.get('requisitionId') in requested]
                recover_metadata(recipe, state, rows, site=config['site'], language=config['language'])
                pages += 1
            node['offset'] += len(fixed)
            progress(pages, len(state['jobs']))
        except ScraperYieldError:
            state['paused'] = True
            return pages, True, warnings + ['Regional public metadata lookup paused; its checkpoint resumes next run.']
        except (ScraperNetworkError, ValueError, TypeError, KeyError):
            return pages, True, warnings + ['Regional public metadata lookup failed; its checkpoint is retained.']
    lookup['done'] = True
    lookup['queue'] = []
    return pages, False, warnings


def prepare(recipe, client, state, progress) -> tuple[int, bool, list[str]]:
    """Checkpoint the India public index, then queue exact Workday ID searches.

    Inventory records never become jobs or add to the source coverage count.
    Only the configured company-owned Workday board can confirm those IDs.
    """
    if not enabled(recipe) or state.get('inventory', {}).get('done'):
        return 0, False, []
    if (state.get('expected', 0) > 0 and len(state['identities']) >= state['expected']
        and not state.get('unprovable') and (state.get('whole_source_verified') or
        all(group.get('verified') for kind in ('facet_groups', 'search_groups')
            for group in state.get(kind, {}).values()))):
        # A discovery fallback must not make complete primary coverage depend
        # on an unrelated outage in the companion index.
        return 0, False, []
    if not supported(recipe):
        return 0, True, ['Accenture inventory requires its dedicated unfiltered Workday board and explicit public host.']
    inventory = state.setdefault('inventory', {'queue': [{'filters': [], 'offset': 0}], 'ids': []})
    config = RequestConfig(url=URL, method='POST', timeout_seconds=30, max_response_bytes=20_000_000)
    pages, warnings = 0, []
    while inventory['queue']:
        node = inventory['queue'][0]
        try:
            response = bounded_request(config, recipe.allowed_hosts, client, form_body={
                'startIndex': str(node['offset']), 'maxResultSize': '200', 'jobKeyword': '',
                'jobCountry': 'India', 'jobLanguage': 'en', 'countrySite': 'in-en',
                'sortBy': '1', 'searchType': 'vectorSearch', 'jobFilters': json.dumps(node['filters'])})
            payload = response.json()
            data, groups = payload['data'], payload['aggregations']
            if not isinstance(data, list) or not isinstance(groups, list):
                raise ValueError('missing inventory collections')
            witness = next(g['items'] for g in groups if g['fieldName'] == 'employeeType.keyword')
            expected = sum(item['count'] for item in witness)
            if not isinstance(expected, int) or expected < 0:
                raise ValueError('invalid inventory count')
            ids = {item['requisitionId'] for item in data}
            if any(not isinstance(identifier, str) or not ID_PATTERN.fullmatch(identifier) for identifier in ids):
                raise ValueError('invalid inventory requisition ID')
        except ScraperYieldError:
            state['paused'] = True
            return pages, True, ['Public requisition inventory paused at its work budget; the next run resumes.']
        except (ScraperNetworkError, ValueError, KeyError, TypeError, StopIteration):
            return pages, True, ['Public requisition inventory failed; its checkpoint is retained and coverage stays incomplete.']
        pages += 1
        recover_metadata(recipe, state, data)
        inventory['ids'] = sorted(set(inventory['ids']) | ids)
        if not node['filters']:
            inventory['expected'] = expected
        if expected > 10000 and node['offset'] == 0:
            used = {f['fieldName'] for f in node['filters']}
            field = next((f for f in ('yearsOfExperience.keyword', 'location.keyword') if f not in used), None)
            buckets = next((g['items'] for g in groups if g['fieldName'] == field), [])
            if buckets:
                inventory['queue'][0:1] = [{'filters': node['filters'] + [
                    {'fieldName': field, 'items': [b['term']], 'multiSelect': False}], 'offset': 0} for b in buckets]
            else:
                inventory.setdefault('gaps', []).append('Inventory query exceeds its result window without a partition.')
                inventory['queue'].pop(0)
        elif node.get('last_ids') == sorted(ids) and ids:
            inventory.setdefault('gaps', []).append('Inventory repeated a page before exhaustion.')
            inventory['queue'].pop(0)
        else:
            node['offset'] += len(data)
            node['last_ids'] = sorted(ids)
            if not data or node['offset'] >= expected:
                inventory['queue'].pop(0)
        progress(pages, len(state['jobs']))
    # This inventory is a discovery aid: even a partial inventory is useful,
    # but it never establishes coverage or closes absent Workday roles.
    inventory['done'] = True
    warnings.extend(inventory.get('gaps', []))
    absent = [identifier for identifier in inventory['ids'] if 'req:' + identifier not in state['identities']]
    roots = [key for key, group in state.get('facet_groups', {}).items()
             if not group['filters'] and not group.get('search_text')]
    inventory['requested_ids'] = absent
    state['queue'].extend({'filters': {}, 'offset': 0, 'expected': 0, 'seen': [],
        'search_text': ' OR '.join('"' + identifier + '"' for identifier in absent[start:start + 20]),
        'prefer_search': True, 'facet_groups': roots} for start in range(0, len(absent), 20))
    state['queue'].extend(state.pop('inventory_deferred_repairs', []))
    if roots:
        state['queue'].append({'filters': {}, 'offset': 0, 'expected': state['expected'], 'seen': [],
            'scope_recheck': {'kind': 'facet_groups', 'id': roots[0]}, 'facet_groups': [roots[0]]})
    return pages, False, warnings
