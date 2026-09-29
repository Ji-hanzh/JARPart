# JARPart

Core implementation of **JARPart: Community-Guided Joint Refinement for
Constrained Multi-FPGA Partitioning**.

JARPart constructs subcommunities from circuit communities and current FPGA
assignments, then refines their placement jointly under fixed-placement,
capacity and maximum-hop constraints. It also provides auxiliary feasibility
recovery for checkpoints with hop violations.

## Source layout

| Path | Contents |
| --- | --- |
| `core/subcommunity.py` | Subcommunity construction and community detection |
| `core/joint_assignment.py` | Sequential refinement and joint assignment |
| `core/feasibility_recovery.py` | Local recovery followed by joint recovery |
| `core/_engine/` | Graph aggregation, objective evaluation, CP-SAT models and bounded runtime |
| `examples/` | Small synthetic inputs for the three components |
| `tests/` | Constraint, joint-move, deadline and rollback checks |
| `config/cache_lock.json` | Circuit-cache identities and checksums |
| `tools/` | Cache installation and file verification |

## Installation

Use Linux x86_64 or WSL with Python 3.10 or newer.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python tools/verify.py
python tests/public_components.py
python tests/search_invariants.py
```

## Usage

The components take a graph checkpoint, fixed assignments and community labels.
The file formats and integration contract are described in [docs/INTERFACE.md](docs/INTERFACE.md).

```bash
python run.py construct --request examples/swap.request.tsv \
  --fixed examples/swap.fixed.tsv --membership examples/swap.membership.u32 \
  --output outputs/subcommunities.json

python run.py refine --request examples/swap.request.tsv \
  --fixed examples/swap.fixed.tsv --membership examples/swap.membership.u32 \
  --profile ordinary120 --output outputs/refined.json

python run.py recover --request examples/recovery.request.tsv \
  --fixed examples/recovery.fixed.tsv --membership examples/recovery.membership.u32 \
  --table table5 --output outputs/recovery.json
```

The swap example uses two fully occupied FPGAs. Joint reassignment reduces cut
cost from 10 to 0 while preserving fixed assignments and capacity. These are
synthetic demonstration inputs, not circuit benchmark results.

`ordinary120` gives sequential and joint refinement a shared 120-second window.
`large480` extends that window to 480 seconds only for models classified as Large;
Small/Medium models retain 120 seconds. Sequential refinement is capped at 30
seconds and each solver stage at 120 seconds. The objective uses normalized
weights of 0.50 connectivity, 0.25 cut and 0.25 hop numerator.

## Circuit caches

Download `JARPart_circuit_caches_20260929.tar.gz` from the repository's
[Releases](https://github.com/Ji-hanzh/JARPart/releases) and place it
beside `run.py`, then run:

```bash
python tools/install_caches.py JARPart_circuit_caches_20260929.tar.gz
python tools/verify.py --caches
```

The asset contains 60 community-membership arrays for 30 circuits, covering
Fixed and Non-Fixed views. The arrays store community labels, not raw netlists
or final FPGA assignments. Use the cache matching the exact input graph and
node ordering; original-graph labels cannot be applied directly to a coarsened
graph. Existing caches are never overwritten by the installer.

## Citation and dependencies

Author and title information is provided in [CITATION.cff](CITATION.cff).
Dependency acknowledgments are in [THIRD_PARTY.md](THIRD_PARTY.md).
