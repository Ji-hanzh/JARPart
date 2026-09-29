"""Auxiliary component: bounded P2 followed, if needed, by P2-JOINT.

Returns a proposal and its diagnostics. The external host must retain the
original checkpoint and apply its final acceptance gate after quality refinement.
"""
import json,tempfile
from pathlib import Path
from ._implementation import read_inputs

def propose(request_path,fixed_path,membership_path,table='table4'):
    read_inputs(request_path,fixed_path,membership_path)
    from recovery import run
    with tempfile.TemporaryDirectory(prefix='jarpart-recovery-') as folder:
        output=Path(folder)/'proposal.tsv'
        run(Path(request_path),Path(fixed_path),output,'jarpart_recovery',30.,60.,Path(membership_path),table)
        labels=list(map(int,output.read_text().splitlines()[1:]))
        result=json.loads(output.with_suffix('.tsv.json').read_text())
    return dict(result,output_labels=labels,output_scope='recovery_proposal_requires_host_final_acceptance')
