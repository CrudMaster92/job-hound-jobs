"""Checkpointed listing traversal; work budgets never imply source exhaustion."""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlsplit

from .http import ScraperNetworkError, ScraperYieldError, bounded_request
from .models import JobRecord, ScraperRecipe, ScraperStrategy
from . import adapters, missing_postings, workday_inventory

CACHE_KEY = "__jobhound_listing_v1__"
# Only these single-valued Workday dimensions provide a source-count witness.
COUNT_FACETS = {"jobFamilyGroup", "timeType"}
SPLIT_FACETS = ("jobFamilyGroup", "locationCountry", "locations", "timeType", "workerSubType")
JSON_STRATEGIES = {ScraperStrategy.ASHBY, ScraperStrategy.GREENHOUSE, ScraperStrategy.LEVER,
                   ScraperStrategy.SMARTRECRUITERS, ScraperStrategy.WORKDAY, ScraperStrategy.GENERIC_JSON}


def facets(payload: dict) -> dict[str, list[dict]]:
    found = {}
    def visit(items):
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            values = item.get("values", [])
            values = values if isinstance(values, list) else []
            key = item.get("facetParameter")
            buckets = [value for value in values if isinstance(value, dict)
                       and isinstance(value.get("id"), str)
                       and isinstance(value.get("count"), int) and value["count"] > 0]
            if key and buckets:
                found[key] = buckets
            visit(values)
    visit(payload.get("facets", []))
    return found


def query_counts(available: dict, filters: dict) -> list[int]:
    """A facet ignores its own selection: count only the selected buckets."""
    counts = []
    for key in COUNT_FACETS:
        if key not in available:
            continue
        values = available[key]
        if key in filters:
            values = [value for value in values if value['id'] in filters[key]]
        counts.append(sum(value['count'] for value in values))
    return counts


def upgrade_workday_identities(state: dict) -> None:
    """Migrate path checkpoints conservatively to stable requisition identities."""
    if state.get('identity_version') == 2:
        return
    paths = {}
    for identifier, job in state['jobs'].items():
        for field in ('source_url', 'canonical_url'):
            path = urlsplit(job.get(field, '')).path
            if path:
                paths[path] = 'req:' + identifier
                if '/job/' in path:
                    paths['/job/' + path.split('/job/', 1)[1]] = 'req:' + identifier
    def convert(values):
        # Unmapped historical URLs retain their jobs, but cannot inflate proof.
        return sorted({('req:' + value[5:] if value.startswith('stub:') else paths.get(value))
                       for value in values} - {None})
    state['identities'] = convert(state['identities'])
    for node in [*state['queue'], *state.get('gap_queries', [])]:
        node['seen'] = convert(node['seen'])
    for group in [*state.get('search_groups', {}).values(), *state.get('facet_groups', {}).values()]:
        group['seen'] = convert(group['seen'])
    state['identity_version'] = 2


def traverse(recipe: ScraperRecipe, client, cache: dict, normalize: Callable,
             progress: Callable) -> tuple[list[JobRecord], int, bool, list[str]]:
    """Resume a listing snapshot. A recipe change invalidates its checkpoint."""
    signature = hashlib.sha256(json.dumps(recipe.model_dump(mode="json"),
                                         sort_keys=True).encode()).hexdigest()
    state = cache.get(CACHE_KEY)
    if not isinstance(state, dict) or state.get("signature") != signature:
        state = {"signature": signature, "queue": [{"filters": {}, "offset": 0,
                 "expected": 0, "seen": []}], "jobs": {}, "identities": [],
                 "expected": 0, "complete": False, "gaps": 0}
        cache[CACHE_KEY] = state
    state['paused'] = False
    pages, warnings = 0, []
    pagination = recipe.pagination
    workday = recipe.strategy == ScraperStrategy.WORKDAY
    if workday:
        upgrade_workday_identities(state)
        if not state.get('workday_url_version'):
            for job in state['jobs'].values():
                for field in ('source_url','canonical_url'):
                    parsed = urlsplit(job.get(field,''))
                    if parsed.hostname == urlsplit(recipe.request.url).hostname and parsed.path.startswith('/job/'):
                        job[field] = adapters.workday_public_url(parsed.path,recipe)
            state['workday_url_version'] = 1
    while state["queue"]:
        node = state["queue"][0]
        params = dict(recipe.request.params)
        body = copy.deepcopy(recipe.request.json_body or {})
        target = body if recipe.request.method == "POST" else params
        offset = node["offset"]
        if pagination.kind != "none":
            if pagination.kind not in {"offset", "page"}:
                raise ValueError("all-role traversal requires offset or page pagination")
            target[pagination.parameter or pagination.kind] = (
                offset if pagination.kind == "offset" else offset // pagination.page_size + 1)
            if pagination.page_size_parameter:
                target[pagination.page_size_parameter] = pagination.page_size
        if workday:
            body["appliedFacets"] = {**body.get("appliedFacets", {}), **node["filters"]}
            if 'search_text' in node:
                body['searchText'] = node['search_text']
        try:
            response = bounded_request(recipe.request, recipe.allowed_hosts, client,
                                       params=params, json_body=body or None)
        except ScraperYieldError:
            state['paused'] = True
            warnings.append("Listing traversal paused at its work budget; the next run resumes its checkpoint.")
            break
        except ScraperNetworkError as error:
            warnings.append(f"Listing request failed ({type(error).__name__}); saved traversal remains incomplete and retries next run.")
            break
        # Commit a checkpoint only after successful parsing/normalization.
        try:
            payload = response.json() if recipe.strategy in JSON_STRATEGIES else response.text
        except ValueError:
            warnings.append('Source returned invalid JSON; saved traversal remains incomplete.')
            break
        records = None
        if recipe.strategy == ScraperStrategy.GENERIC_JSON:
            records = payload
            for part in recipe.mapping.get('items','').split('.') if recipe.mapping.get('items') else []:
                if isinstance(records,dict):
                    records = records.get(part)
                elif isinstance(records,list) and part.isdigit() and int(part) < len(records):
                    records = records[int(part)]
                else:
                    records = None
                    break
        elif recipe.strategy == ScraperStrategy.LEVER:
            records = payload
        elif recipe.strategy in JSON_STRATEGIES and isinstance(payload, dict):
            key = 'jobPostings' if workday else 'content' if recipe.strategy == ScraperStrategy.SMARTRECRUITERS else 'jobs'
            records = payload.get(key)
        if recipe.strategy in JSON_STRATEGIES and not isinstance(records, list):
            warnings.append('Source omitted its expected listing collection; saved traversal remains incomplete.')
            break
        normalized = normalize(payload)
        pages += 1
        if isinstance(payload, dict):
            candidates = ("content", "jobPostings", "jobs")
            if records is None:
                records = next((payload[key] for key in candidates
                                if isinstance(payload.get(key), list)), None)
            total = next((payload[key] for key in ("total", "totalCount", "totalFound", "hits")
                          if isinstance(payload.get(key), int)), 0)
            if records is None:
                records, count = [], len(normalized)
            else:
                count = len(records)
        elif isinstance(payload, list):
            records, count, total = payload, len(payload), 0
        else:
            records, count, total = [], len(normalized), 0
        identities = set()
        if workday:
            missing = state.setdefault('missing_postings', {})
            for raw in records:
                if not isinstance(raw, dict):
                    continue
                path = raw.get('externalPath') or raw.get('url')
                fields = raw.get('bulletFields') or []
                info = raw.get('jobPostingInfo') or raw
                identifier = str(info.get('jobReqId') or info.get('jobPostingId') or (fields[0] if fields else '') or path or '')
                if identifier:
                    identity = 'req:' + identifier
                    if path and identifier in missing and (info.get('title') or raw.get('title')):
                        missing[identifier]['resolved'] = True
                else:
                    state['unprovable'] = True
                    continue
                identities.add(identity)
                if identifier and (not path or not (info.get('title') or raw.get('title'))):
                    known = identifier in state['jobs']
                    missing.setdefault(identifier, {'raw': raw, 'resolved': known, 'status': 'already_observed' if known else 'pending'})
        else:
            identities = {job.source_id for job in normalized}
        for job in normalized:
            state["jobs"][job.source_id] = job.model_dump(mode="json")
            entry = state.get('missing_postings', {}).get(job.source_id)
            if entry is not None:
                entry.update(resolved=True, status='observed_listing_metadata')
        state["identities"] = sorted(set(state["identities"]) | identities)
        prior_seen = set(node["seen"])
        node["seen"] = sorted(prior_seen | identities)
        # Parent evidence may seed a repair's coverage union. It is not evidence
        # that this query already fetched the same page.
        prior_page_seen = set(node.get('page_seen', node.get('last_page', []))) if offset else set()
        node['page_seen'] = sorted(prior_page_seen | identities)
        group = state.get('search_groups', {}).get(node.get('search_group'))
        if group is not None:
            group['seen'] = sorted(set(group['seen']) | identities)
        for group_id in node.get('facet_groups', []):
            facet_group = state['facet_groups'][group_id]
            facet_group['seen'] = sorted(set(facet_group['seen']) | identities)
        available = facets(payload) if workday and isinstance(payload, dict) else {}
        if workday and not node["filters"] and total >= 2000 and not any(
            key in available for key in COUNT_FACETS
        ):
            # A saturated result window without an independent count witness
            # cannot establish that the search exposes the whole source.
            state["unprovable"] = True
        # Facets expose real counts even when the Workday search window says 2000.
        independent_counts = query_counts(available, body.get('appliedFacets', {}))
        witnessed = max([total, *independent_counts])
        if not independent_counts and total == 0 and (offset or count):
            # Workday commonly omits totals/facets on subsequent pages.
            # Missing metadata is not evidence that the query became empty.
            witnessed = node["expected"]
        # An uncapped current response supersedes an older parent facet count.
        # Otherwise normal source churn triggers unnecessary facet expansion.
        if workday and total >= 2000 and not independent_counts:
            witnessed = max(witnessed, node["expected"])
        node["expected"] = witnessed
        if not node["filters"] and 'search_text' not in node:
            state["expected"] = witnessed
        query_key = (hashlib.sha256(json.dumps([body.get('appliedFacets', {}), body.get('searchText', '')],
            sort_keys=True).encode()).hexdigest() if workday else None)
        reused_query = False
        memo = state.get('finished_queries', {}).get(query_key)
        if (workday and offset == 0 and 0 < witnessed < 2000 and isinstance(memo, dict)
            and memo.get('expected') == witnessed and isinstance(memo.get('seen'), list)
            and not any(node.get(key) for key in ('scope_recheck', 'facet_recheck', 'group_recheck', 'recheck'))):
            cached_ids = set(memo['seen'])
            if len(cached_ids) >= witnessed and cached_ids <= set(state['identities']):
                # Reuse only proven evidence for this identical query and crawl
                # generation, after reading its current first page/count.
                # Cached jobs retain their original observation timestamps.
                node['seen'] = sorted(set(node['seen']) | cached_ids)
                if group is not None:
                    group['seen'] = sorted(set(group['seen']) | cached_ids)
                for identifier in node.get('facet_groups', []):
                    parent = state['facet_groups'][identifier]
                    parent['seen'] = sorted(set(parent['seen']) | cached_ids)
                reused_query = True
        split = None
        if workday and offset == 0 and not node.get('prefer_search') and not node.get('facet_recheck') and not node.get('scope_recheck') and (witnessed > total or total >= 2000):
            for key in SPLIT_FACETS:
                values = available.get(key, [])
                if key not in node["filters"] and len(values) > 1 and (
                    sum(value["count"] for value in values) >= witnessed
                ):
                    split = (key, values)
                    break
        search_split = None
        keyword_split = False
        if workday and offset == 0 and not split and witnessed >= 2000 and not node.get('group_recheck') and not node.get('facet_recheck') and not node.get('scope_recheck') and not (recipe.request.json_body or {}).get('searchText'):
            if 'search_text' in node and node['search_text'] and recipe.metadata.get('workday_search_prefixes'):
                search_split = [node['search_text'] + str(digit) for digit in range(10)]
            elif 'search_text' not in node or not node['search_text']:
                try:
                    keyword_split = bool(recipe.metadata.get('workday_search_terms'))
                    configured = json.loads(str(recipe.metadata.get('workday_search_terms') if keyword_split else recipe.metadata.get('workday_search_prefixes', '[]')))
                except ValueError:
                    configured = []
                if isinstance(configured, list) and configured and all(isinstance(value, str) and value for value in configured):
                    search_split = list(dict.fromkeys(configured))
        if reused_query:
            state['queue'].pop(0)
        elif node.get('scope_recheck'):
            scope_group = state[node['scope_recheck']['kind']][node['scope_recheck']['id']]
            scope_group['expected'] = witnessed
            scope_group['verified'] = len(scope_group['seen']) >= witnessed
            state['queue'].pop(0)
            state['scope_check_pending'] = False
            if scope_group['verified'] and not state.get('unprovable'):
                # Proof applies only to this exact query. A smaller scope can
                # finish its descendants without claiming whole-board coverage.
                if not scope_group['filters'] and not scope_group.get('search_text'):
                    state['whole_source_verified'] = True
                    state['root_verified'] = True
                    state['queue'].clear()
                else:
                    kind, identifier = node['scope_recheck']['kind'], node['scope_recheck']['id']
                    state['queue'] = [queued for queued in state['queue']
                        if (queued.get('facet_recheck') or queued.get('group_recheck') or queued.get('scope_recheck')
                            or (identifier not in queued.get('facet_groups', []) if kind == 'facet_groups'
                                else queued.get('search_group') != identifier))]
        elif node.get('facet_recheck'):
            facet_group = state['facet_groups'][node['facet_recheck']]
            facet_group['expected'] = witnessed
            facet_group['verified'] = len(facet_group['seen']) >= witnessed
            state['queue'].pop(0)
            if not facet_group['verified'] and not node.get('repair_attempted') and 'search_text' not in node and (recipe.metadata.get('workday_search_prefixes') or recipe.metadata.get('workday_search_terms')):
                repair = {'filters': copy.deepcopy(node['filters']), 'offset': 0, 'expected': witnessed,
                          'seen': list(facet_group['seen']), 'prefer_search': True, 'facet_groups': node['facet_groups']}
                repairs = [repair, {**node, 'seen': [], 'repair_attempted': True}]
                if workday_inventory.enabled(recipe) and not state.get('inventory', {}).get('done'):
                    state.setdefault('inventory_deferred_repairs', []).extend(repairs)
                else:
                    state['queue'][0:0] = repairs
        elif node.get('group_recheck'):
            group['expected'] = witnessed
            group['verified'] = len(group['seen']) >= witnessed
            state['queue'].pop(0)
        elif node.get('recheck'):
            state['queue'].pop(0)
            state['root_verified'] = True
            partition = state.get('root_partition', {})
            key = partition.get('key')
            new_values = [value for value in available.get(key, [])
                          if value['id'] not in partition.get('ids', [])]
            if new_values:
                partition.setdefault('ids', []).extend(value['id'] for value in new_values)
                root_groups = [identifier for identifier, value in state.get('facet_groups', {}).items() if not value['filters']]
                state['queue'].extend({'filters': {key: [value['id']]}, 'offset': 0,
                                       'expected': value['count'], 'seen': [], 'facet_groups': root_groups} for value in new_values)
                for identifier in root_groups:
                    state['queue'].append({'filters': {}, 'offset': 0, 'expected': witnessed, 'seen': [],
                                           'facet_groups': [identifier], 'facet_recheck': identifier})
                state['root_verified'] = False
        elif search_split:
            if workday_inventory.enabled(recipe) and not state.get('inventory', {}).get('done'):
                state['queue'].pop(0)
                state.setdefault('inventory_deferred_repairs', []).append(copy.deepcopy(node))
                progress(pages, len(state['jobs']))
                continue
            group_id = node.get('search_group')
            if not group_id:
                group_id = hashlib.sha256(json.dumps([node['filters'],node.get('search_text','')], sort_keys=True).encode()).hexdigest()
                state.setdefault('search_groups', {})[group_id] = {'filters': copy.deepcopy(node['filters']),
                    'search_text':node.get('search_text',''), 'expected': witnessed, 'seen': list(node['seen']), 'verified': False}
            state['queue'].pop(0)
            children = [{'filters': copy.deepcopy(node['filters']), 'offset': 0, 'expected': 0,
                         'seen': [], 'search_text': value, 'search_group': group_id, 'prefer_search': not keyword_split,
                         'facet_groups': node.get('facet_groups', [])} for value in search_split]
            if not node.get('search_group'):
                children.append({'filters': copy.deepcopy(node['filters']), 'offset': 0, 'expected': witnessed,
                                 'seen': [], 'search_group': group_id, 'group_recheck': True,
                                 'facet_groups': node.get('facet_groups', [])})
            state['queue'][0:0] = children
        elif offset and identities and identities <= prior_page_seen:
            if not witnessed or len(node['seen']) < witnessed:
                state["gaps"] += 1
                state.setdefault('gap_queries', []).append(copy.deepcopy(node))
            state["queue"].pop(0)
        elif split:
            key, values = split
            facet_id = hashlib.sha256(json.dumps([node['filters'], node.get('search_text', '')], sort_keys=True).encode()).hexdigest()
            state.setdefault('facet_groups', {})[facet_id] = {'filters': copy.deepcopy(node['filters']),
                'search_text':node.get('search_text',''), 'expected': witnessed, 'seen': list(node['seen']), 'verified': False}
            memberships = [*node.get('facet_groups', []), facet_id]
            if not node['filters']:
                state['root_partition'] = {'key': key, 'ids': [value['id'] for value in values]}
            state["queue"].pop(0)
            state["queue"][0:0] = [{**{field: node[field] for field in ('search_text', 'search_group') if field in node},
                                   "filters": {**node["filters"], key: [value["id"]]},
                                   "offset": 0, "expected": value["count"], "seen": [], 'facet_groups': memberships}
                                  for value in sorted(values, key=lambda value: (value["count"], value["id"]))] + [
                                      {**node, 'offset': 0, 'seen': [], 'facet_groups': memberships, 'facet_recheck': facet_id}]
        elif pagination.kind == "none" or count < pagination.page_size or (
            total > 0 and offset + count >= total
        ) or (
            workday and witnessed > 0 and len(node['seen']) >= witnessed
        ):
            if workday and len(node["seen"]) < witnessed:
                state["gaps"] += 1
                state.setdefault('gap_queries', []).append(copy.deepcopy(node))
            elif not workday and total and offset + count < total:
                state["gaps"] += 1
            if workday and 0 < witnessed < 2000 and len(node['seen']) >= witnessed:
                state.setdefault('finished_queries', {})[query_key] = {
                    'expected': witnessed, 'seen': list(node['seen'])}
            state["queue"].pop(0)
        else:
            prior = node.get("last_page")
            current = sorted(identities)
            if prior == current and current:
                state["gaps"] += 1
                state["queue"].pop(0)
            else:
                node["last_page"] = current
                node["offset"] += pagination.page_size
        progress(pages, len(state["jobs"]))
        if (workday and state['queue'] and state['expected'] > 2000
            and not state.get('unprovable') and not state.get('scope_check_pending')):
            for kind in ('facet_groups','search_groups'):
                candidate = next(((key,value) for key,value in state.get(kind,{}).items()
                                  if (not value.get('verified') or (not value['filters'] and not value.get('search_text')
                                      and not state.get('whole_source_verified')))
                                  and value.get('expected', 0) > 0
                                  and len(value['seen']) >= value['expected']),None)
                if candidate:
                    identifier, value = candidate
                    state['scope_check_pending'] = True
                    state['queue'].insert(0,{'filters':copy.deepcopy(value['filters']),'offset':0,'expected':value['expected'],'seen':[],
                        'scope_recheck':{'kind':kind,'id':identifier},
                        **({'search_text':value['search_text']} if value.get('search_text') else {}),
                        'facet_groups':[key for key, parent in state.get('facet_groups', {}).items()
                            if all(value['filters'].get(field) == selected for field, selected in parent['filters'].items())
                            and (not parent.get('search_text') or parent['search_text'] == value.get('search_text'))],
                        **({'search_group':identifier} if kind == 'search_groups' else {})})
                    break
    if not state["queue"]:
        if workday and state['expected'] > 2000 and not state.get('root_verified'):
            state['queue'].append({'filters': {}, 'offset': 0, 'expected': state['expected'],
                                   'seen': [], 'recheck': True})
            jobs, extra_pages, complete, extra_warnings = traverse(recipe, client, cache, normalize, progress)
            return jobs, pages + extra_pages, complete, warnings + extra_warnings
        if workday:
            extra_pages, pending, inventory_warnings = workday_inventory.prepare(recipe, client, state, progress)
            pages += extra_pages
            warnings.extend(inventory_warnings)
            if pending:
                return ([JobRecord.model_validate(value) for value in state['jobs'].values()], pages, False, warnings)
            if state['queue']:
                jobs, extra_pages, complete, extra_warnings = traverse(recipe, client, cache, normalize, progress)
                return jobs, pages + extra_pages, complete, warnings + extra_warnings
            if not state.get('listing_finished_at'):
                state['listing_finished_at'] = datetime.now(timezone.utc).isoformat()
            extra_pages, pending, metadata_warnings = workday_inventory.recover_regional_metadata(recipe, client, state, progress)
            pages += extra_pages
            warnings.extend(metadata_warnings)
            if pending:
                return ([JobRecord.model_validate(value) for value in state['jobs'].values()], pages, False, warnings)
            extra_pages, extra_warnings = missing_postings.recover(recipe, client, state)
            pages += extra_pages
            warnings.extend(extra_warnings)
        unresolved = sum(not entry.get('resolved') for entry in state.get('missing_postings', {}).values())
        incomplete_groups = sum(not group.get('verified') for group in state.get('search_groups', {}).values())
        incomplete_facets = sum(not group.get('verified') for group in state.get('facet_groups', {}).values())
        if state.get('whole_source_verified'):
            incomplete_groups = incomplete_facets = 0
        if incomplete_facets:
            warnings.append(f"Facet unions could not establish full coverage for {incomplete_facets} searches.")
        if incomplete_groups:
            warnings.append(f"Requisition search partitions could not establish full coverage for {incomplete_groups} capped searches.")
        if unresolved:
            warnings.append(f"Public metadata is still missing for {unresolved} advertised postings; no placeholder roles were fabricated.")
        # Overlapping facet buckets are acceptable only when their union covers
        # the independent advertised source count. Missing buckets stay partial.
        state["complete"] = (len(state["identities"]) >= state["expected"]
                             and not state.get("unprovable")
                             and not unresolved
                             and not incomplete_groups
                             and not incomplete_facets
                             and (state["expected"] > 0 or not state["gaps"])
                             if workday else not state["gaps"])
        if not state["complete"]:
            warnings.append("Source result window could not be fully traversed: "
                            f"observed {len(state['identities'])}/{state['expected']} advertised postings.")
    return ([JobRecord.model_validate(value) for value in state["jobs"].values()],
            pages, bool(state["complete"]), warnings)
