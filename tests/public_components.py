"""Check public entrypoints against the preserved algorithms and exact metrics."""
import copy,json,struct,sys,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from core.subcommunity import construct,detect
from core.joint_assignment import refine
from core.feasibility_recovery import propose
from core._implementation import read_inputs
from repair_local import evaluate

def main():
    paths=tuple(ROOT/'examples'/('swap.'+s) for s in ('request.tsv','fixed.tsv','membership.u32'))
    request,fixed,members=read_inputs(*paths)
    instance=construct(*paths)
    assert instance['block_nodes']==[[0],[1],[2],[3]]
    assert instance['block_sizes']==[1]*4 and instance['fixed_blocks']==[1,3]
    assert instance['domains'][1]==[0] and instance['domains'][3]==[1]
    membership,meta=detect(request)
    assert len(membership)==4 and meta['seed']==250
    for profile in ('ordinary120','large480'):
        result=refine(*paths,profile)
        assert result['state']=='OK_FINAL_ZERO',result.get('error')
        checked=evaluate(request,result['output_labels'])
        assert checked['cut']==0 and checked['violation']==0 and checked['capacity_overflow']==0
        assert all(result['output_labels'][n]==label for n,label in fixed.items())
        assert result['input_metrics']['cut']==10
        assert result['quality_weights']==[.5,.25,.25]
    with tempfile.TemporaryDirectory() as folder:
        f=Path(folder);req=f/'r.tsv';fix=f/'f.tsv';mem=f/'m.u32'
        # Fixed vertices sharing a detected community must remain separate units.
        mem.write_bytes(struct.pack('<4I',0,0,0,0))
        grouped=construct(paths[0],paths[1],mem)
        assert len(grouped['block_nodes'])==4 and grouped['fixed_blocks']==[1,3]
        req.write_text('ITER240_REQUEST_V1 3 4 3 1 2 1\nD 0 0 1 2 3\nD 1 1 0 1 2\nD 2 2 1 0 1\nD 3 3 2 1 0\nN 0 1 0\nN 1 1 2\nN 2 1 3\nP 0 1 1\nP 1 2 1\nH 1 0 2 1 2\n')
        fix.write_text('ITER247_FIXED_V1 3 1\n0 0\n');mem.write_bytes(struct.pack('<3I',7,7,8))
        result=propose(req,fix,mem,'table5')
        assert result['accepted_strict_zero'] and result['output_labels'][0]==0
        r,_,_=read_inputs(req,fix,mem);m=evaluate(r,result['output_labels'])
        assert not m['violation'] and not m['capacity_overflow']
        assert result['commit_authority']=='CXX_FULL_GRAPH_EXACT_GATE'
        assert result['output_scope']=='recovery_proposal_requires_host_final_acceptance'
        mem.write_bytes(b'bad')
        try:construct(req,fix,mem)
        except RuntimeError:pass
        else:raise AssertionError('Mismatched membership accepted')
    print('PUBLIC_COMPONENTS_PASS construction fixed_singletons pair_louvain both_engines joint_swap recovery_proposal bad_cache_rejected')
if __name__=='__main__':main()
