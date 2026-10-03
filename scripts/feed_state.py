"""Persist only bounded anonymous state and publication artifacts between builds."""
import gzip
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / '.feed-state'
SITE = ROOT / '_site'
PUBLISH = ROOT / '.publish-state'
PART_BYTES = 32 * 1024 * 1024


def restore():
    marker = json.loads((STATE / 'publisher.json').read_text())
    if marker == {'format': 'jobhound-public-state', 'version': 1}:
        paths = [STATE / 'state.json.gz']
    elif marker.get('format') == 'jobhound-public-state' and marker.get('version') == 2:
        parts = marker.get('parts')
        if not isinstance(parts, list) or not parts:
            raise ValueError('Missing public state parts')
        paths = []
        for index, part in enumerate(parts):
            name = f'state-{index:05d}.json.gz'
            if not isinstance(part, dict) or part.get('path') != name:
                raise ValueError('Invalid public state part order or path')
            path = STATE / name
            with path.open('rb') as source:
                digest = hashlib.file_digest(source, 'sha256').hexdigest()
            if path.stat().st_size != part.get('bytes') or digest != part.get('sha256'):
                raise ValueError(f'Public state integrity check failed: {name}')
            paths.append(path)
    else:
        raise ValueError('Unrecognized generated state branch')
    temporary = STATE / 'state.json.tmp'
    try:
        with temporary.open('wb') as destination:
            for path in paths:
                with gzip.open(path, 'rb') as source:
                    shutil.copyfileobj(source, destination)
        temporary.replace(STATE / 'state.json')
    finally:
        temporary.unlink(missing_ok=True)
    shutil.copytree(STATE / 'site', SITE, dirs_exist_ok=True)


def prepare():
    PUBLISH.mkdir(exist_ok=True)
    parts = []
    with (STATE / 'state.json').open('rb') as source:
        while block := source.read(PART_BYTES):
            name = f'state-{len(parts):05d}.json.gz'
            content = gzip.compress(block, compresslevel=9, mtime=0)
            (PUBLISH / name).write_bytes(content)
            parts.append({'path': name, 'bytes': len(content),
                          'sha256': hashlib.sha256(content).hexdigest()})
    if not parts:
        raise ValueError('Empty public state')
    shutil.copytree(SITE, PUBLISH / 'site', dirs_exist_ok=True)
    (PUBLISH / 'publisher.json').write_text(json.dumps({
        'format': 'jobhound-public-state', 'version': 2, 'parts': parts}))
    oversized = [path.relative_to(PUBLISH).as_posix() for path in PUBLISH.rglob('*')
                 if path.is_file() and path.stat().st_size > 90_000_000]
    if oversized:
        raise ValueError(f'Public state files exceed the 90 MB Git safety budget: {oversized}')


def main():
    if sys.argv[1:] == ['restore']:
        restore()
    elif sys.argv[1:] == ['prepare']:
        prepare()
    else:
        raise SystemExit('Usage: feed_state.py restore|prepare')


if __name__ == '__main__':
    main()
