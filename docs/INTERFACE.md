# Checkpoint interface

Each component accepts a graph checkpoint, fixed assignments and a binary
community-membership array. Indices are zero-based.

## Graph

```text
ITER240_REQUEST_V1 n k capacity hmax number_of_unique_pairs number_of_hyperedges
D fpga distance_to_0 ... distance_to_k_minus_1
N node resource_weight current_fpga
P lower_node upper_node multiplicity
H weight driver number_of_drains drain_0 ...
```

Provide one D row per FPGA and one N row per node. P records describe weighted
undirected pairs for cut/hop evaluation; H records preserve hyperedges for
connectivity. Node resources and edge weights are positive integers. Capacity
is scalar. The format identifiers are retained for existing checkpoint compatibility.

## Fixed assignments

```text
ITER247_FIXED_V1 n number_of_fixed_nodes
node fpga
```

An empty fixed set still includes its header. Membership is exactly n
little-endian uint32 community labels in the same node order.

## Refinement and recovery

`refine` requires an input satisfying fixed assignments, capacity and Hmax.
It preserves the original checkpoint as its quality anchor and checks each
metric against the original per-metric 5% acceptance limit. A `PROCESS_FAILED`
state is not a successful optimization result.

`recover` accepts a checkpoint with hop violations and returns a recovery
proposal. When integrating it into a full partitioner, retain the original
checkpoint through recovery and subsequent quality refinement, and verify
fixed assignments, capacity, Hmax and all three original-checkpoint metric
limits before final acceptance. Reject failures back to that original checkpoint.

These component interfaces integrate at the refinement stage of a host
partitioner. Coarsening, initial partitioning and the host's final acceptance
remain host responsibilities.
