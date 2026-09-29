"""Budget policy."""
def budget_plan(cfg,tier,requested_seconds):
    large=tier=='large_vcycle_deferred'
    # Direct short durations are for isolated local tests, never formal invoke_flow.
    override=requested_seconds!=cfg['total_seconds']
    window=requested_seconds if override else cfg.get('large_total_seconds',requested_seconds) if large else requested_seconds
    return dict(root_tier=tier,large_triggered=large,window_seconds=window,
        configured_large_seconds=cfg.get('large_total_seconds'),small_medium_seconds=cfg['total_seconds'],
        c_max_seconds=cfg['c_seconds'],a_stage_solver_max_seconds=cfg.get('stage_solver_seconds',120),
        scope='C_plus_A_wall_including_model_build; common_preparation_excluded',test_duration_override=override)

def validate_budget_record(result,cfg):
    p=result['budget_plan'];expected=budget_plan(cfg,p['root_tier'],cfg['total_seconds'])
    assert p==expected,'budget policy mismatch or test override leaked into formal run'
    assert result['budget_seconds']==p['window_seconds']
    assert all(s['workers']==1 and s.get('solver_limit_seconds',0)<=120.000001 for s in result['solver_calls'])
    assert result.get('hybrid',{}).get('root_tier')==p['root_tier']
