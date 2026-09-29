"""Component 1: community × current FPGA, with fixed singleton protection.

Input: a current-level request, fixed assignments and community membership.
Output: the atomic instance, including resource/pair/net aggregates.
"""
from ._implementation import engine,read_inputs,FROZEN,load

def construct(request_path,fixed_path,membership_path):
    request,fixed,membership=read_inputs(request_path,fixed_path,membership_path)
    # This is the same fixed-singleton preparation as the C+A engine.
    next_id=max(membership,default=-1)+1
    for offset,node in enumerate(sorted(fixed)):membership[node]=next_id+offset
    _,_,_,atomic,_,parent=engine('ordinary120').modules()
    legacy=parent.build_instance(request,membership,'subcommunity')
    legacy['fixed_blocks']=sorted({legacy['block_of_node'][n] for n in fixed})
    for node,label in fixed.items():legacy['domains'][legacy['block_of_node'][node]]=[label]
    return atomic.build_atomic_instance(request,membership,'subcommunity',legacy)

def detect(request):
    """Standard seed-250 Louvain for a supplied current-level graph.

    The special full-circuit bitcoin R100 cache is provided as a release asset;
    this function intentionally does not claim to regenerate that variant.
    """
    module=load('jarpart_pair_louvain',FROZEN/'community.py')
    return module.pair_louvain_membership(request)
