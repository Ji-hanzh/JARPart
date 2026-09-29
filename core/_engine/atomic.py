"""Atomic."""
#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import os
import sys
from collections import Counter
from pathlib import Path


FIXED_MODEL_PATH = Path(__file__).resolve().parent / 'fixed_input.py'
COMMUNITY_MODEL_PATH = Path(__file__).resolve().parent / 'community.py'
METRICS_MODEL_PATH = Path(__file__).resolve().parent / 'metrics.py'

OBJECTIVE_MODE = "mfs_balanced"
DEBT_PCT = "5"
DETECTOR = "pair"
SMALL_MAX_VARIABLES = 20_000
SMALL_MAX_CONSTRAINTS = 60_000
MEDIUM_MAX_VARIABLES = 1_250_000
MEDIUM_MAX_CONSTRAINTS = 5_000_000
SMALL_SECONDS = 120.0
MEDIUM_SECONDS = 120.0


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def setting(name: str, default, cast):
    return cast(os.environ.get(name, default))


def build_atomic_instance(request, membership: list[int], tag: str, legacy: dict) -> dict:
    """Collapse community x incumbent-partition intersections into atomic units."""

    if len(membership) != request.n:
        raise RuntimeError(f"membership length {len(membership)} != {request.n}")
    fixed_legacy = set(legacy.get("fixed_blocks", []))
    fixed_nodes = {
        node for node, block in enumerate(legacy["block_of_node"])
        if block in fixed_legacy
    }

    key_to_block: dict[tuple[int, int], int] = {}
    block_of_node = [0] * request.n
    block_sizes: list[int] = []
    current_labels: list[int] = []
    block_fixed: list[bool] = []
    block_nodes: list[list[int]] = []
    family_keys: list[tuple[int, int]] = []
    for node, (community, label) in enumerate(zip(membership, request.labels)):
        key = int(community), int(label)
        block = key_to_block.get(key)
        node_fixed = node in fixed_nodes
        if block is None:
            block = len(block_sizes)
            key_to_block[key] = block
            block_sizes.append(0)
            current_labels.append(label)
            block_fixed.append(node_fixed)
            block_nodes.append([])
            family_keys.append(key)
        elif block_fixed[block] != node_fixed:
            raise RuntimeError(f"mixed Fixed/non-Fixed atomic family {key}")
        block_sizes[block] += request.node_weights[node]
        block_nodes[block].append(node)
        block_of_node[node] = block

    domains = [
        [label] if block_fixed[block] else list(range(request.k))
        for block, label in enumerate(current_labels)
    ]
    pair_weights: Counter[tuple[int, int]] = Counter()
    for (left, right), weight in request.pair_weights.items():
        left_block, right_block = block_of_node[left], block_of_node[right]
        if left_block != right_block:
            pair_weights[tuple(sorted((left_block, right_block)))] += weight

    directed_weights: Counter[tuple[int, int]] = Counter()
    hyperedge_weights: Counter[tuple[int, ...]] = Counter()
    hop_count = 0
    for weight, source, drains in request.hyperedges:
        source_block = block_of_node[source]
        blocks = {source_block}
        for drain in drains:
            drain_block = block_of_node[drain]
            blocks.add(drain_block)
            hop_count += weight
            if source_block != drain_block:
                directed_weights[source_block, drain_block] += weight
        hyperedge_weights[tuple(sorted(blocks))] += weight

    return {
        "dataset": request,
        "name": tag,
        "assignment": list(request.labels),
        "distances": request.distances,
        "hmax": request.hmax,
        "k": request.k,
        "capacity": request.capacity,
        "block_of_node": block_of_node,
        "block_nodes": block_nodes,
        "block_sizes": block_sizes,
        "current_labels": current_labels,
        "domains": domains,
        "pair_weights": pair_weights,
        "directed_weights": directed_weights,
        "hyperedge_weights": hyperedge_weights,
        "hop_count": hop_count,
        "fixed_blocks": [block for block, fixed in enumerate(block_fixed) if fixed],
        "atomic_family_keys": family_keys,
        "atomic_family_count": len(block_sizes),
        "atomic_movable_family_count": sum(not fixed for fixed in block_fixed),
        "domain_policy": "all_fpga_labels_except_fixed",
    }


def estimate_model_size(instance: dict, selected: list[int]) -> dict[str, int]:
    """Count the complete model before materializing pair-state variables."""

    selected_set = set(selected)
    variables = sum(len(instance["domains"][block]) for block in selected)
    constraints = len(selected) + instance["k"] + 3 + 2
    pair_state_variables = 0
    pair_constraints = 0
    for left, right in instance["pair_weights"]:
        left_selected, right_selected = left in selected_set, right in selected_set
        if left_selected and right_selected:
            compatible = 0
            incompatible = 0
            for left_label in instance["domains"][left]:
                for right_label in instance["domains"][right]:
                    if instance["distances"][left_label][right_label] <= instance["hmax"]:
                        compatible += 1
                    else:
                        incompatible += 1
            pair_state_variables += compatible
            pair_constraints += incompatible + 3 * compatible + 1
        elif left_selected or right_selected:
            moving, fixed = (left, right) if left_selected else (right, left)
            fixed_label = instance["current_labels"][fixed]
            pair_constraints += sum(
                instance["distances"][label][fixed_label] > instance["hmax"]
                for label in instance["domains"][moving]
            )
    variables += pair_state_variables
    constraints += pair_constraints

    presence_variables = 0
    for blocks in instance["hyperedge_weights"]:
        moving = [block for block in blocks if block in selected_set]
        if not moving:
            continue
        fixed_labels = {
            instance["current_labels"][block]
            for block in blocks if block not in selected_set
        }
        for label in range(instance["k"]):
            if label not in fixed_labels and any(
                label in instance["domains"][block] for block in moving
            ):
                presence_variables += 1
    variables += presence_variables
    constraints += presence_variables
    return {
        "variables_upper": variables,
        "constraints_upper": constraints,
        "assignment_variables": sum(len(instance["domains"][block]) for block in selected),
        "pair_state_variables": pair_state_variables,
        "hyperedge_presence_variables": presence_variables,
    }


def model_policy(estimate: dict[str, int]) -> dict:
    small_variables = setting("JARPART_ATOMIC_MODEL_SMALL_MAX_VARIABLES", SMALL_MAX_VARIABLES, int)
    small_constraints = setting("JARPART_ATOMIC_MODEL_SMALL_MAX_CONSTRAINTS", SMALL_MAX_CONSTRAINTS, int)
    medium_variables = setting("JARPART_ATOMIC_MODEL_MEDIUM_MAX_VARIABLES", MEDIUM_MAX_VARIABLES, int)
    medium_constraints = setting("JARPART_ATOMIC_MODEL_MEDIUM_MAX_CONSTRAINTS", MEDIUM_MAX_CONSTRAINTS, int)
    variables, constraints = estimate["variables_upper"], estimate["constraints_upper"]
    if variables <= small_variables and constraints <= small_constraints:
        tier = "small_exact"
        relative_gap, seconds = 0.0, setting("JARPART_ATOMIC_MODEL_SMALL_SECONDS", SMALL_SECONDS, float)
    elif variables <= medium_variables and constraints <= medium_constraints:
        tier = "medium_gap1"
        relative_gap, seconds = 0.01, setting("JARPART_ATOMIC_MODEL_MEDIUM_SECONDS", MEDIUM_SECONDS, float)
    else:
        tier = "large_vcycle_deferred"
        relative_gap, seconds = None, None
    return {
        "tier": tier,
        "relative_gap_limit": relative_gap,
        "max_time_seconds": seconds,
        "small_limits": {"variables": small_variables, "constraints": small_constraints},
        "medium_limits": {"variables": medium_variables, "constraints": medium_constraints},
        **estimate,
    }


def relative_gap(objective: float, bound: float) -> float:
    return abs(objective - bound) / max(1.0, abs(objective))
