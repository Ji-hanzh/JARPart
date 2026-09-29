"""Install the exact returned cache asset into data/caches; never rebuild it."""
import argparse,hashlib,json,shutil,tarfile,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
EXPECTED_SHA='c2c6a883b06daafea9dbf6faf4de143cf16cd004297801d7b78bd1417a648bd9'

def install(archive):
    if hashlib.sha256(archive.read_bytes()).hexdigest()!=EXPECTED_SHA:raise ValueError('Cache archive SHA mismatch')
    target=ROOT/'data/caches'
    if target.exists():raise FileExistsError('Cache directory already exists; use verify.py --caches')
    target.parent.mkdir(parents=True,exist_ok=True)
    lock=json.loads((ROOT/'config/cache_lock.json').read_text())
    wanted={'caches/MANIFEST.json'}
    for row in lock:
        name=f"caches/{row['view']}/{row['case']}/pair_louvain.membership.u32"
        wanted.update((name,name+'.json'))
    with tempfile.TemporaryDirectory(prefix='cache-install-',dir=target.parent) as td:
        stage=Path(td)
        with tarfile.open(archive,'r:gz') as t:
            members=t.getmembers()
            if len(members)!=len(wanted) or {m.name for m in members}!=wanted or not all(m.isfile() for m in members):
                raise ValueError('Unexpected cache archive contents')
            manifest=json.load(t.extractfile('caches/MANIFEST.json'))
            expected={r['path']:r for r in manifest['files']}
            for m in members:
                data=t.extractfile(m).read()
                if m.name in expected:
                    row=expected[m.name]
                    if len(data)!=row['bytes'] or hashlib.sha256(data).hexdigest()!=row['sha256']:raise ValueError(m.name)
                dest=stage/m.name;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(data)
        for row in lock:
            p=stage/'caches'/row['view']/row['case']/'pair_louvain.membership.u32'
            meta=json.loads(p.with_suffix('.u32.json').read_text())
            if hashlib.sha256(p.read_bytes()).hexdigest()!=row['sha256'] or meta['input_sha256']!=row['input_sha256'] or meta['view']!=row['view'] or p.stat().st_size!=4*meta['nodes']:
                raise ValueError('Wrong cache identity: '+str(p))
        (stage/'caches').rename(target)
    print('CACHE_INSTALLED 30 circuits / 60 views:',target)
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('archive',type=Path);a=p.parse_args();install(a.archive)
