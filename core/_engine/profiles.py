"""Profiles."""
ARMS = {'H30_L480': {'mode': 'H', 'total_seconds': 120, 'c_seconds': 30, 'large_total_seconds': 480, 'stage_solver_seconds': 120}}
def variant(arm):
    if arm in ('A','B','C','A120','H30_T120'):
        return dict(mode='H' if arm=='H30_T120' else arm[0],total_seconds=120,c_seconds=30 if arm=='H30_T120' else 120 if arm=='C' else 0)
    return dict(ARMS[arm])
