"""Read-only catalog/feed drift diagnostics; never admits or executes recipes."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .schema import digest, json_bytes


def catalog_coverage(root: Path, lock: dict) -> dict:
    admitted = {entry['id']: entry for entry in lock['monitors']}
    excluded = {entry['id']: entry for entry in lock.get('excluded', [])}
    current = {}
    for path in sorted((root / 'companies').glob('*/monitors/*.json')):
        if path.is_symlink() or path.stat().st_size > 512_000:
            raise ValueError('Catalog diagnostics require bounded normal JSON files')
        raw = path.read_bytes()
        item = json.loads(raw)
        current[item['id']] = (item, digest(raw.replace(b'\r\n', b'\n')))
    if not current:
        raise ValueError('Catalog diagnostics found no monitor sources')
    missing, outdated, bounded = [], [], []
    for mid, (item, sha) in current.items():
        pin = admitted.get(mid)
        if not pin and mid not in excluded:
            missing.append(mid)
        if pin and pin['sha256'] != sha:
            outdated.append({'id': mid, 'pinned_revision': pin['revision'], 'catalog_revision': item['revision']})
        recipe = item['recipe']
        pagination = recipe.get('pagination', {})
        if recipe.get('coverage_mode', 'bounded') != 'all' and pagination.get('kind', 'none') != 'none':
            bounded.append({'id': mid, 'max_pages': pagination.get('max_pages', 10),
                            'page_size': pagination.get('page_size', 100)})
    collections = []
    pinned_collections = {item['id'] for item in lock['collections']}
    for path in sorted((root / 'collections').glob('*.json')):
        item = json.loads(path.read_text('utf-8'))
        ids = set()
        for member in item['companies']:
            explicit = member.get('monitor_ids')
            ids.update(explicit if explicit is not None else
                       [mid for mid, (monitor, _) in current.items() if monitor['company_id'] == member['company_id']])
        collections.append({'id': item['id'], 'present_in_feed': item['id'] in pinned_collections,
                            'catalog_monitors': len(ids), 'admitted': len(ids & admitted.keys()),
                            'excluded': len(ids & excluded.keys()),
                            'not_pinned': sorted(ids - admitted.keys() - excluded.keys())})
    return {'format': 'jobhound-public-coverage', 'version': 1, 'catalog_monitors': len(current),
            'admitted_monitors': len(admitted), 'excluded_monitors': len(excluded),
            'not_pinned': missing, 'outdated_pins': outdated, 'bounded_pagination': bounded,
            'collections': collections}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', required=True, type=Path)
    parser.add_argument('--lock', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    report = catalog_coverage(args.catalog, json.loads(args.lock.read_text('utf-8')))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(json_bytes(report))
    print(json.dumps({'coverage': {key: len(report[key]) for key in ('not_pinned', 'outdated_pins', 'bounded_pagination')}}))


if __name__ == '__main__':
    main()
