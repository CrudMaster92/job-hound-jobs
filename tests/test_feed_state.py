"""Durable publication state survives Git's individual file size limit."""
import gzip
import importlib.util
import json
import os
from pathlib import Path
import shutil

import pytest


@pytest.fixture
def state_files(tmp_path):
    spec = importlib.util.spec_from_file_location('feed_state', Path(__file__).parents[1] / 'scripts/feed_state.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.STATE = tmp_path / 'state'
    module.SITE = tmp_path / 'site'
    module.PUBLISH = tmp_path / 'publish'
    module.PART_BYTES = 128
    module.STATE.mkdir()
    module.SITE.mkdir()
    (module.SITE / 'manifest.json').write_text('{"generation":"test"}')
    return module


def test_chunked_state_round_trip(state_files):
    m = state_files
    original = os.urandom(1025)
    (m.STATE / 'state.json').write_bytes(original)
    m.prepare()
    marker = json.loads((m.PUBLISH / 'publisher.json').read_text())
    assert marker['version'] == 2
    assert len(marker['parts']) == 9
    assert all(part['bytes'] < 200 for part in marker['parts'])
    shutil.copytree(m.PUBLISH, m.STATE, dirs_exist_ok=True)
    (m.STATE / 'state.json').write_bytes(b'old')
    m.restore()
    assert (m.STATE / 'state.json').read_bytes() == original
    assert (m.SITE / 'manifest.json').read_text() == '{"generation":"test"}'


def test_legacy_state_restore(state_files):
    m = state_files
    (m.STATE / 'publisher.json').write_text(json.dumps({'format': 'jobhound-public-state', 'version': 1}))
    (m.STATE / 'state.json.gz').write_bytes(gzip.compress(b'{"jobs":[]}'))
    shutil.copytree(m.SITE, m.STATE / 'site')
    m.restore()
    assert (m.STATE / 'state.json').read_bytes() == b'{"jobs":[]}'


@pytest.mark.parametrize('damage', ['corrupt', 'missing', 'reorder', 'unsafe_path'])
def test_invalid_chunks_preserve_existing_state(state_files, damage):
    m = state_files
    (m.STATE / 'state.json').write_bytes(os.urandom(300))
    m.prepare()
    shutil.copytree(m.PUBLISH, m.STATE, dirs_exist_ok=True)
    marker_path = m.STATE / 'publisher.json'
    marker = json.loads(marker_path.read_text())
    part = m.STATE / marker['parts'][0]['path']
    if damage == 'corrupt':
        part.write_bytes(b'corrupt')
    elif damage == 'missing':
        part.unlink()
    elif damage == 'reorder':
        marker['parts'].reverse()
    else:
        marker['parts'][0]['path'] = '../outside.json.gz'
    marker_path.write_text(json.dumps(marker))
    (m.STATE / 'state.json').write_bytes(b'preserve')
    with pytest.raises((ValueError, FileNotFoundError)):
        m.restore()
    assert (m.STATE / 'state.json').read_bytes() == b'preserve'
