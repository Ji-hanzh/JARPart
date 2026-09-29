"""Solver hint."""
def project(instance,selected,node_labels):
    assignment=[None]*len(instance['current_labels'])
    if len(instance['block_of_node'])!=len(node_labels):return None,'node_count'
    for n,block in enumerate(instance['block_of_node']):
        label=node_labels[n]
        if assignment[block] is not None and assignment[block]!=label:return None,'merged_block_conflict'
        assignment[block]=label
    chosen=set(selected)
    for b,label in enumerate(assignment):
        if label is None or label not in instance['domains'][b]:return None,'domain'
        if b not in chosen and label!=instance['current_labels'][b]:return None,'outside_corridor'
    return assignment,'compatible'

def install_hint(model,instance,selected,variables,node_labels,base,coef):
    labels,reason=project(instance,selected,node_labels)
    if labels is None:return dict(applied=False,reason=reason)
    m=base.metrics(instance,labels,coef)
    if m['violation'] or m['capacity_overflow']:return dict(applied=False,reason='model_graph_infeasible')
    anchor=instance['incumbent_metrics']
    if any(100*m[k]>105*anchor[k] for k in base.STANDARD_METRICS):return dict(applied=False,reason='model_anchor_guard')
    # Do not replace a known better existing assignment hint.
    hints=dict(zip(model.proto.solution_hint.vars,model.proto.solution_hint.values))
    previous=list(instance['current_labels']);complete=True
    for b in selected:
        hits=[l for l in instance['domains'][b] if hints.get(variables[b,l].index)==1]
        if len(hits)!=1:complete=False;break
        previous[b]=hits[0]
    if complete:
        old=base.metrics(instance,previous,coef)
        if not old['violation'] and not old['capacity_overflow'] and base.policy_key(old,'mfs_balanced')<=base.policy_key(m,'mfs_balanced'):
            return dict(applied=False,reason='existing_hint_at_least_as_good')
    # Assignment hints are advisory. Auxiliary variables remain unspecified;
    # CP-SAT decides feasibility against every additional corridor constraint.
    model.clear_hints()
    for b in selected:
        for label in instance['domains'][b]:model.add_hint(variables[b,label],int(labels[b]==label))
    return dict(applied=True,reason='compatible_assignment_hint',selected_blocks=len(selected),metrics=m,
        complete_model_feasibility_certified=False)
