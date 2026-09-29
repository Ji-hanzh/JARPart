"""Partition."""
#!/usr/bin/env python3

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass


SELECTED_BLOCKS = 48
ORDER = ("connectivity", "cut", "hop_numerator")


@dataclass
class Request:
    n: int
    k: int
    capacity: int
    hmax: int
    distances: list[list[int]]
    node_weights: list[int]
    labels: list[int]
    pair_weights: Counter[tuple[int, int]]
    hyperedges: list[tuple[int, int, tuple[int, ...]]]


def assignment_hash(values: list[int]) -> int:
    result = 1469598103934665603
    for value in values:
        result ^= value + 1
        result = (result * 1099511628211) & ((1 << 64) - 1)
    return result


def evaluate(request: Request, labels: list[int]) -> dict:
    if len(labels) != request.n or any(not 0 <= label < request.k for label in labels):
        raise RuntimeError("invalid assignment")
    cut = sum(weight for (left, right), weight in request.pair_weights.items() if labels[left] != labels[right])
    violation = sum(
        weight
        for (left, right), weight in request.pair_weights.items()
        if request.distances[labels[left]][labels[right]] > request.hmax
    )
    connectivity = 0
    hop_numerator = 0
    hop_count = 0
    for weight, source, drains in request.hyperedges:
        blocks = {labels[source], *(labels[drain] for drain in drains)}
        connectivity += weight * (len(blocks) - 1)
        for drain in drains:
            hop_numerator += weight * request.distances[labels[source]][labels[drain]]
            hop_count += weight
    loads = [0] * request.k
    for node, label in enumerate(labels):
        loads[label] += request.node_weights[node]
    return {
        "cut": cut,
        "connectivity": connectivity,
        "hop_numerator": hop_numerator,
        "hop": hop_numerator / hop_count if hop_count else 0.0,
        "violation": violation,
        "capacity_overflow": sum(max(load - request.capacity, 0) for load in loads),
        "loads": loads,
    }


def compatible_masks(request: Request) -> list[int]:
    masks = []
    for target in range(request.k):
        mask = 0
        for label in range(request.k):
            if request.distances[target][label] <= request.hmax:
                mask |= 1 << label
        masks.append(mask)
    return masks


def bits(mask: int):
    while mask:
        bit = mask & -mask
        yield bit.bit_length() - 1
        mask ^= bit


def build_instance(request: Request, membership: list[int], tag: str) -> dict:
    current = evaluate(request, request.labels)
    if current["violation"] or current["capacity_overflow"]:
        raise RuntimeError("optimizer called on infeasible incumbent")
    compatibility = compatible_masks(request)
    node_neighbor_masks = [0] * request.n
    for left, right in request.pair_weights:
        node_neighbor_masks[left] |= 1 << request.labels[right]
        node_neighbor_masks[right] |= 1 << request.labels[left]

    signatures = []
    topology_cache = {}
    for node, current_label in enumerate(request.labels):
        neighbor_mask = node_neighbor_masks[node]
        topology = topology_cache.get(neighbor_mask)
        if topology is None:
            topology = 0
            for target, allowed in enumerate(compatibility):
                if not neighbor_mask & ~allowed:
                    topology |= 1 << target
            topology_cache[neighbor_mask] = topology
        capacity_mask = 0
        for target in range(request.k):
            if current["loads"][target] + request.node_weights[node] <= request.capacity:
                capacity_mask |= 1 << target
        signatures.append((topology & capacity_mask) | (1 << current_label))

    key_to_block = {}
    block_of_node = [0] * request.n
    block_sizes = []
    block_labels = []
    block_signatures = []
    for node, key in enumerate(zip(membership, request.labels, signatures)):
        block = key_to_block.get(key)
        if block is None:
            block = len(block_sizes)
            key_to_block[key] = block
            block_sizes.append(0)
            block_labels.append(key[1])
            block_signatures.append(key[2])
        block_sizes[block] += request.node_weights[node]
        block_of_node[node] = block

    block_neighbor_masks = [0] * len(block_sizes)
    for left, right in request.pair_weights:
        left_block, right_block = block_of_node[left], block_of_node[right]
        if left_block != right_block:
            block_neighbor_masks[left_block] |= 1 << request.labels[right]
            block_neighbor_masks[right_block] |= 1 << request.labels[left]
    domains = []
    for block, (size, current_label, signature) in enumerate(zip(block_sizes, block_labels, block_signatures)):
        domain = 1 << current_label
        for target in bits(signature):
            if target == current_label:
                continue
            if current["loads"][target] + size > request.capacity:
                continue
            if block_neighbor_masks[block] & ~compatibility[target]:
                continue
            domain |= 1 << target
        domains.append(list(bits(domain)))

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
    instance = {
        "dataset": request,
        "name": tag,
        "assignment": request.labels,
        "distances": request.distances,
        "k": request.k,
        "capacity": request.capacity,
        "block_of_node": block_of_node,
        "block_sizes": block_sizes,
        "current_labels": block_labels,
        "domains": domains,
        "pair_weights": pair_weights,
        "directed_weights": directed_weights,
        "hyperedge_weights": hyperedge_weights,
        "hop_count": hop_count,
        "node_metrics": current,
    }
    return instance
