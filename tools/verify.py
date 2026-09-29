"""Verify released source checksums and optionally installed circuit caches."""
import argparse
import hashlib
import json
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--caches', action='store_true')
    args = parser.parse_args()
    manifest = json.loads((ROOT / 'MANIFEST.json').read_text())
    for row in manifest['files']:
        path = ROOT / row['path']
        assert path.is_file(), row['path']
        raw = path.read_bytes()
        assert len(raw) == row['bytes'], row['path']
        assert hashlib.sha256(raw).hexdigest() == row['sha256'], row['path']
    print('SOURCE_PASS', len(manifest['files']), 'files')
    if args.caches:
        rows = json.loads((ROOT / 'config/cache_lock.json').read_text())
        for row in rows:
            path = ROOT / 'data/caches' / row['view'] / row['case'] / 'pair_louvain.membership.u32'
            raw = path.read_bytes()
            meta = json.loads(path.with_suffix('.u32.json').read_text())
            assert hashlib.sha256(raw).hexdigest() == row['sha256'] == meta['output_sha256']
            assert len(raw) == 4 * meta['nodes']
            assert meta['input_sha256'] == row['input_sha256'] and meta['view'] == row['view']
            assert all(value[0] < meta['nodes'] for value in struct.iter_unpack('<I', raw))
        print('CACHE_PASS', len(rows), 'circuit views')

if __name__ == '__main__':
    main()
