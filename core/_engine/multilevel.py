"""Multilevel."""
#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import itertools
import math
import os
import sys
import time
from collections import Counter
from pathlib import Path

from ortools.sat.python import cp_model


HIERARCHY_MODEL_PATH = Path(__file__).resolve().parent / 'hierarchy.py'
DEFAULT_STAGE_SECONDS = 120.0
# Zero means every stored coarse-to-atomic level.  A positive override is an
# explicit emergency cap and is reported in the result.
DEFAULT_MAX_REFINEMENT_LEVELS = 0
DEFAULT_COARSE_RETRIES = 3
DEFAULT_EXTRA_COARSE_LEVELS = 2
DEFAULT_TARGET_MEDIUM_RATIO = 1.0
# Zero means use the same compact-actual Medium envelope as anytime_model.  Positive
# values remain available only as explicit operational overrides.
DEFAULT_REFINEMENT_VARIABLE_LIMIT = 0
DEFAULT_REFINEMENT_CONSTRAINT_LIMIT = 0
DEFAULT_REFINEMENT_GROWTH_FACTOR = 2.0
# Zero means an adaptive call budget large enough to finish exponential search
# plus binary refinement.  A positive environment override remains available
# as an explicit emergency cap and is reported as an uncertified prefix.
DEFAULT_REFINEMENT_ESTIMATE_CALLS = 0
DEFAULT_FALLBACK_PROPOSAL_SEEDS = 0
DEFAULT_HIERARCHY_CLIQUE_MAX_PINS = 32


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


def presence_count(instance: dict, selected: list[int]) -> int:
    selected_set = set(selected)
    count = 0
    for blocks in instance["hyperedge_weights"]:
        moving = [block for block in blocks if block in selected_set]
        if not moving:
            continue
        fixed_labels = {
            instance["current_labels"][block]
            for block in blocks if block not in selected_set
        }
        count += sum(
            label not in fixed_labels
            and any(label in instance["domains"][block] for block in moving)
            for label in range(instance["k"])
        )
    return count


def compress_active_hyperedges(instance: dict, selected: list[int]) -> tuple[dict, dict]:
    """Losslessly merge hyperedges with the same active OR expression.

    A signature contains the selected blocks on an edge and the labels already
    supplied by the fixed background.  Fixed block identities do not affect
    Connectivity.  The transformation therefore preserves Connectivity for
    every assignment of ``selected`` while avoiding duplicate presence vars.
    """

    selected_set = set(selected)
    signatures: dict[tuple[tuple[int, ...], tuple[int, ...]], tuple[int, tuple[int, ...]]] = {}
    for blocks, weight in instance["hyperedge_weights"].items():
        moving = tuple(block for block in blocks if block in selected_set)
        representatives: dict[int, int] = {}
        for block in blocks:
            if block not in selected_set:
                label = instance["current_labels"][block]
                representatives[label] = min(block, representatives.get(label, block))
        fixed_labels = tuple(sorted(representatives))
        signature = moving, fixed_labels
        represented_blocks = tuple(sorted((*moving, *representatives.values())))
        old_weight, _ = signatures.get(signature, (0, represented_blocks))
        signatures[signature] = old_weight + weight, represented_blocks

    compressed_weights: Counter[tuple[int, ...]] = Counter()
    for weight, blocks in signatures.values():
        if len(blocks) > 1:
            compressed_weights[blocks] += weight

    compressed = dict(instance)
    compressed["hyperedge_weights"] = compressed_weights
    raw_presence = presence_count(instance, selected)
    compressed_presence = presence_count(compressed, selected)
    stats = {
        "raw_hyperedges": len(instance["hyperedge_weights"]),
        "signature_hyperedges": len(compressed_weights),
        "collapsed_hyperedges": len(instance["hyperedge_weights"]) - len(compressed_weights),
        "raw_presence_variables": raw_presence,
        "compressed_presence_variables": compressed_presence,
        "presence_variables_saved": raw_presence - compressed_presence,
    }
    compressed["active_hyperedge_signature_stats"] = stats
    return compressed, stats


def compressed_estimate(legacy_estimate, compact_model, instance: dict, selected: list[int]) -> dict:
    compressed, stats = compress_active_hyperedges(instance, selected)
    estimate = compact_model.compact_estimate(legacy_estimate, compressed, selected)
    raw_variables = estimate["variables_upper"] + stats["presence_variables_saved"]
    raw_constraints = estimate["constraints_upper"] + stats["presence_variables_saved"]
    return {
        **estimate,
        **stats,
        "raw_compact_variables_upper": raw_variables,
        "raw_compact_constraints_upper": raw_constraints,
        "representation": "compact_label_cost_table_active_hyperedge_signature_v2",
    }


def build_model(compact_model, base, instance: dict, selected: list[int], hint: list[int],
                locks: dict[str, int], objective: str, coefficients: dict[str, int],
                debt_text: str):
    compressed, _ = compress_active_hyperedges(instance, selected)
    return compact_model.build_model(
        base, compressed, selected, hint, locks, objective, coefficients, debt_text
    )


def materialize_level(hierarchy_model, atomic: dict, roots: tuple[int, ...]) -> tuple[dict, list[list[int]], list[int]]:
    level, groups = hierarchy_model.aggregate_instance(atomic, list(roots))
    atom_to_level = [0] * len(roots)
    for block, atoms in enumerate(groups):
        for atom in atoms:
            atom_to_level[atom] = block
    return level, groups, atom_to_level


def downsample_records(records: list[dict], max_levels: int) -> list[dict]:
    if max_levels <= 0:
        return []
    if max_levels == 1:
        return [records[0]]
    if len(records) <= max_levels:
        return records
    indexes = {
        round(position * (len(records) - 1) / (max_levels - 1))
        for position in range(max_levels)
    }
    indexes.update((0, len(records) - 1))
    return [records[index] for index in sorted(indexes)]


def hierarchy_edges(
    instance: dict, coefficients: dict[str, int]
) -> list[tuple[float, int, int, int]]:
    """Rank reversible merges by topology backbone, then normalized MFS support.

    Direct pair/source-drain relations define the exact MFS topology backbone,
    so they form an explicit first tier instead of competing numerically with
    projected Connectivity links.  Within each tier all support remains in the
    parent-derived Cut/Hop/Connectivity units and is normalized by block-size
    product.  Unlike V8, a large hyperedge with no real objective adjacency is
    never converted into an arbitrary size/ID chain.
    """

    pair_strengths: Counter[tuple[int, int]] = Counter()
    hyperedge_strengths: Counter[tuple[int, int]] = Counter()
    fixed = set(instance["fixed_blocks"])
    objective_neighbors: dict[int, set[int]] = {
        block: set() for block in range(len(instance["block_sizes"]))
    }

    def eligible(left: int, right: int) -> bool:
        return (
            left != right
            and left not in fixed and right not in fixed
            and instance["current_labels"][left] == instance["current_labels"][right]
            and instance["domains"][left] == instance["domains"][right]
        )

    for (left, right), weight in instance["pair_weights"].items():
        if eligible(left, right):
            edge = tuple(sorted((left, right)))
            pair_strengths[edge] += coefficients["cut"] * weight
            objective_neighbors[left].add(right)
            objective_neighbors[right].add(left)

    for (source, drain), weight in instance["directed_weights"].items():
        if eligible(source, drain):
            edge = tuple(sorted((source, drain)))
            pair_strengths[edge] += coefficients["hop_numerator"] * weight
            objective_neighbors[source].add(drain)
            objective_neighbors[drain].add(source)

    clique_max_pins = max(2, setting(
        "JARPART_VCYCLE_MODEL_HIERARCHY_CLIQUE_MAX_PINS",
        DEFAULT_HIERARCHY_CLIQUE_MAX_PINS,
        int,
    ))
    for blocks, weight in instance["hyperedge_weights"].items():
        buckets: dict[tuple[int, tuple[int, ...]], list[int]] = {}
        for block in blocks:
            if block not in fixed:
                key = instance["current_labels"][block], tuple(instance["domains"][block])
                buckets.setdefault(key, []).append(block)
        for members in buckets.values():
            divisor = max(1, len(members) - 1)
            support = max(1, coefficients["connectivity"] * weight // divisor)
            if len(members) <= clique_max_pins:
                pairs = itertools.combinations(sorted(members), 2)
            else:
                member_set = set(members)
                pairs = (
                    (left, right)
                    for left in sorted(members)
                    for right in sorted(objective_neighbors[left] & member_set)
                    if left < right
                )
            for left, right in pairs:
                hyperedge_strengths[left, right] += support

    ranked = []
    for left, right in pair_strengths.keys() | hyperedge_strengths.keys():
        denominator = max(1, instance["block_sizes"][left] * instance["block_sizes"][right])
        pair_strength = pair_strengths[left, right]
        hyperedge_strength = hyperedge_strengths[left, right]
        total_strength = pair_strength + hyperedge_strength
        ranked.append((
            bool(pair_strength),
            pair_strength / denominator,
            hyperedge_strength / denominator,
            total_strength / denominator,
            total_strength,
            left,
            right,
        ))
    ranked.sort(key=lambda row: (
        -int(row[0]), -row[1], -row[2], -row[3], -row[4], row[5], row[6]
    ))
    return [(row[3], row[4], row[5], row[6]) for row in ranked]


def target_medium_reached(policy: dict, ratio: float) -> bool:
    """Require headroom inside Medium, not merely first contact with its boundary."""

    if policy["tier"] == "large_vcycle_deferred":
        return False
    limits = policy["medium_limits"]
    return (
        policy["variables_upper"] <= ratio * limits["variables"]
        and policy["constraints_upper"] <= ratio * limits["constraints"]
    )


def progressive_refinement_limits(
    reference_variables: int,
    reference_constraints: int,
    medium_limits: dict[str, int],
    growth_factor: float,
) -> tuple[int, int]:
    """Grow one V-cycle step geometrically without jumping to the full ceiling."""

    if growth_factor <= 1.0:
        raise ValueError("refinement growth factor must be greater than one")
    return (
        min(
            medium_limits["variables"],
            max(reference_variables + 1, math.ceil(reference_variables * growth_factor)),
        ),
        min(
            medium_limits["constraints"],
            max(reference_constraints + 1, math.ceil(reference_constraints * growth_factor)),
        ),
    )


def build_hierarchy(
    hierarchy_model, atomic_model, atomic: dict, coefficients: dict[str, int]
) -> tuple[list[dict] | None, dict]:
    """Keep coarsening after first Medium so UNKNOWN can retry on a smaller model."""

    count = len(atomic["block_sizes"])
    selected = [block for block, domain in enumerate(atomic["domains"]) if len(domain) > 1]
    original_estimate = atomic_model.estimate_model_size(atomic, selected)
    original_policy = atomic_model.model_policy(original_estimate)
    identity = tuple(range(count))
    records = [{
        "roots": identity,
        "estimate": original_estimate,
        "policy": original_policy,
        "merge_count": 0,
    }]
    if original_policy["tier"] != "large_vcycle_deferred":
        return records, {
            "status": "not_large", "levels": 1, "original_policy": original_policy,
        }

    parent = list(range(count))
    weights = list(atomic["block_sizes"])

    def find(block: int) -> int:
        while parent[block] != block:
            parent[block] = parent[parent[block]]
            block = parent[block]
        return block

    edges = hierarchy_edges(atomic, coefficients)
    merged = 0
    batch = max(1, len(selected) // 10)
    first_medium_index = None
    target_index = None
    extra_levels = max(1, setting(
        "JARPART_VCYCLE_MODEL_EXTRA_COARSE_LEVELS", DEFAULT_EXTRA_COARSE_LEVELS, int,
    ))
    target_ratio = setting(
        "JARPART_VCYCLE_MODEL_TARGET_MEDIUM_RATIO", DEFAULT_TARGET_MEDIUM_RATIO, float,
    )
    for edge_index, (_, _, left, right) in enumerate(edges, start=1):
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            continue
        if weights[left_root] + weights[right_root] > atomic["capacity"]:
            continue
        low, high = sorted((left_root, right_root))
        parent[high] = low
        weights[low] += weights[high]
        merged += 1
        if merged % batch and edge_index != len(edges):
            continue
        roots = tuple(find(block) for block in range(count))
        level, _, _ = materialize_level(hierarchy_model, atomic, roots)
        level_selected = [
            block for block, domain in enumerate(level["domains"]) if len(domain) > 1
        ]
        estimate = atomic_model.estimate_model_size(level, level_selected)
        policy = atomic_model.model_policy(estimate)
        records.append({
            "roots": roots,
            "estimate": estimate,
            "policy": policy,
            "merge_count": merged,
        })
        if policy["tier"] != "large_vcycle_deferred" and first_medium_index is None:
            first_medium_index = len(records) - 1
        if (
            first_medium_index is not None
            and target_medium_reached(policy, target_ratio)
            and len(records) - 1 - first_medium_index >= extra_levels
        ):
            target_index = len(records) - 1
            break

    if first_medium_index is None:
        last = records[-1]
        return None, {
            "status": "unresolved",
            "original_policy": original_policy,
            "last_policy": last["policy"],
            "original_families": count,
            "last_families": len(set(last["roots"])),
            "merges": merged,
            "eligible_edges": len(edges),
        }

    if target_index is None:
        target_index = len(records) - 1
    return records, {
        "status": "activated",
        "original_policy": original_policy,
        "first_medium_policy": records[first_medium_index]["policy"],
        "coarse_policy": records[target_index]["policy"],
        "original_families": count,
        "first_medium_families": len(set(records[first_medium_index]["roots"])),
        "coarse_families": len(set(records[target_index]["roots"])),
        "levels": len(records),
        "first_medium_index": first_medium_index,
        "target_index": target_index,
        "continued_levels_after_first_medium": target_index - first_medium_index,
        "target_medium_ratio": target_ratio,
        "target_medium_reached": target_medium_reached(records[target_index]["policy"], target_ratio),
        "merges": count - len(set(records[target_index]["roots"])),
        "eligible_edges": len(edges),
        "hierarchy_score_policy": "topology_backbone_first_then_parent_normalized_mfs_v4",
        "hyperedge_projection": "symmetric_small_clique_then_real_objective_adjacency_no_fabricated_chain",
        "stored_state": "atomic_to_level_maps_only",
    }


def select_coarse_retry_records(records: list[dict], first: int, last: int,
                                retries: int) -> list[tuple[int, dict]]:
    """Try first-Medium, intermediate and deepest states in increasing compression."""

    indexes = list(range(first, last + 1))
    if len(indexes) > retries:
        positions = {
            round(position * (len(indexes) - 1) / (retries - 1))
            for position in range(retries)
        }
        indexes = [indexes[position] for position in sorted(positions)]
    return [(index, records[index]) for index in indexes]


def select_refinement_records(records: list[dict], coarse_index: int,
                              max_levels: int) -> list[tuple[int, dict]]:
    """Return a bounded coarse-to-fine path that always ends at the atomic level."""

    finer = records[:coarse_index]
    if not finer or max_levels <= 0:
        return []
    sampled = downsample_records(finer, min(max_levels, len(finer)))
    by_identity = {record["roots"]: index for index, record in enumerate(records)}
    return [(by_identity[record["roots"]], record) for record in reversed(sampled)]


def project_atomic_labels(groups: list[list[int]], atomic_labels: list[int]) -> list[int]:
    labels = []
    for atoms in groups:
        values = {atomic_labels[atom] for atom in atoms}
        if len(values) != 1:
            raise RuntimeError(f"nonuniform projected super-family {atoms[:8]} labels={values}")
        labels.append(next(iter(values)))
    return labels


def atomic_labels_from_level(groups: list[list[int]], labels: list[int], count: int) -> list[int]:
    atomic_labels = [0] * count
    for block, atoms in enumerate(groups):
        for atom in atoms:
            atomic_labels[atom] = labels[block]
    return atomic_labels


def local_importance(instance: dict, coefficients: dict[str, int]) -> list[int]:
    """Rank current objective pressure, not merely incident edge volume."""

    importance = [0] * len(instance["block_sizes"])
    labels = instance["current_labels"]
    for (left, right), weight in instance["pair_weights"].items():
        if labels[left] == labels[right]:
            continue
        value = coefficients["cut"] * weight
        importance[left] += value
        importance[right] += value
    for (source, drain), weight in instance["directed_weights"].items():
        distance = instance["distances"][labels[source]][labels[drain]]
        if not distance:
            continue
        value = coefficients["hop_numerator"] * weight * distance
        importance[source] += value
        importance[drain] += value
    for blocks, weight in instance["hyperedge_weights"].items():
        connectivity = len({labels[block] for block in blocks}) - 1
        if not connectivity:
            continue
        value = coefficients["connectivity"] * weight * connectivity
        for block in blocks:
            importance[block] += value
    return importance


def largest_medium_prefix(
    atomic_model,
    instance: dict,
    ordered: list[int],
    variable_limit: int | None = None,
    constraint_limit: int | None = None,
    deadline: float | None = None,
    max_calls: int = DEFAULT_REFINEMENT_ESTIMATE_CALLS,
    prefix_boundaries: list[int] | None = None,
) -> tuple[list[int], dict]:
    """Find the largest certified fitting prefix on legal bundle boundaries.

    The historical Medium ceiling is too large for a refinement stage: a
    1.2M-variable corridor can consume the whole reserved budget in model
    construction alone.  Refinement therefore keeps an explicit smaller build
    envelope, but it no longer confuses six estimates with a 32-family limit.
    Exponential probing brackets the boundary and binary search certifies the
    largest legal prefix.  Bundle endpoints prevent a capacity-exchange group
    from being cut in half.
    """

    if not ordered:
        return [], {"candidate_families": 0, "selected_families": 0}

    boundaries = sorted({
        int(length) for length in (prefix_boundaries or range(1, len(ordered) + 1))
        if 0 < int(length) <= len(ordered)
    })
    if not boundaries or boundaries[-1] != len(ordered):
        boundaries.append(len(ordered))

    started = time.perf_counter()
    calls = 0
    adaptive_call_budget = 2 * math.ceil(math.log2(len(boundaries) + 1)) + 4
    call_budget = max_calls if max_calls > 0 else adaptive_call_budget

    def classify(length: int) -> tuple[dict, dict]:
        nonlocal calls
        calls += 1
        estimate = atomic_model.estimate_model_size(instance, ordered[:length])
        return estimate, atomic_model.model_policy(estimate)

    def fits(estimate: dict, policy: dict) -> bool:
        return (
            policy["tier"] != "large_vcycle_deferred"
            and (variable_limit is None or estimate["variables_upper"] <= variable_limit)
            and (
                constraint_limit is None
                or estimate["constraints_upper"] <= constraint_limit
            )
        )

    low_position = -1
    high_position = None
    best_estimate = None
    best_policy = None
    first_rejected_estimate = None
    first_rejected_policy = None
    stop_reason = "all_candidates_fit"
    probe_position = 0
    while calls < call_budget:
        if deadline is not None and time.perf_counter() >= deadline:
            stop_reason = "estimation_deadline"
            break
        length = boundaries[probe_position]
        estimate, policy = classify(length)
        if not fits(estimate, policy):
            stop_reason = "refinement_build_envelope"
            high_position = probe_position
            first_rejected_estimate = estimate
            first_rejected_policy = policy
            break
        low_position = probe_position
        best_estimate, best_policy = estimate, policy
        if probe_position == len(boundaries) - 1:
            break
        probe_position = min(len(boundaries) - 1, 2 * probe_position + 1)
    else:
        stop_reason = "estimate_call_cap"

    # Once exponential search finds the first rejected boundary, certify the
    # exact largest fitting bundle prefix rather than returning the last power
    # of two.  Deadline and an explicit positive call cap remain hard bounds.
    while (
        high_position is not None
        and high_position - low_position > 1
        and calls < call_budget
    ):
        if deadline is not None and time.perf_counter() >= deadline:
            stop_reason = "estimation_deadline"
            break
        middle = (low_position + high_position) // 2
        estimate, policy = classify(boundaries[middle])
        if fits(estimate, policy):
            low_position = middle
            best_estimate, best_policy = estimate, policy
        else:
            high_position = middle
            first_rejected_estimate = estimate
            first_rejected_policy = policy

    maximum_prefix_certified = (
        low_position == len(boundaries) - 1
        or high_position is not None and high_position - low_position == 1
    )
    if not maximum_prefix_certified and stop_reason not in (
        "estimation_deadline", "estimate_call_cap",
    ):
        stop_reason = "estimate_call_cap"
    low = boundaries[low_position] if low_position >= 0 else 0

    return ordered[:low], {
        "candidate_families": len(ordered),
        "selected_families": low,
        "estimate": best_estimate,
        "policy": best_policy,
        "first_rejected_families": (
            boundaries[high_position] if high_position is not None else None
        ),
        "first_rejected_estimate": first_rejected_estimate,
        "first_rejected_policy": first_rejected_policy,
        "selected_family_ids": ordered[:low],
        "variable_limit": variable_limit,
        "constraint_limit": constraint_limit,
        "estimate_calls": calls,
        "estimate_call_budget": call_budget,
        "estimate_call_budget_mode": "explicit" if max_calls > 0 else "adaptive",
        "estimation_seconds": time.perf_counter() - started,
        "prefix_search": "exponential_then_binary_on_bundle_boundaries",
        "prefix_boundary_count": len(boundaries),
        "maximum_prefix_certified": maximum_prefix_certified,
        "stop_reason": stop_reason,
    }


def capacity_exchange_partners(
    instance: dict,
    groups: list[list[int]],
    current_labels: list[int],
    original_atomic_labels: list[int],
    moved_children: set[int],
    proposal_children: list[int],
    importance: list[int],
) -> tuple[set[int], dict[int, list[dict]], dict]:
    """Cover the real capacity deficit of each seed/target proposal.

    V8 selected four partners regardless of FPGA count, current slack or block
    size.  V14 instead chooses the smallest ranked reverse-mover prefix whose
    cumulative weight covers the exact target deficit.  Targets with enough
    slack need no artificial partner, and an infeasible incomplete group is
    recorded rather than presented as a usable exchange.
    """

    movable = {
        block for block, domain in enumerate(instance["domains"]) if len(domain) > 1
    }
    loads = [0] * instance["k"]
    for block, label in enumerate(current_labels):
        loads[label] += instance["block_sizes"][block]
    partners = set()
    partner_map: dict[int, list[dict]] = {}
    target_proposals = 0
    slack_targets = 0
    covered_deficits = 0
    uncovered_deficits = 0
    for anchor in proposal_children:
        source = current_labels[anchor]
        targets = [
            label for label in instance["domains"][anchor] if label != source
        ]
        anchor_groups = []
        for target in targets:
            target_proposals += 1
            required = max(
                0,
                loads[target] + instance["block_sizes"][anchor] - instance["capacity"],
            )
            if required == 0:
                slack_targets += 1
                continue
            ranked = []
            for block in movable:
                if (
                    block == anchor
                    or current_labels[block] != target
                    or source not in instance["domains"][block]
                ):
                    continue
                ranked.append((
                    abs(
                        instance["block_sizes"][block]
                        - required
                    ),
                    -importance[block],
                    block,
                ))
            chosen = []
            covered = 0
            for _, _, block in sorted(ranked):
                chosen.append(block)
                covered += instance["block_sizes"][block]
                if covered >= required:
                    break
            if covered < required:
                uncovered_deficits += 1
                continue
            covered_deficits += 1
            group = {
                "target_label": target,
                "required_weight": required,
                "covered_weight": covered,
                "partner_ids": chosen,
            }
            anchor_groups.append(group)
            partners.update(chosen)
        if anchor_groups:
            partner_map[anchor] = anchor_groups
    return partners, partner_map, {
        "target_proposals": target_proposals,
        "slack_targets": slack_targets,
        "covered_capacity_deficits": covered_deficits,
        "uncovered_capacity_deficits": uncovered_deficits,
        "partner_policy": "minimum_ranked_reverse_weight_cover_per_target_deficit",
    }


def select_refinement_corridor(
    atomic_model,
    instance: dict,
    groups: list[list[int]],
    coarse_roots: tuple[int, ...],
    current_labels: list[int],
    original_atomic_labels: list[int],
    coefficients: dict[str, int],
    deadline: float | None = None,
    variable_limit_override: int | None = None,
    constraint_limit_override: int | None = None,
) -> tuple[list[int], dict]:
    """Free split children and their MFS/topology boundary under Medium limits."""

    parent_key = [coarse_roots[atoms[0]] for atoms in groups]
    child_counts = Counter(parent_key)
    movable = {
        block for block, domain in enumerate(instance["domains"]) if len(domain) > 1
    }
    split_children = {
        block for block in movable if child_counts[parent_key[block]] > 1
    }
    moved_children = {
        block for block in split_children
        if current_labels[block] != original_atomic_labels[groups[block][0]]
    }
    boundary = set()
    neighbors: dict[int, set[int]] = {block: set() for block in movable}
    for left, right in instance["pair_weights"]:
        if parent_key[left] != parent_key[right]:
            boundary.update((left, right))
        if left in movable and right in movable:
            neighbors[left].add(right)
            neighbors[right].add(left)
    for blocks in instance["hyperedge_weights"]:
        if len({parent_key[block] for block in blocks}) > 1:
            boundary.update(blocks)
        active = [block for block in blocks if block in movable]
        for block in active:
            neighbors[block].update(other for other in active if other != block)

    seeds = moved_children or (split_children & boundary) or split_children
    one_hop = {
        neighbor for block in seeds for neighbor in neighbors.get(block, ())
    }
    loads = [0] * instance["k"]
    for block, label in enumerate(current_labels):
        loads[label] += instance["block_sizes"][block]
    load_ratios = [
        load_value / instance["capacity"] if instance["capacity"] else 0.0
        for load_value in loads
    ]
    importance = local_importance(instance, coefficients)
    seed_limit = setting(
        "JARPART_VCYCLE_MODEL_FALLBACK_PROPOSAL_SEEDS",
        DEFAULT_FALLBACK_PROPOSAL_SEEDS,
        int,
    )
    ranked_seeds = sorted(
        seeds,
        key=lambda block: (
            -load_ratios[current_labels[block]],
            -importance[block],
            block,
        ),
    )
    proposal_seeds = ranked_seeds if seed_limit <= 0 else ranked_seeds[:seed_limit]
    exchange_partners, exchange_partner_map, exchange_diagnostics = capacity_exchange_partners(
        instance, groups, current_labels, original_atomic_labels,
        moved_children, proposal_seeds, importance,
    )
    candidates = (split_children | one_hop | exchange_partners) & movable
    if not candidates:
        candidates = boundary & movable
    remainder = sorted(
        candidates,
        key=lambda block: (
            -(6 if block in moved_children else 0),
            -(5 if block in seeds else 0),
            -(4 if block in exchange_partners else 0),
            -(3 if block in boundary else 0),
            -(2 if block in one_hop else 0),
            -load_ratios[current_labels[block]],
            -importance[block],
            block,
        ),
    )
    # Capacity groups overlap: one atom may be another seed or a partner in
    # several groups.  Every prefix boundary therefore closes the dependency
    # graph.  If an anchor and any member of one of its deficit-cover groups
    # are present, the whole group is inserted before the boundary is legal.
    ordered = []
    ordered_seen = set()
    prefix_boundaries = []
    proposal_bundle_records = [
        {
            "anchor": seed,
            "target_label": group["target_label"],
            "required_weight": group["required_weight"],
            "covered_weight": group["covered_weight"],
            "partner_ids": list(group["partner_ids"]),
        }
        for seed, groups_for_seed in exchange_partner_map.items()
        for group in groups_for_seed
    ]

    def append_once(block: int) -> None:
        if block in candidates and block not in ordered_seen:
            ordered_seen.add(block)
            ordered.append(block)

    def close_active_partner_groups() -> None:
        changed = True
        while changed:
            changed = False
            for record in proposal_bundle_records:
                partner_set = set(record["partner_ids"])
                active = (
                    record["anchor"] in ordered_seen
                    and bool(ordered_seen.intersection(partner_set))
                )
                if not active or partner_set <= ordered_seen:
                    continue
                before = len(ordered)
                for block in record["partner_ids"]:
                    append_once(block)
                changed = changed or len(ordered) > before

    for seed in proposal_seeds:
        append_once(seed)
        close_active_partner_groups()
        prefix_boundaries.append(len(ordered))
        for group in exchange_partner_map.get(seed, ()):
            for block in group["partner_ids"]:
                append_once(block)
            close_active_partner_groups()
            prefix_boundaries.append(len(ordered))
    for block in remainder:
        append_once(block)
        close_active_partner_groups()
        prefix_boundaries.append(len(ordered))
    variable_limit_setting = setting(
        "JARPART_VCYCLE_MODEL_REFINEMENT_VARIABLE_LIMIT",
        DEFAULT_REFINEMENT_VARIABLE_LIMIT,
        int,
    )
    constraint_limit_setting = setting(
        "JARPART_VCYCLE_MODEL_REFINEMENT_CONSTRAINT_LIMIT",
        DEFAULT_REFINEMENT_CONSTRAINT_LIMIT,
        int,
    )
    variable_limit = variable_limit_override
    constraint_limit = constraint_limit_override
    if variable_limit_setting > 0:
        variable_limit = (
            variable_limit_setting
            if variable_limit is None
            else min(variable_limit, variable_limit_setting)
        )
    if constraint_limit_setting > 0:
        constraint_limit = (
            constraint_limit_setting
            if constraint_limit is None
            else min(constraint_limit, constraint_limit_setting)
        )
    selected, metadata = largest_medium_prefix(
        atomic_model,
        instance,
        ordered,
        variable_limit=variable_limit,
        constraint_limit=constraint_limit,
        deadline=deadline,
        max_calls=setting(
            "JARPART_VCYCLE_MODEL_REFINEMENT_ESTIMATE_CALLS",
            DEFAULT_REFINEMENT_ESTIMATE_CALLS,
            int,
        ),
        prefix_boundaries=prefix_boundaries,
    )
    selected_set = set(selected)
    selected_proposal_bundles = [
        record for record in proposal_bundle_records
        if record["anchor"] in selected_set
        and selected_set.intersection(record["partner_ids"])
        and set(record["partner_ids"]) <= selected_set
    ]
    partially_selected_proposal_bundles = [
        record for record in proposal_bundle_records
        if record["anchor"] in selected_set
        and selected_set.intersection(record["partner_ids"])
        and not set(record["partner_ids"]) <= selected_set
    ]
    metadata.update({
        "candidate_family_ids": ordered,
        "split_children": len(split_children),
        "moved_children": len(moved_children),
        "boundary_children": len(boundary & movable),
        "one_hop_children": len(one_hop),
        "capacity_exchange_partners": len(exchange_partners),
        "capacity_exchange_partner_pairs": sum(
            len(group["partner_ids"])
            for groups_for_seed in exchange_partner_map.values()
            for group in groups_for_seed
        ),
        "capacity_exchange_diagnostics": exchange_diagnostics,
        "proposal_seed_ids": proposal_seeds,
        "proposal_seed_count": len(proposal_seeds),
        "proposal_bundles": proposal_bundle_records,
        "proposal_bundle_count": len(proposal_bundle_records),
        "selected_proposal_bundle_count": len(selected_proposal_bundles),
        "partial_proposal_bundle_count": len(partially_selected_proposal_bundles),
        "proposal_bundle_atomicity_preserved": not partially_selected_proposal_bundles,
        "proposal_order": "dependency_closed_seed_capacity_groups_then_ranked_singletons",
        "label_load_ratios": load_ratios,
        "ranking": "moved_split_then_exchange_boundary_neighbors_then_continuous_load_and_mfs_pressure",
        "refinement_limit_policy": (
            "progressive_vcycle_growth_with_optional_explicit_cap"
            if variable_limit_override is not None or constraint_limit_override is not None
            else "explicit_override" if variable_limit is not None or constraint_limit is not None
            else "anytime_model_compact_actual_medium_envelope"
        ),
        "effective_variable_limit": variable_limit,
        "effective_constraint_limit": constraint_limit,
    })
    return selected, metadata


def solve_parity_level(
    anytime_model,
    atomic_model,
    base,
    parent,
    instance: dict,
    selected: list[int],
    hint: list[int],
    coefficients: dict[str, int],
    debt_text: str,
    policy: dict,
    stage_solver_seconds: float,
):
    """Match anytime_model: 120 solver seconds per objective, build time excluded.

    The balanced objective always runs.  Cut and Hop run only after the
    preceding objective is proven exact, with the exact value locked just as
    in anytime_model.  A later tie-break failure never discards an earlier exact-safe
    candidate from the same level.
    """

    level_started = time.perf_counter()
    locks: dict[str, int] = {}
    stage_hint = list(hint)
    best_labels = None
    best_values = None
    last_success = None
    objective_results = []
    for objective in ("mfs_balanced_score", "cut", "hop_numerator"):
        objective_started = time.perf_counter()
        model, variables, expressions = base.build_model(
            instance, selected, stage_hint, locks, objective, coefficients, debt_text
        )
        model_built = time.perf_counter()
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = stage_solver_seconds
        solver.parameters.num_search_workers = 1
        solver.parameters.random_seed = 237
        if policy["relative_gap_limit"]:
            solver.parameters.relative_gap_limit = policy["relative_gap_limit"]
        solve_started = time.perf_counter()
        status = solver.solve(model)
        solved = time.perf_counter()
        has_solution = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
        gap = (
            atomic_model.relative_gap(solver.objective_value, solver.best_objective_bound)
            if has_solution else None
        )
        certificate = anytime_model.certificate_name(
            has_solution, gap, policy["relative_gap_limit"]
        )
        row = {
            "objective": objective,
            "status": solver.status_name(status),
            "solver_status": solver.status_name(status),
            "certificate": certificate,
            "has_solution": has_solution,
            "qualified": certificate in ("OPTIMAL", "CERTIFIED_1PCT"),
            "proven_exact": certificate == "OPTIMAL",
            "relative_gap": gap,
            "relative_gap_limit": policy["relative_gap_limit"],
            "objective_value": int(round(solver.objective_value)) if has_solution else None,
            "best_bound": float(solver.best_objective_bound) if has_solution else None,
            "model_variables": len(model.proto.variables),
            "model_constraints": len(model.proto.constraints),
            "model_build_seconds": model_built - objective_started,
            "allocated_solver_seconds": stage_solver_seconds,
            "solver_wall_seconds": solved - solve_started,
            "wall_seconds": solved - objective_started,
            "representation": "compact_label_cost_table_active_hyperedge_signature_v2",
        }
        objective_results.append(row)
        if not has_solution:
            break

        labels = list(instance["current_labels"])
        for block in selected:
            chosen = [
                label for label in instance["domains"][block]
                if solver.boolean_value(variables[block, label])
            ]
            if len(chosen) != 1:
                raise RuntimeError((instance["name"], block, chosen))
            labels[block] = chosen[0]
        measured = base.metrics(instance, labels, coefficients)
        values = {
            name: int(solver.value(expression))
            for name, expression in expressions.items()
        }
        if any(measured[name] != values[name] for name in expressions):
            raise RuntimeError(f"vcycle_model compressed model metric mismatch {measured} {values}")
        expanded = base.expand_labels(instance, labels)
        exact = parent.evaluate(instance["dataset"], expanded)
        if exact["violation"] or exact["capacity_overflow"]:
            row.update({
                "status": "EXACT_HARD_CONSTRAINT_REJECTED",
                "exact_rejected": True,
                "feasible_exact_metrics": exact,
            })
            break
        exact_scored = dict(exact)
        exact_scored["mfs_balanced_score"] = base.score_value(exact, coefficients)
        row["feasible_metrics"] = measured
        row["feasible_exact_metrics"] = exact_scored
        row["feasible_assignment_hash"] = parent.assignment_hash(expanded)
        best_labels = labels
        best_values = values
        last_success = row
        if not row["proven_exact"]:
            break
        stage_hint = labels
        locks[objective] = values[objective]

    terminal = objective_results[-1]
    summary = {
        "objective": last_success["objective"] if last_success else terminal["objective"],
        "status": terminal["status"],
        "solver_status": terminal["solver_status"],
        "certificate": last_success["certificate"] if last_success else terminal["certificate"],
        "has_solution": best_labels is not None,
        "qualified": bool(last_success and last_success["qualified"]),
        "proven_exact": bool(last_success and last_success["proven_exact"]),
        "relative_gap": last_success["relative_gap"] if last_success else None,
        "relative_gap_limit": policy["relative_gap_limit"],
        "objective_value": last_success["objective_value"] if last_success else None,
        "best_bound": last_success["best_bound"] if last_success else None,
        "model_variables": max(row["model_variables"] for row in objective_results),
        "model_constraints": max(row["model_constraints"] for row in objective_results),
        "model_build_seconds": sum(row["model_build_seconds"] for row in objective_results),
        "allocated_solver_seconds_per_objective": stage_solver_seconds,
        "solver_budget_seconds": stage_solver_seconds * len(objective_results),
        "solver_wall_seconds": sum(row["solver_wall_seconds"] for row in objective_results),
        "wall_seconds": time.perf_counter() - level_started,
        "objectives_attempted": [row["objective"] for row in objective_results],
        "objective_results": objective_results,
        "representation": "compact_label_cost_table_active_hyperedge_signature_v2",
    }
    if last_success:
        for key in ("feasible_metrics", "feasible_exact_metrics", "feasible_assignment_hash"):
            summary[key] = last_success[key]
    return summary, best_labels, best_values


def exact_scored(parent, base, request, node_labels: list[int], coefficients: dict[str, int]) -> dict:
    exact = parent.evaluate(request, node_labels)
    exact = dict(exact)
    exact["mfs_balanced_score"] = base.score_value(exact, coefficients)
    return exact


def optimize(hierarchy_model, anytime_model, atomic_model, base, parent, request, membership: list[int], tag: str):
    legacy = parent.build_instance(request, membership, tag)
    atomic = atomic_model.build_atomic_instance(request, membership, tag, legacy)
    current = base.metrics(atomic, atomic["current_labels"])
    exact_current = parent.evaluate(request, request.labels)
    for name in base.STANDARD_METRICS + ("violation", "capacity_overflow"):
        if current[name] != exact_current[name]:
            raise RuntimeError(f"vcycle_model reconstruction mismatch {name}")
    coefficients = base.score_coefficients(current)
    current = base.metrics(atomic, atomic["current_labels"], coefficients)
    atomic["incumbent_metrics"] = current
    selected = [block for block, domain in enumerate(atomic["domains"]) if len(domain) > 1]
    estimate = atomic_model.estimate_model_size(atomic, selected)
    policy = atomic_model.model_policy(estimate)
    if policy["tier"] != "large_vcycle_deferred":
        labels, result = anytime_model.optimize(atomic_model, base, parent, request, membership, tag)
        result["vcycle_model_large_v14"] = {
            "status": "not_large",
            "active_hyperedge_signature": {
                key: estimate[key] for key in (
                    "raw_hyperedges", "signature_hyperedges", "presence_variables_saved"
                )
            },
        }
        return labels, result

    stage_solver_seconds = setting(
        "JARPART_VCYCLE_MODEL_LARGE_STAGE_SECONDS", DEFAULT_STAGE_SECONDS, float
    )
    if stage_solver_seconds <= 0.0:
        raise ValueError("JARPART_VCYCLE_MODEL_LARGE_STAGE_SECONDS must be positive")
    started = time.perf_counter()
    hierarchy, metadata = build_hierarchy(hierarchy_model, atomic_model, atomic, coefficients)
    if hierarchy is None:
        labels, result = anytime_model.optimize(atomic_model, base, parent, request, membership, tag)
        result["vcycle_model_large_v14"] = metadata
        result["accepted_source"] = "large_adaptive_unresolved"
        return labels, result

    best_atomic_labels = list(atomic["current_labels"])
    best_metrics = current
    best_debt = base.debt_diagnostic(current, current, base.debt_limit(atomic_model.DEBT_PCT))
    search_atomic_labels = list(atomic["current_labels"])
    search_metrics = current
    stages = []
    safe_candidates = 0
    exact_safe_candidates = 0
    search_candidates = 0
    global_best_updates = 0
    last_proposed_metrics = exact_scored(
        parent, base, request, list(request.labels), coefficients
    )
    last_proposed_hash = parent.assignment_hash(request.labels)
    last_proposed_source = "incumbent"
    chosen_coarse_index = None
    chosen_coarse_roots = None
    coarse_solver_solution_level_index = None
    refinement_anchor_source = None
    last_refinable_coarse_index = None
    last_refinable_coarse_roots = None
    incumbent_refinement_fallback = False
    chosen_coarse_rank = None
    coarse_solution_level_indices = []
    refinement_records = []
    refinement_opened_atomic = set()
    refinement_candidate_atomic = set()
    refinement_path_stopped_early = False
    growth_factor = None
    growth_reference_variables = None
    growth_reference_constraints = None

    retries = max(2, setting(
        "JARPART_VCYCLE_MODEL_COARSE_RETRIES", DEFAULT_COARSE_RETRIES, int
    ))
    coarse_records = select_coarse_retry_records(
        hierarchy, metadata["first_medium_index"], metadata["target_index"], retries,
    )
    for attempt, (level_index, record) in enumerate(coarse_records):
        stage_wall = stage_solver_seconds
        level, groups, _ = materialize_level(hierarchy_model, atomic, record["roots"])
        current_labels = project_atomic_labels(groups, atomic["current_labels"])
        level["current_labels"] = current_labels
        level["assignment"] = base.expand_labels(level, current_labels)
        level["incumbent_metrics"] = current
        level_selected = [
            block for block, domain in enumerate(level["domains"]) if len(domain) > 1
        ]
        row = {
            "phase": "coarse_acquisition",
            "attempt": attempt,
            "level_index": level_index,
            "families": len(groups),
            "selected_families": len(level_selected),
            "allocated_seconds": stage_wall,
            "corridor": {"kind": "full_coarse_model"},
        }
        level_policy = atomic_model.model_policy(atomic_model.estimate_model_size(level, level_selected))
        row["model_policy"] = level_policy
        if level_policy["tier"] == "large_vcycle_deferred":
            row.update({"status": "SKIPPED_COARSE_STILL_LARGE", "has_solution": False})
            stages.append(row)
            continue
        # Even when CP-SAT cannot find a new coarse incumbent, the projection
        # of the exact atomic incumbent is a certified feasible state at this
        # level.  Remember the deepest solvable representation so the reserved
        # refinement budget can still search a bounded fine-level corridor.
        last_refinable_coarse_index = level_index
        last_refinable_coarse_roots = record["roots"]
        stage, labels, _ = solve_parity_level(
            anytime_model, atomic_model, base, parent, level, level_selected, current_labels,
            coefficients, atomic_model.DEBT_PCT, level_policy, stage_solver_seconds,
        )
        row.update(stage)
        if labels is not None:
            candidate_atomic = atomic_labels_from_level(
                groups, labels, len(atomic["block_sizes"])
            )
            candidate_nodes = base.expand_labels(atomic, candidate_atomic)
            candidate = exact_scored(parent, base, request, candidate_nodes, coefficients)
            debt = base.debt_diagnostic(
                current, candidate, base.debt_limit(atomic_model.DEBT_PCT)
            )
            strict_safe = not candidate["violation"] and not candidate["capacity_overflow"]
            exact_safe_candidates += int(strict_safe)
            search_eligible = strict_safe and debt["accepted"]
            globally_improved = (
                base.policy_key(candidate, atomic_model.OBJECTIVE_MODE)
                < base.policy_key(best_metrics, atomic_model.OBJECTIVE_MODE)
            )
            changed_atomic = sum(
                left != right
                for left, right in zip(candidate_atomic, atomic["current_labels"])
            )
            candidate_rank = (
                base.policy_key(candidate, atomic_model.OBJECTIVE_MODE),
                -changed_atomic,
                level_index,
            )
            anchor_preferred = search_eligible and (
                chosen_coarse_rank is None or candidate_rank < chosen_coarse_rank
            )
            row.update({
                "candidate_exact_metrics": candidate,
                "debt_diagnostic": debt,
                "candidate_changed_atomic_families": changed_atomic,
                "search_state_eligible": search_eligible,
                "coarse_anchor_preferred": anchor_preferred,
                "global_policy_improved": globally_improved,
            })
            last_proposed_metrics = candidate
            last_proposed_hash = parent.assignment_hash(candidate_nodes)
            last_proposed_source = f"coarse_level_{level_index}"
            if search_eligible:
                search_candidates += 1
                coarse_solution_level_indices.append(level_index)
                if anchor_preferred:
                    chosen_coarse_rank = candidate_rank
                    search_atomic_labels = candidate_atomic
                    search_metrics = candidate
                    chosen_coarse_index = level_index
                    chosen_coarse_roots = record["roots"]
                    coarse_solver_solution_level_index = level_index
                    refinement_anchor_source = "best_coarse_solver_candidate_after_retry_pool"
                if globally_improved:
                    best_atomic_labels = candidate_atomic
                    best_metrics = candidate
                    best_debt = debt
                    safe_candidates += 1
                    global_best_updates += 1
        stages.append(row)

    if (
        chosen_coarse_index is None
        and last_refinable_coarse_index is not None
    ):
        # UNKNOWN/NO_SOLUTION is not evidence that the exact incumbent is a
        # poor refinement anchor.  Project it through the deepest compatible
        # coarse map and spend the mandatory reserve on the fine corridor.
        # This remains an anytime fallback: only an exactly safe, globally
        # policy-improving refined assignment may replace the incumbent.
        chosen_coarse_index = last_refinable_coarse_index
        chosen_coarse_roots = last_refinable_coarse_roots
        refinement_anchor_source = "exact_atomic_incumbent_projected"
        incumbent_refinement_fallback = True
        stages.append({
            "phase": "coarse_anchor",
            "level_index": chosen_coarse_index,
            "status": "EXACT_INCUMBENT_REFINEMENT_FALLBACK",
            "solver_status": "NO_COARSE_SOLUTION",
            "certificate": "EXACT_INCUMBENT",
            "has_solution": True,
            "solver_has_solution": False,
            "qualified": True,
            "proven_exact": True,
            "candidate_exact_metrics": current,
            "candidate_assignment_hash": parent.assignment_hash(request.labels),
        })

    if chosen_coarse_index is not None:
        max_refinement_setting = setting(
            "JARPART_VCYCLE_MODEL_MAX_REFINEMENT_LEVELS",
            DEFAULT_MAX_REFINEMENT_LEVELS,
            int,
        )
        max_refinement = (
            len(hierarchy)
            if max_refinement_setting <= 0
            else max_refinement_setting
        )
        refinement_records = select_refinement_records(
            hierarchy, chosen_coarse_index, max_refinement,
        )
        coarse_reference_rows = [
            row for row in stages
            if row.get("phase") == "coarse_acquisition"
            and row.get("level_index") == chosen_coarse_index
        ]
        if not coarse_reference_rows:
            raise RuntimeError("vcycle_model missing progressive-growth coarse reference")
        coarse_reference = coarse_reference_rows[-1]
        growth_reference_variables = coarse_reference.get(
            "model_variables", coarse_reference["model_policy"]["variables_upper"]
        )
        growth_reference_constraints = coarse_reference.get(
            "model_constraints", coarse_reference["model_policy"]["constraints_upper"]
        )
        medium_limits = coarse_reference["model_policy"]["medium_limits"]
        growth_factor = setting(
            "JARPART_VCYCLE_MODEL_REFINEMENT_GROWTH_FACTOR",
            DEFAULT_REFINEMENT_GROWTH_FACTOR,
            float,
        )
        coarse_roots = chosen_coarse_roots
        for order, (level_index, record) in enumerate(refinement_records):
            stage_started = time.perf_counter()
            stage_wall = stage_solver_seconds
            level, groups, _ = materialize_level(hierarchy_model, atomic, record["roots"])
            current_labels = project_atomic_labels(groups, search_atomic_labels)
            level["current_labels"] = current_labels
            level["assignment"] = base.expand_labels(level, current_labels)
            level["incumbent_metrics"] = current
            progressive_variable_limit, progressive_constraint_limit = (
                progressive_refinement_limits(
                    growth_reference_variables,
                    growth_reference_constraints,
                    medium_limits,
                    growth_factor,
                )
            )
            level_selected, corridor = select_refinement_corridor(
                atomic_model, level, groups, coarse_roots, current_labels,
                atomic["current_labels"], coefficients, None,
                progressive_variable_limit,
                progressive_constraint_limit,
            )
            candidate_atomic_ids = {
                atom
                for block in corridor.get("candidate_family_ids", [])
                for atom in groups[block]
            }
            selected_atomic_ids = {
                atom for block in level_selected for atom in groups[block]
            }
            refinement_candidate_atomic.update(candidate_atomic_ids)
            refinement_opened_atomic.update(selected_atomic_ids)
            movable_weight = sum(
                atomic["block_sizes"][block] for block in selected
            )
            selected_atomic_weight = sum(
                atomic["block_sizes"][block] for block in selected_atomic_ids
            )
            candidate_atomic_weight = sum(
                atomic["block_sizes"][block] for block in candidate_atomic_ids
            )
            corridor.update({
                "candidate_atomic_families": len(candidate_atomic_ids),
                "selected_atomic_families": len(selected_atomic_ids),
                "candidate_atomic_weight": candidate_atomic_weight,
                "selected_atomic_weight": selected_atomic_weight,
                "movable_atomic_weight": movable_weight,
                "selected_atomic_weight_ratio": (
                    selected_atomic_weight / movable_weight if movable_weight else 0.0
                ),
                "growth_factor": growth_factor,
                "growth_reference_variables": growth_reference_variables,
                "growth_reference_constraints": growth_reference_constraints,
                "progressive_variable_limit": progressive_variable_limit,
                "progressive_constraint_limit": progressive_constraint_limit,
            })
            estimation_seconds = time.perf_counter() - stage_started
            corridor["kind"] = (
                "incumbent_pressure_boundary_corridor"
                if incumbent_refinement_fallback
                else "moved_exchange_boundary_corridor"
            )
            row = {
                "phase": "refinement",
                "refinement_order": order,
                "level_index": level_index,
                "families": len(groups),
                "selected_families": len(level_selected),
                "allocated_seconds": stage_wall,
                "estimation_and_materialization_seconds": estimation_seconds,
                "corridor": corridor,
            }
            if not level_selected:
                row.update({"status": "SKIPPED_EMPTY_CORRIDOR", "has_solution": False})
                stages.append(row)
                coarse_roots = record["roots"]
                continue
            level_policy = atomic_model.model_policy(atomic_model.estimate_model_size(level, level_selected))
            row["model_policy"] = level_policy
            if level_policy["tier"] == "large_vcycle_deferred":
                row.update({"status": "SKIPPED_CORRIDOR_STILL_LARGE", "has_solution": False})
                stages.append(row)
                coarse_roots = record["roots"]
                continue
            stage, labels, _ = solve_parity_level(
                anytime_model, atomic_model, base, parent, level, level_selected, current_labels,
                coefficients, atomic_model.DEBT_PCT, level_policy,
                stage_solver_seconds,
            )
            row.update(stage)
            if labels is not None:
                growth_reference_variables = row["model_variables"]
                growth_reference_constraints = row["model_constraints"]
                candidate_atomic = atomic_labels_from_level(
                    groups, labels, len(atomic["block_sizes"])
                )
                changed_from_incumbent = sum(
                    left != right
                    for left, right in zip(candidate_atomic, atomic["current_labels"])
                )
                changed_from_search = sum(
                    left != right
                    for left, right in zip(candidate_atomic, search_atomic_labels)
                )
                candidate_nodes = base.expand_labels(atomic, candidate_atomic)
                candidate = exact_scored(parent, base, request, candidate_nodes, coefficients)
                debt = base.debt_diagnostic(
                    current, candidate, base.debt_limit(atomic_model.DEBT_PCT)
                )
                strict_safe = not candidate["violation"] and not candidate["capacity_overflow"]
                exact_safe_candidates += int(strict_safe)
                search_not_worse = (
                    base.policy_key(candidate, atomic_model.OBJECTIVE_MODE)
                    <= base.policy_key(search_metrics, atomic_model.OBJECTIVE_MODE)
                )
                # A refinement state may move non-monotonically inside the
                
                # best can reach the helper output, so final acceptance remains
                # strict-safe and policy-improving.
                search_eligible = strict_safe and debt["accepted"]
                globally_improved = (
                    base.policy_key(candidate, atomic_model.OBJECTIVE_MODE)
                    < base.policy_key(best_metrics, atomic_model.OBJECTIVE_MODE)
                )
                row.update({
                    "candidate_exact_metrics": candidate,
                    "candidate_changed_atomic_families": changed_from_incumbent,
                    "candidate_changed_from_search_atomic_families": changed_from_search,
                    "debt_diagnostic": debt,
                    "search_state_not_worse": search_not_worse,
                    "search_transition_policy": "strict_safe_5pct_debt_nonmonotone",
                    "search_state_eligible": search_eligible,
                    "global_policy_improved": globally_improved,
                })
                last_proposed_metrics = candidate
                last_proposed_hash = parent.assignment_hash(candidate_nodes)
                last_proposed_source = f"refinement_level_{level_index}"
                if search_eligible:
                    search_atomic_labels = candidate_atomic
                    search_metrics = candidate
                    search_candidates += 1
                    if globally_improved:
                        best_atomic_labels = candidate_atomic
                        best_metrics = candidate
                        best_debt = debt
                        safe_candidates += 1
                        global_best_updates += 1
            stages.append(row)
            coarse_roots = record["roots"]

    expanded = base.expand_labels(atomic, best_atomic_labels)
    exact = exact_scored(parent, base, request, expanded, coefficients)
    final_atomic_rollback = False
    if exact["violation"] or exact["capacity_overflow"]:
        best_atomic_labels = list(atomic["current_labels"])
        expanded = list(request.labels)
        exact = exact_scored(parent, base, request, expanded, coefficients)
        final_atomic_rollback = True
    accepted = best_atomic_labels != atomic["current_labels"]
    large_wall_seconds = time.perf_counter() - started
    objective_stage_count = sum(
        len(row.get("objective_results", [])) for row in stages
    )
    solver_wall_seconds = sum(
        objective.get("solver_wall_seconds", 0.0)
        for row in stages
        for objective in row.get("objective_results", [])
    )
    refinement_rows = [row for row in stages if row.get("phase") == "refinement"]
    refinement_path_complete = (
        chosen_coarse_index is not None
        and len(refinement_rows) == len(refinement_records)
        and not refinement_path_stopped_early
    )
    refinement_opened_ids = sorted(refinement_opened_atomic)
    refinement_candidate_ids = sorted(refinement_candidate_atomic)
    movable_weight = sum(atomic["block_sizes"][block] for block in selected)
    refinement_opened_weight = sum(
        atomic["block_sizes"][block] for block in refinement_opened_ids
    )
    refinement_candidate_weight = sum(
        atomic["block_sizes"][block] for block in refinement_candidate_ids
    )
    accepted_changed_atomic_ids = [
        block for block, (before_label, after_label) in enumerate(
            zip(atomic["current_labels"], best_atomic_labels)
        )
        if before_label != after_label
    ]
    accepted_changed_atomic_weight = sum(
        atomic["block_sizes"][block] for block in accepted_changed_atomic_ids
    )
    metadata.update({
        "budget_mode": "anytime_model_per_objective_solver_stage_parity",
        "stage_solver_seconds": stage_solver_seconds,
        "model_build_time_counted_against_solver_budget": False,
        "objective_stage_count": objective_stage_count,
        "nominal_solver_budget_seconds": objective_stage_count * stage_solver_seconds,
        "solver_wall_seconds": solver_wall_seconds,
        "large_wall_seconds": large_wall_seconds,
        "shared_total_seconds": None,
        "shared_budget_policy": "removed_no_cross_level_shared_deadline",
        "coarse_retry_count": len(coarse_records),
        "coarse_solution_level_indices": coarse_solution_level_indices,
        "coarse_candidates_considered_before_refinement": len(coarse_solution_level_indices),
        "coarse_solution_level_index": coarse_solver_solution_level_index,
        "refinement_anchor_level_index": chosen_coarse_index,
        "refinement_anchor_source": refinement_anchor_source,
        "incumbent_refinement_fallback": incumbent_refinement_fallback,
        "solve_objectives_per_level": [
            "mfs_balanced_score", "cut_if_previous_exact", "hop_if_previous_exact"
        ],
        "internal_search_policy": "strict_safe_5pct_debt_nonmonotone_search_global_best_guard",
        "refinement_growth_policy": "previous_solved_model_geometric_growth_capped_by_anytime_model_medium",
        "refinement_growth_factor": growth_factor,
        "final_growth_reference_variables": growth_reference_variables,
        "final_growth_reference_constraints": growth_reference_constraints,
        "refinement_levels_planned": len(refinement_records),
        "refinement_levels_recorded": len(refinement_rows),
        "refinement_path_stopped_early": refinement_path_stopped_early,
        "refinement_path_complete": refinement_path_complete,
        "refinement_candidate_atomic_families": len(refinement_candidate_ids),
        "refinement_opened_atomic_families": len(refinement_opened_ids),
        "refinement_candidate_atomic_weight": refinement_candidate_weight,
        "refinement_opened_atomic_weight": refinement_opened_weight,
        "movable_atomic_weight": movable_weight,
        "refinement_opened_atomic_weight_ratio": (
            refinement_opened_weight / movable_weight if movable_weight else 0.0
        ),
        "exact_safe_stage_candidates": exact_safe_candidates,
        "global_best_updates": global_best_updates,
        "accepted_changed_atomic_families": len(accepted_changed_atomic_ids),
        "accepted_changed_atomic_weight": accepted_changed_atomic_weight,
        "final_atomic_rollback": final_atomic_rollback,
        "active_hyperedge_signature": {
            key: estimate[key] for key in (
                "raw_hyperedges", "signature_hyperedges", "collapsed_hyperedges",
                "raw_presence_variables", "compressed_presence_variables",
                "presence_variables_saved",
            )
        },
    })
    source = "large_progressive_vcycle_growth_v14_safe_anytime" if accepted else "incumbent"
    return expanded, {
        "tag": tag,
        "nodes": request.n,
        "blocks": len(atomic["block_sizes"]),
        "movable_blocks": len(selected),
        "selected_blocks": len(refinement_opened_ids),
        "selected_block_ids": refinement_opened_ids,
        "selected_blocks_semantics": "atomic_families_actually_opened_by_refinement_union",
        "accepted_changed_atomic_family_ids": accepted_changed_atomic_ids,
        "vcycle_model_large_v14": metadata,
        "anytime_model_policy": {
            "detector": atomic_model.DETECTOR,
            "scope": "global_coarse_via_adaptive_reversible_multilevel_plus_local_refinement",
            "objective": atomic_model.OBJECTIVE_MODE,
            "debt_pct": atomic_model.DEBT_PCT,
            "uncertified_acceptance": "exact_safe_policy_improving",
            "stage_seconds": stage_solver_seconds,
            "large_path": "stage_parity_all_levels_progressive_growth_topology_first_no_synthetic_chain_dependency_closed_corridor_v14",
        },
        "scope_policy": {
            "name": "large_global_coarse_plus_actual_refinement_union",
            "atom_limit": None,
            "total_movable_families": len(selected),
            "coarse_scope_families": len(selected),
            "refinement_candidate_families": len(refinement_candidate_ids),
            "selected_families": len(refinement_opened_ids),
            "selected_atoms": len(refinement_opened_ids),
            "skipped_families": len(selected) - len(refinement_opened_ids),
            "partial_selected_families": 0,
            "single_move_prefilter": False,
            "exchange_bundle_atomicity": "whole_capacity_deficit_partner_group_boundaries",
            "target_domain": "all_fpga_labels_except_fixed",
        },
        "model_policy": policy,
        "atomic_family_keys": [list(key) for key in atomic["atomic_family_keys"]],
        "objective_mode": atomic_model.OBJECTIVE_MODE,
        "objective_order": ["mfs_balanced_score", "cut", "hop_numerator"],
        "score_policy": {
            "name": "incumbent_normalized_mfs_two_family_v1",
            "scale": base.SCORE_SCALE,
            "weights": {"connectivity": 0.50, "cut": 0.25, "hop_numerator": 0.25},
            "coefficients": coefficients,
        },
        "debt_policy": {"name": atomic_model.DEBT_PCT, **best_debt},
        "debt_enforced_in_model": True,
        "incumbent_metrics": current,
        "incumbent_assignment_hash": parent.assignment_hash(request.labels),
        "cpsat_metrics": best_metrics,
        "cpsat_metrics_source": "global_best_exact_stage_candidate",
        "proposed_source": last_proposed_source,
        "proposed_metrics": last_proposed_metrics,
        "proposed_assignment_hash": last_proposed_hash,
        "accepted_source": source,
        "accepted_metrics": exact,
        "changed": accepted,
        "safe_candidates": safe_candidates,
        "exact_safe_candidates": exact_safe_candidates,
        "global_best_updates": global_best_updates,
        "search_candidates": search_candidates,
        "stopped_after": None,
        "stages_complete": refinement_path_complete,
        "stage_results": stages,
        "assignment_hash": parent.assignment_hash(expanded),
    }
