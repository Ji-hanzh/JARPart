"""Load the numerical implementation behind public names."""
import importlib.util
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
FROZEN=ROOT/'core/_engine'
SCRIPTS=FROZEN
if str(SCRIPTS) not in sys.path:sys.path.insert(0,str(SCRIPTS))

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module
    spec.loader.exec_module(module)
    return module

def engine(profile):
    paths={'ordinary120':'assignment_ordinary.py','large480':'assignment_large.py'}
    if profile not in paths:raise ValueError('Unknown profile: '+profile)
    return load('jarpart_'+profile,FROZEN/paths[profile])

def read_inputs(request_path,fixed_path,membership_path):
    from repair_local import read_request,read_fixed
    from repair_joint import read_membership
    request=read_request(Path(request_path))
    fixed=read_fixed(Path(fixed_path),request.n)
    if any(not 0<=n<request.n or not 0<=label<request.k for n,label in fixed.items()):
        raise ValueError('Invalid fixed node or FPGA')
    if any(request.labels[n]!=label for n,label in fixed.items()):
        raise ValueError('Checkpoint does not preserve fixed assignments')
    membership=read_membership(Path(membership_path),request.n)
    return request,fixed,membership
