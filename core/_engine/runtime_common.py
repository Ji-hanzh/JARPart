"""Runtime common."""
import hashlib
import json
import os
import tempfile
from pathlib import Path

PKG=Path(__file__).resolve().parents[1]
def load_json(p):return json.loads(Path(p).read_text(encoding='utf-8'))
def sha256(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for block in iter(lambda:f.read(1<<20),b''):h.update(block)
    return h.hexdigest()
def atomic_json(p,value):
    p=Path(p);p.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile('w',encoding='utf-8',dir=p.parent,delete=False) as f:
        json.dump(value,f,indent=2,sort_keys=True);f.write('\n');tmp=f.name
    os.replace(tmp,p)
def assignment_hash(labels):
    value=1469598103934665603
    for x in labels:value=((value^(x+1))*1099511628211)&((1<<64)-1)
    return value
