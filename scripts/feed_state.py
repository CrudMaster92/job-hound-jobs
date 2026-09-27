"""Persist only bounded anonymous state and publication artifacts between builds."""
import gzip
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / '.feed-state'
SITE = ROOT / '_site'
PUBLISH = ROOT / '.publish-state'


def main():
    if sys.argv[1:] == ['restore']:
        marker = json.loads((STATE / 'publisher.json').read_text())
        if marker != {'format': 'jobhound-public-state', 'version': 1}:
            raise ValueError('Unrecognized generated state branch')
        with gzip.open(STATE / 'state.json.gz', 'rb') as source, (STATE / 'state.json').open('wb') as destination:
            shutil.copyfileobj(source, destination)
        shutil.copytree(STATE / 'site', SITE, dirs_exist_ok=True)
    elif sys.argv[1:] == ['prepare']:
        PUBLISH.mkdir(exist_ok=True)
        with (STATE / 'state.json').open('rb') as source, gzip.open(PUBLISH / 'state.json.gz', 'wb', compresslevel=9) as destination:
            shutil.copyfileobj(source, destination)
        shutil.copytree(SITE, PUBLISH / 'site', dirs_exist_ok=True)
        (PUBLISH / 'publisher.json').write_text(json.dumps({'format': 'jobhound-public-state', 'version': 1}))
        if any(path.stat().st_size > 90_000_000 for path in PUBLISH.rglob('*') if path.is_file()):
            raise ValueError('Public state file exceeds its 90 MB Git safety budget')
    else:
        raise SystemExit('Usage: feed_state.py restore|prepare')


if __name__ == '__main__':
    main()
