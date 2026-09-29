"""Repair closure."""
#!/usr/bin/env python3
from __future__ import annotations

from collections import Counter

from repair_local import Request, adjacency, strict_domains, violating_core


MAX_ADAPTIVE_CLOSURE_NODES = 512
OPTIONAL_TARGET_NODES = 448
FRONTIER_DEPTH = 2
FRONTIER_PER_LEVEL = 2048


def _edge_weight(request: Request, left: int, right: int) -> int:
    return int(request.pair_weights[min(left, right), max(left, right)])


def _fixed_compatible_labels(
    request: Request,
    node: int,
    fixed: dict[int, int],
    graph: list[list[int]],
) -> list[int]:
    fixed_neighbors = [other for other in graph[node] if other in fixed]
    return [
        label
        for label in range(request.k)
        if all(
            request.distances[label][fixed[other]] <= request.hmax
            for other in fixed_neighbors
        )
    ]


def _repair_empty_domains(
    request: Request,
    fixed: dict[int, int],
    graph: list[list[int]],
    movable: set[int],
    depth: dict[int, int],
    max_nodes: int,
) -> dict:
    additions: list[dict] = []
    iterations = 0
    while True:
        domains = strict_domains(request, movable, graph)
        empty = [node for node in sorted(movable) if not domains[node]]
        if not empty:
            return {
                "ok": True,
                "domains": domains,
                "empty_nodes": [],
                "fixed_blocked_empty_nodes": [],
                "cap_exhausted": False,
                "iterations": iterations,
                "blocker_additions": additions,
            }
        iterations += 1
        requested: dict[int, tuple[int, int]] = {}
        fixed_blocked: list[int] = []
        for node in empty:
            choices = []
            for label in range(request.k):
                outside = [other for other in graph[node] if other not in movable]
                fixed_blockers = [
                    other
                    for other in outside
                    if other in fixed
                    and request.distances[label][fixed[other]] > request.hmax
                ]
                if fixed_blockers:
                    continue
                blockers = sorted(
                    other
                    for other in outside
                    if other not in fixed
                    and request.distances[label][request.labels[other]] > request.hmax
                )
                choices.append((
                    len(blockers),
                    sum(request.node_weights[other] for other in blockers),
                    request.distances[request.labels[node]][label],
                    label,
                    blockers,
                ))
            if not choices:
                fixed_blocked.append(node)
                continue
            _, _, _, chosen_label, blockers = min(choices)
            for blocker in blockers:
                source = requested.get(blocker)
                candidate = (depth.get(node, 0) + 1, node)
                if source is None or candidate < source:
                    requested[blocker] = candidate
            additions.append({
                "empty_node": node,
                "chosen_label": chosen_label,
                "blockers": blockers,
            })
        if fixed_blocked:
            return {
                "ok": False,
                "domains": domains,
                "empty_nodes": empty,
                "fixed_blocked_empty_nodes": fixed_blocked,
                "cap_exhausted": False,
                "iterations": iterations,
                "blocker_additions": additions,
            }
        new_nodes = sorted(set(requested) - movable)
        if not new_nodes:
            return {
                "ok": False,
                "domains": domains,
                "empty_nodes": empty,
                "fixed_blocked_empty_nodes": [],
                "cap_exhausted": False,
                "iterations": iterations,
                "blocker_additions": additions,
            }
        if len(movable) + len(new_nodes) > max_nodes:
            return {
                "ok": False,
                "domains": domains,
                "empty_nodes": empty,
                "fixed_blocked_empty_nodes": [],
                "cap_exhausted": True,
                "requested_nodes_at_failure": len(movable) + len(new_nodes),
                "iterations": iterations,
                "blocker_additions": additions,
            }
        for node in new_nodes:
            movable.add(node)
            depth[node] = requested[node][0]


def _frontier_order(
    request: Request,
    fixed: dict[int, int],
    graph: list[list[int]],
    seed: set[int],
) -> list[tuple[int, int, int]]:
    order: list[tuple[int, int, int]] = []
    discovered = set(seed) | set(fixed)
    layer = set(seed)
    for level in range(1, FRONTIER_DEPTH + 1):
        scores: Counter[int] = Counter()
        for node in layer:
            for other in graph[node]:
                if other in discovered:
                    continue
                scores[other] += _edge_weight(request, node, other)
        discovered.update(scores)
        ranked = [
            node
            for node, _score in sorted(
                scores.items(), key=lambda item: (-item[1], item[0])
            )
            if _fixed_compatible_labels(request, node, fixed, graph)
        ][:FRONTIER_PER_LEVEL]
        order.extend((node, level, int(scores[node])) for node in ranked)
        layer = set(ranked)
        if not layer:
            break
    return order


def adaptive_closure(
    request: Request,
    fixed: dict[int, int],
    max_nodes: int = MAX_ADAPTIVE_CLOSURE_NODES,
) -> dict:
    graph = adjacency(request)
    core = violating_core(request) - fixed.keys()
    base_movable = set(core)
    base_depth = {node: 0 for node in core}
    base_repair = _repair_empty_domains(
        request, fixed, graph, base_movable, base_depth, max_nodes
    )
    if not base_repair["ok"]:
        return {
            "movable": sorted(base_movable),
            "domains": base_repair["domains"],
            "empty_nodes": base_repair["empty_nodes"],
            "bounded": not base_repair["cap_exhausted"],
            "one_hop_additions": sorted(
                node for node in base_movable - core if base_depth.get(node, 0) == 1
            ),
            "diagnostics": {
                "policy": "CLOSURE_MODEL_ADAPTIVE_BLOCKER_FRONTIER_V1",
                "max_nodes": max_nodes,
                "core_nodes": len(core),
                "base_nodes": len(base_movable),
                "optional_frontier_candidates": 0,
                "optional_frontier_committed": 0,
                **{key: value for key, value in base_repair.items() if key not in {"ok", "domains"}},
            },
        }

    frontier = _frontier_order(request, fixed, graph, base_movable)
    optional_limit = min(
        len(frontier),
        max(0, min(OPTIONAL_TARGET_NODES, max_nodes) - len(base_movable)),
    )

    def build(prefix: int) -> tuple[set[int], dict[int, int], dict]:
        movable = set(base_movable)
        depth = dict(base_depth)
        for node, level, _score in frontier[:prefix]:
            movable.add(node)
            depth[node] = level
        repaired = _repair_empty_domains(
            request, fixed, graph, movable, depth, max_nodes
        )
        return movable, depth, repaired

    best_movable, best_depth, best_repair = build(0)
    if optional_limit:
        trial_movable, trial_depth, trial_repair = build(optional_limit)
        if trial_repair["ok"]:
            best_movable, best_depth, best_repair = (
                trial_movable, trial_depth, trial_repair
            )
            committed = optional_limit
        else:
            committed = 0
            low, high = 1, optional_limit - 1
            while low <= high:
                middle = (low + high) // 2
                trial_movable, trial_depth, trial_repair = build(middle)
                if trial_repair["ok"]:
                    best_movable, best_depth, best_repair = (
                        trial_movable, trial_depth, trial_repair
                    )
                    committed = middle
                    low = middle + 1
                else:
                    high = middle - 1
    else:
        committed = 0

    additions = best_movable - core
    diagnostics = {
        "policy": "CLOSURE_MODEL_ADAPTIVE_BLOCKER_FRONTIER_V1",
        "max_nodes": max_nodes,
        "optional_target_nodes": OPTIONAL_TARGET_NODES,
        "frontier_depth": FRONTIER_DEPTH,
        "frontier_per_level": FRONTIER_PER_LEVEL,
        "core_nodes": len(core),
        "base_nodes": len(base_movable),
        "final_nodes": len(best_movable),
        "optional_frontier_candidates": len(frontier),
        "optional_frontier_requested": optional_limit,
        "optional_frontier_committed": committed,
        "blocker_added_nodes": len(best_movable - (core | {row[0] for row in frontier[:committed]})),
        "max_observed_depth": max(best_depth.values(), default=0),
        "cap_exhausted": bool(best_repair["cap_exhausted"]),
        "fixed_blocked_empty_nodes": best_repair["fixed_blocked_empty_nodes"],
        "empty_nodes": best_repair["empty_nodes"],
        "domain_repair_iterations": best_repair["iterations"],
        "frontier_committed_by_depth": {
            str(level): sum(1 for _node, item_level, _score in frontier[:committed] if item_level == level)
            for level in range(1, FRONTIER_DEPTH + 1)
        },
    }
    return {
        "movable": sorted(best_movable),
        "domains": best_repair["domains"],
        "empty_nodes": best_repair["empty_nodes"],
        "bounded": len(best_movable) <= max_nodes and not best_repair["cap_exhausted"],
        "one_hop_additions": sorted(
            node for node in additions if best_depth.get(node, 0) == 1
        ),
        "adaptive_additions": sorted(additions),
        "diagnostics": diagnostics,
    }
