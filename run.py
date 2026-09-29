#!/usr/bin/env python3
"""Run a paper component on an existing checkpoint. See README for examples."""
import argparse,json,struct
from pathlib import Path

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('component',choices=('construct','refine','recover'))
    p.add_argument('--request',type=Path,required=True)
    p.add_argument('--fixed',type=Path,required=True)
    p.add_argument('--membership',type=Path,required=True)
    p.add_argument('--profile',choices=('ordinary120','large480'),default='ordinary120')
    p.add_argument('--table',choices=[f'table{i}' for i in range(4,12)],default='table4')
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    if a.output.exists() or a.output.with_suffix('.labels.u32').exists():p.error('Choose a new output path')
    paths=(a.request.resolve(),a.fixed.resolve(),a.membership.resolve())
    if a.component=='construct':
        from core.subcommunity import construct
        instance=construct(*paths)
        result={k:instance[k] for k in ('block_of_node','block_nodes','block_sizes','current_labels','domains','fixed_blocks','atomic_family_keys')}
        result['connection_counts']={k:len(instance[k]) for k in ('pair_weights','directed_weights','hyperedge_weights')}
    elif a.component=='refine':
        from core.joint_assignment import refine
        result=refine(*paths,a.profile,a.output.stem)
    else:
        from core.feasibility_recovery import propose
        result=propose(*paths,a.table)
    a.output.parent.mkdir(parents=True,exist_ok=True)
    labels=result.pop('output_labels',None)
    if labels is not None:a.output.with_suffix('.labels.u32').write_bytes(struct.pack(f'<{len(labels)}I',*labels))
    a.output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(a.component,result.get('state',result.get('status','CONSTRUCTED')),a.output)
    return int(result.get('state')=='PROCESS_FAILED')
if __name__=='__main__':raise SystemExit(main())
