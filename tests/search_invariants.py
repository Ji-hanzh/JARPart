from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"core/_engine"))
"""Independent invariants: C incumbent transfer, joint swaps, deadlines, hints, bounded runtime."""
import array,copy,signal,tempfile,time
from pathlib import Path
from unittest.mock import patch
import assignment_large as e
from solver_hint import project,install_hint
from profiles import ARMS,variant
def fixture(folder,capacity=3):
    req=folder/'x.request.tsv';fix=folder/'x.fixed.tsv';mem=folder/'m.u32'
    req.write_text(f'ITER240_REQUEST_V1 4 2 {capacity} 1 2 2\nD 0 0 1\nD 1 1 0\nN 0 1 0\nN 1 1 0\nN 2 1 1\nN 3 1 1\nP 0 3 5\nP 1 2 5\nH 5 0 1 3\nH 5 1 1 2\n')
    fix.write_text('ITER247_FIXED_V1 4 2\n1 0\n3 1\n');mem.write_bytes(array.array('I',[0,1,2,3]).tobytes())
    return req,fix,mem
def tests():
    with tempfile.TemporaryDirectory(prefix='ca-test-') as td:
        f=fixture(Path(td));original=e.modules
        # All published policy branches work with unchanged original anchor.
        for arm in ARMS:
            r=e.bounded_run(*f,arm,'allpolicies',1)
            assert r['state']=='OK_FINAL_ZERO',r.get('error')
            assert r['output_metrics']['cut']==0 and not r['fixed_mismatches'],arm
            assert r['input_assignment_sha256']==e.identity([0,0,1,1])
            assert r['quality_weights']==[.5,.25,.25] and r['budget_seconds']==1
            if variant(arm)['total_seconds']>120 and variant(arm)['mode']!='C':
                assert r['hybrid']['solver_budget_scale']==variant(arm)['total_seconds']/120
                assert all(s.get('solver_limit_seconds',0)<=1 for s in r['solver_calls'])
        def stalled():
            values=original()
            def slow(*args):
                signal.setitimer(signal.ITIMER_REAL,0);time.sleep(3);return list(args[-3].labels),{}
            values[0].optimize=slow;return values
        def large_route():
            values=original();old_policy=values[3].model_policy
            values[3].model_policy=lambda estimate:dict(old_policy(estimate),tier='large_vcycle_deferred')
            values[0].optimize=lambda *args:(list(args[-3].labels),{})
            return values
        with patch.object(e,'modules',large_route):r=e.bounded_run(*f,'A120','large_not_double_scaled',1)
        assert r['state']=='OK_FINAL_ZERO' and r['hybrid'].get('solver_budget_scale',1)==1
        with patch.object(e,'modules',stalled):r=e.bounded_run(*f,'H30_T120','C_then_stalled_A',.4)
        assert r['state']=='OK_FINAL_ZERO' and r['output_metrics']['cut']==0,r
        assert r['hybrid']['c_improved'] and r['stop_reason']=='hard_total_deadline' and r['processing_seconds']<1.5
        # A technical failure after C must still fail closed to original checkpoint.
        def broken():
            values=original()
            def fail(*args):raise RuntimeError('injected A fault after C')
            values[0].optimize=fail;return values
        with patch.object(e,'modules',broken):r=e.bounded_run(*f,'H30_T120','failure_after_C',1)
        assert r['state']=='PROCESS_FAILED' and r['output_labels']==[0,0,1,1] and r['hybrid']['c_improved']
        # C phase overruns its cooperative loop: phase alarm still hands remaining budget to A.
        real_variant=e.variant
        def short(arm):return dict(mode='H',total_seconds=1,c_seconds=.05)
        def slow_c(*args):time.sleep(3)
        with patch.object(e,'variant',short),patch.object(e,'sequential',slow_c):r=e.bounded_run(*f,'H30_T120','phase_alarm',1)
        assert r['state']=='OK_FINAL_ZERO' and r['hybrid']['c_stop']=='prefix_deadline'
        assert .025<r['hybrid']['c_seconds']<.3 and r['output_metrics']['cut']==0
        f=fixture(Path(td),2)
        c=e.bounded_run(*f,'C','blocked_C',1);h=e.bounded_run(*f,'H30_T120','joint_swap',1)
        assert c['output_metrics']['cut']==10 and h['output_metrics']['cut']==0
        assert not h['hybrid']['c_improved'],'joint stage must remain capable of compound swaps'
        for key in ('input_metrics','common_groups','coefficients','input_assignment_sha256'):assert c[key]==h[key]
    inst=dict(current_labels=[0,0],block_of_node=[0,0,1],domains=[[0,1],[0,1]])
    assert project(inst,[0,1],[1,0,1])[1]=='merged_block_conflict'
    assert project(inst,[0],[1,1,1])[1]=='outside_corridor'
    assert project(inst,[0,1],[1,1,1])==([1,1],'compatible')
    from ortools.sat.python import cp_model
    model=cp_model.CpModel();variables={(b,l):model.new_bool_var(f'x{b}_{l}') for b in range(2) for l in (0,1)}
    for b in range(2):model.add_exactly_one(variables[b,0],variables[b,1]);model.add_hint(variables[b,0],1);model.add_hint(variables[b,1],0)
    class Base:
        STANDARD_METRICS=('cut',)
        @staticmethod
        def metrics(instance,labels,coef):return dict(cut=2-sum(labels),violation=0,capacity_overflow=0,mfs_balanced_score=2-sum(labels))
        @staticmethod
        def policy_key(m,mode):return (m['cut'],)
    inst['incumbent_metrics']={'cut':2}
    before_constraints=[str(c) for c in model.proto.constraints]
    h=install_hint(model,inst,[0,1],variables,[1,1,1],Base,{})
    assert h['applied'] and len(model.proto.solution_hint.vars)==4 and [str(c) for c in model.proto.constraints]==before_constraints
    assert not install_hint(model,inst,[0,1],variables,[1,1,1],Base,{})['applied']
    assert not model.validate()
    print('CA_SELF_TEST_PASS all2 C_retained A_failure_rollback phase_alarm total_watchdog joint_swap immutable_anchor compatible_incompatible_hints')
if __name__=='__main__':tests()
