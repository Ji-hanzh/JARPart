"""Component 2: sequential subcommunity refinement followed by joint CP-SAT.

The actual search, budgets, fixed/constraint checks and rollback behavior are
delegated to the ordinary or Large refinement engine.
"""
import sys
from pathlib import Path
from ._implementation import engine,read_inputs

def refine(request_path,fixed_path,membership_path,profile='ordinary120',tag='jarpart'):
    if sys.platform!='linux':raise RuntimeError('Use Linux/WSL for the POSIX deadline runtime')
    read_inputs(request_path,fixed_path,membership_path)
    arm={'ordinary120':'H30_T120','large480':'H30_L480'}[profile]
    return engine(profile).bounded_run(Path(request_path),Path(fixed_path),Path(membership_path),arm,tag,120)
