"""Hierarchy."""
#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import sys
from collections import Counter
from pathlib import Path


ANYTIME_MODEL_PATH = Path(__file__).resolve().parent / 'anytime.py'


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def compact_actual_policy(legacy_policy, estimate: dict[str, int]) -> dict:
    """Classify the model that CP-SAT really receives, not atomic_model's old expansion."""

    policy = legacy_policy(estimate)
    policy.update(estimate)
    policy["classification_variables_upper"] = estimate["variables_upper"]
    policy["classification_constraints_upper"] = estimate["constraints_upper"]
    policy["classification_basis"] = "compact_actual"
    policy["representation"] = "compact_label_cost_table_v1"
    return policy


def aggregate_instance(instance: dict, roots: list[int]) -> tuple[dict, list[list[int]]]:
    """Contract atomic families while preserving every represented assignment exactly."""

    groups_by_root: dict[int, list[int]] = {}
    for block, root in enumerate(roots):
        groups_by_root.setdefault(root, []).append(block)
    groups = sorted(groups_by_root.values(), key=lambda members: members[0])
    old_to_new = [0] * len(roots)
    for new, members in enumerate(groups):
        for old in members:
            old_to_new[old] = new

    block_sizes = []
    current_labels = []
    domains = []
    block_nodes = []
    family_keys = []
    fixed_old = set(instance["fixed_blocks"])
    fixed_blocks = []
    for new, members in enumerate(groups):
        labels = {instance["current_labels"][old] for old in members}
        domain_set = {tuple(instance["domains"][old]) for old in members}
        fixed_flags = {old in fixed_old for old in members}
        if len(labels) != 1 or len(domain_set) != 1 or len(fixed_flags) != 1:
            raise RuntimeError(f"unsafe coarse family {members[:8]}")
        block_sizes.append(sum(instance["block_sizes"][old] for old in members))
        current_labels.append(next(iter(labels)))
        domains.append(list(next(iter(domain_set))))
        nodes: list[int] = []
        for old in members:
            nodes.extend(instance["block_nodes"][old])
        block_nodes.append(nodes)
        family_keys.append(tuple(instance["atomic_family_keys"][members[0]]))
        if True in fixed_flags:
            fixed_blocks.append(new)

    pair_weights: Counter[tuple[int, int]] = Counter()
    for (left, right), weight in instance["pair_weights"].items():
        coarse = tuple(sorted((old_to_new[left], old_to_new[right])))
        if coarse[0] != coarse[1]:
            pair_weights[coarse] += weight

    directed_weights: Counter[tuple[int, int]] = Counter()
    for (source, drain), weight in instance["directed_weights"].items():
        coarse = old_to_new[source], old_to_new[drain]
        if coarse[0] != coarse[1]:
            directed_weights[coarse] += weight

    hyperedge_weights: Counter[tuple[int, ...]] = Counter()
    for blocks, weight in instance["hyperedge_weights"].items():
        coarse = tuple(sorted({old_to_new[block] for block in blocks}))
        if len(coarse) > 1:
            hyperedge_weights[coarse] += weight

    coarse = dict(instance)
    coarse.update({
        "block_of_node": [old_to_new[old] for old in instance["block_of_node"]],
        "block_nodes": block_nodes,
        "block_sizes": block_sizes,
        "current_labels": current_labels,
        "domains": domains,
        "pair_weights": pair_weights,
        "directed_weights": directed_weights,
        "hyperedge_weights": hyperedge_weights,
        "fixed_blocks": fixed_blocks,
        "atomic_family_keys": family_keys,
        "atomic_family_count": len(groups),
        "atomic_movable_family_count": len(groups) - len(fixed_blocks),
        "domain_policy": "large_v1_same_label_super_family",
    })
    return coarse, groups
