"""Assignment ordinary."""
import array, hashlib, json, random, signal, time, traceback
from collections import Counter, defaultdict
from pathlib import Path
from runtime_common import sha256
from repair_local import read_request, read_fixed

class Deadline(Exception):pass
PROGRESS=None
from profiles import variant
from solver_hint import install_hint
from atomic_mailbox import Mailbox, candidate_commit, supervise
def progress(**fields):
    if PROGRESS is not None:PROGRESS(dict(kind='progress',emitted_at=time.monotonic(),fields=fields))

def identity(value):
    return hashlib.sha256(json.dumps(value,separators=(',',':'),sort_keys=True).encode()).hexdigest()

def modules():
    import importlib.util,sys
    p=Path(__file__).resolve().parent / 'multilevel.py'
    spec=importlib.util.spec_from_file_location('rq2_v14',p);v=importlib.util.module_from_spec(spec);spec.loader.exec_module(v)
    h=v.load('rq2_h',v.HIERARCHY_MODEL_PATH);s=h.load('rq2_s',h.ANYTIME_MODEL_PATH)
    compact=s.load('rq2_compact',s.COMPACT_MODEL_PATH);a=s.load('rq2_atomic',compact.ATOMIC_MODEL_PATH)
    b=a.load('rq2_base',a.METRICS_MODEL_PATH);f=a.load('rq2_fixed',a.FIXED_MODEL_PATH);parent=f.import_parent()
    estimate=a.estimate_model_size;policy=a.model_policy
    b.build_model=lambda *args,**kwargs:v.build_model(compact,b,*args,**kwargs)
    a.estimate_model_size=lambda inst,sel:v.compressed_estimate(estimate,compact,inst,sel)
    a.model_policy=lambda est:h.compact_actual_policy(policy,est)
    sys.modules['hierarchy_model_anytime_model']=s
    return v,h,s,a,b,parent

def group_summary(req,member,fixed):
    groups=defaultdict(list)
    for n,g in enumerate(member):groups[(g,req.labels[n])].append(n)
    stats={str(k):(len(ns),sum(req.node_weights[n] for n in ns)) for k,ns in groups.items()}
    return dict(membership_sha256=identity(member),groups=len(groups),movable_groups=sum(not any(n in fixed for n in ns) for ns in groups.values()),sizes_resources_sha256=identity(stats),sizes_resources=stats)

def shuffle(req,member,fixed,seed):
    buckets=defaultdict(list)
    for n in range(req.n):
        if n not in fixed:
            w=req.node_weights[n];key=(req.labels[n],tuple(w) if isinstance(w,(list,tuple)) else w)
            buckets[key].append(n)
    out=list(member);rng=random.Random(seed)
    for ns in buckets.values():
        labels=[member[n] for n in ns];rng.shuffle(labels)
        for n,label in zip(ns,labels):out[n]=label
    assert all(out[n]==member[n] for n in fixed)
    assert group_summary(req,out,fixed)['sizes_resources']==group_summary(req,member,fixed)['sizes_resources']
    changed=sum(x!=y for x,y in zip(member,out))
    return out,dict(changed_members=changed,movable_nodes=req.n-len(fixed),changed_member_fraction=changed/max(1,req.n-len(fixed)),seed=seed,buckets=len(buckets),informative=changed>0)

def sequential(base,instance,deadline,consider,progress):
    """Full-graph equivalent atomic metric evaluation; fixed membership and anchor."""
    labels=list(instance['current_labels']);rounds=0;tests=0;moves=[]
    progress.update(rounds=0,candidate_evaluations=0,moves=moves)
    anchor=instance['incumbent_metrics'];coef=base.score_coefficients(anchor)
    key=lambda m:base.policy_key(m,'mfs_balanced')
    while time.monotonic()<deadline:
        rounds+=1;progress['rounds']=rounds;changed=False
        for block,domain in enumerate(instance['domains']):
            if len(domain)<=1:continue
            old=labels[block];best=old;best_m=base.metrics(instance,labels,coef)
            for target in domain:
                if target==old:continue
                if time.monotonic()>=deadline:raise Deadline()
                labels[block]=target
                try:m=base.metrics(instance,labels,coef)
                finally:labels[block]=old
                tests+=1;progress['candidate_evaluations']=tests
                if tests%64==0:globals()['progress'](details=dict(progress))
                safe=(m['violation']==0 and m['capacity_overflow']==0 and all(100*m[k]<=105*anchor[k] for k in base.STANDARD_METRICS))
                if safe and key(m)<key(best_m):best,best_m=target,m
            if best!=old:
                # Commit one whole subcommunity only after all target evaluations.
                labels[block]=best;consider(base.expand_labels(instance,labels))
                moves.append(dict(block=block,source=old,target=best,metrics=best_m));changed=True
                globals()['progress'](details=dict(progress))
        if not changed:return dict(rounds=rounds,candidate_evaluations=tests,moves=moves,stop='no_strict_improvement')
    raise Deadline()

def run(request_path,fixed_path,membership_path,arm,task_id,seconds=120):
    cfg=variant(arm);mode=cfg['mode']
    prep=time.monotonic();v,h,s,a,b,parent=modules()
    req=read_request(request_path);fixed=read_fixed(fixed_path,req.n)
    member=array.array('I');member.frombytes(Path(membership_path).read_bytes());member=list(member)
    assert len(member)==req.n
    for offset,n in enumerate(sorted(fixed)):
        assert req.labels[n]==fixed[n]
        # Same Fixed singleton handling as fixed_model, computed once before edits.
    next_id=max(member,default=-1)+1
    for offset,n in enumerate(sorted(fixed)):member[n]=next_id+offset
    raw_build=parent.build_instance
    def build(r,m,t):
        x=raw_build(r,m,t);x['fixed_blocks']=sorted({x['block_of_node'][n] for n in fixed})
        for n,label in fixed.items():x['domains'][x['block_of_node'][n]]=[label]
        return x
    legacy=build(req,member,task_id);atomic=a.build_atomic_instance(req,member,task_id,legacy)
    original_eval=parent.evaluate;anchor=original_eval(req,req.labels)
    assert anchor['violation']==0 and anchor['capacity_overflow']==0,'INPUT_NOT_NATURALLY_FEASIBLE'
    coef=b.score_coefficients(anchor);anchor=dict(anchor,mfs_balanced_score=b.score_value(anchor,coef))
    exact_atomic=b.metrics(atomic,atomic['current_labels'],coef)
    assert all(exact_atomic[k]==anchor[k] for k in (*b.STANDARD_METRICS,'violation','capacity_overflow'))
    atomic['incumbent_metrics']=anchor
    common=group_summary(req,member,fixed);group_before=common
    prep_seconds=time.monotonic()-prep
    best=(list(req.labels),anchor);accepted=[];solver_rows=[];models=[];searching=None;model_context={};c_candidate=None;solver_budget_scale=1.;hybrid=dict(enabled=mode=='H',c_budget_seconds=cfg['c_seconds'],hints=[],phase='pending')
    def consider(labels):
        nonlocal best
        measured=original_eval(req,labels)
        measured=dict(measured,mfs_balanced_score=b.score_value(measured,coef))
        safe=not measured['violation'] and not measured['capacity_overflow'] and all(labels[n]==lab for n,lab in fixed.items()) and all(100*measured[k]<=105*anchor[k] for k in b.STANDARD_METRICS)
        if safe and b.policy_key(measured,'mfs_balanced')<b.policy_key(best[1],'mfs_balanced'):
            candidate=list(labels);digest=identity(candidate)
            events=accepted+[dict(elapsed=time.monotonic()-started,metrics=measured,assignment_sha256=digest)]
            changed=[n for n,(x,y) in enumerate(zip(req.labels,candidate)) if x!=y]
            fields=dict(output_metrics=measured,output_assignment_sha256=digest,strict_improvement=True,
                changed_nodes=len(changed),changed_groups=len({atomic['block_of_node'][n] for n in changed}),
                sparse_changes=[[n,req.labels[n],candidate[n]] for n in changed],accepted_candidates=events)
            publication=(PROGRESS.prepare(dict(kind='progress',emitted_at=time.monotonic(),fields=fields))
                         if PROGRESS is not None else lambda:None)
            # Encoding is interruptible. Only rename + local best replacement are atomic.
            # A pending C alarm is delivered AFTER both sides have the same candidate.
            with candidate_commit():
                publication()
                best=(candidate,measured)
                accepted[:]=events
        return measured
    def tap(r,labels):
        measured=original_eval(r,labels)
        if r is req and len(labels)==req.n:consider(labels)
        return measured
    parent.evaluate=tap
    # Freeze normalization in every hierarchy level, including continuation.
    b.score_coefficients=lambda ignored:dict(coef)
    raw_model=b.build_model
    def model(*args,**kwargs):
        if time.monotonic()>=deadline:raise Deadline()
        start=time.monotonic();out=raw_model(*args,**kwargs)
        models.append(dict(seconds=time.monotonic()-start,variables=len(out[0].proto.variables),constraints=len(out[0].proto.constraints)))
        model_context[id(out[0])]=(args[0],list(args[1]),out[1],out[2])
        progress(models=models)
        return out
    b.build_model=model
    from ortools.sat.python import cp_model
    raw_solve=cp_model.CpSolver.solve
    def solve(solver,mod,*args,**kwargs):
        nonlocal searching
        remaining=deadline-time.monotonic()
        if remaining<=0:raise Deadline()
        solver.parameters.max_time_in_seconds=min((solver.parameters.max_time_in_seconds or remaining)*solver_budget_scale,remaining)
        solver.parameters.num_search_workers=1
        instance,selected,variables,expressions=model_context[id(mod)]
        if c_candidate is not None:
            hint_start=time.monotonic()
            hint=install_hint(mod,instance,selected,variables,c_candidate,b,coef)
            hint['seconds']=time.monotonic()-hint_start;hybrid['hints'].append(hint)
            progress(hybrid=hybrid)
            remaining=deadline-time.monotonic()
            if remaining<=0:raise Deadline()
            solver.parameters.max_time_in_seconds=min(solver.parameters.max_time_in_seconds,remaining)
        searching=solver;start=time.monotonic()
        progress(active_solver=dict(started=start,allocated_seconds=remaining,solver_limit_seconds=solver.parameters.max_time_in_seconds,budget_scale=solver_budget_scale,workers=1))
        class SafeIncumbent(cp_model.CpSolverSolutionCallback):
            def on_solution_callback(self):
                labels=list(instance['current_labels'])
                for block in selected:
                    chosen=[label for label in instance['domains'][block] if self.value(variables[block,label])]
                    if len(chosen)!=1:raise RuntimeError('invalid callback assignment')
                    labels[block]=chosen[0]
                measured=b.metrics(instance,labels,coef)
                if any(measured[name]!=int(self.value(expr)) for name,expr in expressions.items()):raise RuntimeError('callback model metric mismatch')
                consider(b.expand_labels(instance,labels))
                progress(solver_last_incumbent=dict(objective=self.objective_value,bound=self.best_objective_bound,elapsed=time.monotonic()-started))
        try:
            assert not args and not kwargs,'unexpected solver callback'
            status=raw_solve(solver,mod,solution_callback=SafeIncumbent())
            if status==cp_model.MODEL_INVALID:raise RuntimeError('CP-SAT MODEL_INVALID: '+solver.response_stats())
            return status
        finally:
            searching=None;solver_rows.append(dict(seconds=time.monotonic()-start,allocated_seconds=remaining,solver_limit_seconds=solver.parameters.max_time_in_seconds,budget_scale=solver_budget_scale,workers=1))
            model_context.pop(id(mod),None)
            progress(active_solver=None,solver_calls=solver_rows)
    def alarm(*_):
        if searching is not None:searching.stop_search()
        else:raise Deadline()
    bootstrap=dict(schema='RQ2_MODULE_V1',arm=arm,task_id=task_id,state='OK_FINAL_ZERO',error=None,stop_reason='hard_total_deadline',
        request_sha256=sha256(request_path),fixed_sha256=sha256(fixed_path),source_membership_sha256=sha256(membership_path),
        input_assignment_sha256=identity(req.labels),output_assignment_sha256=identity(req.labels),common_groups=common,groups=common,
        shuffle=dict(informative=False,changed_members=0,changed_member_fraction=0,stage='not_committed') if mode=='B' else {},
        coefficients=coef,quality_weights=[.5,.25,.25],input_metrics=anchor,output_metrics=anchor,strict_improvement=False,
        changed_nodes=0,changed_groups=0,sparse_changes=[],fixed_mismatches=0,capacity_overflow=0,violation=0,
        common_preparation_seconds=time.monotonic()-prep,processing_seconds=0,budget_seconds=seconds,grouping_seconds=0,
        models=[],solver_calls=[],accepted_candidates=[],details={},hybrid=hybrid,output_labels=list(req.labels))
    prep_seconds=bootstrap['common_preparation_seconds']
    started=time.monotonic();deadline=started+seconds;reason='complete';detail={};shuffled={};error=None
    if PROGRESS is not None:PROGRESS(dict(kind='ready',started=started,deadline=deadline,result=bootstrap))
    cp_model.CpSolver.solve=solve;old_handler=signal.signal(signal.SIGALRM,alarm)
    try:
        signal.setitimer(signal.ITIMER_REAL,seconds)
        if mode=='B':
            seed=int.from_bytes(hashlib.sha256(('RQ2:'+task_id).encode()).digest()[:8],'little')
            member,shuffled=shuffle(req,member,fixed,seed)
            legacy=build(req,member,task_id);atomic=a.build_atomic_instance(req,member,task_id,legacy)
            atomic['incumbent_metrics']=anchor
        grouped=time.monotonic();group_after=group_summary(req,member,fixed)
        progress(groups=group_after,shuffle=shuffled if mode=='B' else {},grouping_seconds=grouped-started)
        if mode=='H':
            c_started=time.monotonic();c_deadline=min(deadline,c_started+cfg['c_seconds'])
            hybrid.update(phase='C',c_started_seconds=c_started-started,c_budget_seconds=cfg['c_seconds'])
            progress(hybrid=hybrid)
            cdetail={};c_stop='complete'
            # A phase alarm interrupts long Python metric evaluation as well as loops.
            # The independent supervisor remains the hard TOTAL-window authority.
            signal.setitimer(signal.ITIMER_REAL,max(.000001,c_deadline-time.monotonic()))
            try:cdetail=sequential(b,atomic,c_deadline,consider,cdetail)
            except Deadline:c_stop='prefix_deadline'
            finally:signal.setitimer(signal.ITIMER_REAL,max(.000001,deadline-time.monotonic()))
            c_candidate=list(best[0])
            hybrid.update(phase='A',c_seconds=time.monotonic()-c_started,c_stop=c_stop,
                c_metrics=dict(best[1]),c_assignment_sha256=identity(c_candidate),c_improved=c_candidate!=req.labels,
                c_details=cdetail,a_start_seconds=time.monotonic()-started)
            progress(hybrid=hybrid)
            if not hybrid['c_improved']:c_candidate=None
        if mode in ('A','B','H'):
            parent.build_instance=lambda *args:legacy
            raw_atomic=a.build_atomic_instance;a.build_atomic_instance=lambda *args:atomic
            if cfg['total_seconds']>120:
                selected=[block for block,domain in enumerate(atomic['domains']) if len(domain)>1]
                tier=a.model_policy(a.estimate_model_size(atomic,selected))['tier']
                # Large already scales through its existing stage-seconds setting.
                # Compact/medium fixed solver slices need explicit proportional scaling.
                solver_budget_scale=1. if tier=='large_vcycle_deferred' else cfg['total_seconds']/120.
                hybrid.update(root_tier=tier,solver_budget_scale=solver_budget_scale)
                progress(hybrid=hybrid)
            try:
                labels,detail=v.optimize(h,s,a,b,parent,req,member,task_id)
                consider(labels)
            finally:a.build_atomic_instance=raw_atomic
        else:detail=sequential(b,atomic,deadline,consider,detail)
    except Deadline:reason='total_deadline'
    except Exception:
        reason='process_failure';error=traceback.format_exc();best=(list(req.labels),anchor)
        detail['exception_diagnostics']=dict(hyperedge_weight_types=dict(Counter(type(w).__name__ for w in atomic['hyperedge_weights'].values())),python=__import__('sys').version)
    finally:
        signal.setitimer(signal.ITIMER_REAL,0);signal.signal(signal.SIGALRM,old_handler);cp_model.CpSolver.solve=raw_solve
    processing=time.monotonic()-started
    if mode=='H':hybrid.update(phase='finished',a_seconds=max(0,processing-hybrid.get('a_start_seconds',processing)))
    if reason=='complete' and processing>=seconds:reason='total_deadline'
    labels,metrics=best
    checked=original_eval(req,labels)
    assert all(checked[k]==metrics[k] for k in b.STANDARD_METRICS)
    assert not checked['violation'] and not checked['capacity_overflow'] and all(labels[n]==lab for n,lab in fixed.items())
    assert all(100*checked[k]<=105*anchor[k] for k in b.STANDARD_METRICS)
    changed=[n for n,(x,y) in enumerate(zip(req.labels,labels)) if x!=y]
    final_group=group_summary(req,member,fixed)
    return dict(schema='RQ2_MODULE_V1',arm=arm,task_id=task_id,state='PROCESS_FAILED' if error else 'OK_FINAL_ZERO',error=error,stop_reason=reason,
        request_sha256=sha256(request_path),fixed_sha256=sha256(fixed_path),source_membership_sha256=sha256(membership_path),input_assignment_sha256=identity(req.labels),output_assignment_sha256=identity(labels),
        common_groups=common,groups=final_group,shuffle=shuffled,coefficients=coef,quality_weights=[.5,.25,.25],input_metrics=anchor,output_metrics=metrics,strict_improvement=bool(changed),
        changed_nodes=len(changed),changed_groups=len({atomic['block_of_node'][n] for n in changed}),sparse_changes=[[n,req.labels[n],labels[n]] for n in changed],fixed_mismatches=0,capacity_overflow=checked['capacity_overflow'],violation=checked['violation'],
        common_preparation_seconds=prep_seconds,processing_seconds=processing,budget_seconds=seconds,grouping_seconds=locals().get('grouped',started)-started,
        models=models,solver_calls=solver_rows,accepted_candidates=accepted,details=detail,hybrid=hybrid,output_labels=labels)

def worker(folder,args):
    global PROGRESS
    PROGRESS=Mailbox(folder)
    try:PROGRESS(dict(kind='done',result=run(*args)))
    except BaseException:PROGRESS(dict(kind='error',error=traceback.format_exc()))


def bounded_run(*args):
    """Hard deadline with atomic complete-message publication and exact recheck."""
    from repair_local import evaluate
    state,deadline,started,done,failure,stopped,worker_exitcode=supervise(worker,args)
    if done is not None:
        done['deadline_enforcement']='supervised_worker';done['candidate_publication']='atomic_mailbox_v1';return done
    if state is None:raise RuntimeError(failure or 'worker failed before common preparation completed')
    state['processing_seconds']=(stopped or time.monotonic())-started
    state['deadline_enforcement']='supervised_worker'
    state['candidate_publication']='atomic_mailbox_v1'
    state['worker_exitcode']=worker_exitcode
    active=state.pop('active_solver',None)
    if active is not None:
        state['solver_calls'].append(dict(seconds=max(0,(stopped or time.monotonic())-active['started']),allocated_seconds=active['allocated_seconds'],solver_limit_seconds=active.get('solver_limit_seconds'),budget_scale=active.get('budget_scale'),workers=1,stopped_by_supervisor=True))
    labels=list(state['output_labels'])
    for n,before,after in state['sparse_changes']:
        assert labels[n]==before;labels[n]=after
    if stopped is None or failure is not None:
        state.update(state='PROCESS_FAILED',error=failure or 'worker exited before deadline',stop_reason='process_failure')
        labels=list(state['output_labels']);state.update(output_metrics=state['input_metrics'],output_assignment_sha256=state['input_assignment_sha256'],strict_improvement=False,changed_nodes=0,changed_groups=0,sparse_changes=[])
    # Always validate in a surviving process, after the timed optimization window.
    req=read_request(args[0]);fixed=read_fixed(args[1],req.n);measured=evaluate(req,labels)
    assert not measured['violation'] and not measured['capacity_overflow'] and all(labels[n]==lab for n,lab in fixed.items())
    for k in ('connectivity','cut','hop_numerator'):
        assert measured[k]==state['output_metrics'][k] and 100*measured[k]<=105*state['input_metrics'][k]
    assert sha256(args[0])==state['request_sha256'] and sha256(args[1])==state['fixed_sha256']
    state['output_labels']=labels
    assert identity(labels)==state['output_assignment_sha256']
    return state
