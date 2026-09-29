"""Repair local."""
#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from ortools.sat.python import cp_model


BETA = 10_000
GAMMA = 15
MAX_CLOSURE_NODES = 256


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


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_request(path: Path) -> Request:
    with Path(path).open(encoding="utf-8") as handle:
        header = handle.readline().split()
        if len(header) != 7 or header[0] != "ITER240_REQUEST_V1":
            raise RuntimeError(f"invalid request header: {header}")
        n, k, capacity, hmax, expected_pairs, expected_hyperedges = map(int, header[1:])
        distances = [[-1] * k for _ in range(k)]
        node_weights = [-1] * n
        labels = [-1] * n
        pairs: Counter[tuple[int, int]] = Counter()
        hyperedges: list[tuple[int, int, tuple[int, ...]]] = []
        seen_d = seen_n = 0
        for raw in handle:
            fields = raw.split()
            if not fields:
                continue
            if fields[0] == "D":
                row = int(fields[1]); values = list(map(int, fields[2:]))
                if not 0 <= row < k or len(values) != k:
                    raise RuntimeError(f"bad distance row: {raw.rstrip()}")
                distances[row] = values; seen_d += 1
            elif fields[0] == "N":
                node, weight, label = map(int, fields[1:])
                if not 0 <= node < n or weight <= 0 or not 0 <= label < k:
                    raise RuntimeError(f"bad node row: {raw.rstrip()}")
                node_weights[node] = weight; labels[node] = label; seen_n += 1
            elif fields[0] == "P":
                left, right, weight = map(int, fields[1:])
                if not 0 <= left < right < n or weight <= 0:
                    raise RuntimeError(f"bad pair row: {raw.rstrip()}")
                pairs[left, right] += weight
            elif fields[0] == "H":
                weight, source, count = map(int, fields[1:4])
                drains = tuple(map(int, fields[4:]))
                if weight <= 0 or not 0 <= source < n or len(drains) != count:
                    raise RuntimeError(f"bad hyperedge row: {raw.rstrip()}")
                hyperedges.append((weight, source, drains))
            else:
                raise RuntimeError(f"unknown request row: {raw.rstrip()}")
    if seen_d != k or seen_n != n or len(pairs) != expected_pairs or len(hyperedges) != expected_hyperedges:
        raise RuntimeError("incomplete request")
    if any(value < 0 for row in distances for value in row):
        raise RuntimeError("incomplete distance matrix")
    return Request(n, k, capacity, hmax, distances, node_weights, labels, pairs, hyperedges)


def read_fixed(path: Path, n: int) -> dict[int, int]:
    with Path(path).open(encoding="utf-8") as handle:
        header = handle.readline().split()
        if len(header) != 3 or header[0] != "ITER247_FIXED_V1":
            raise RuntimeError(f"invalid fixed header: {header}")
        expected_n, expected_count = map(int, header[1:])
        if expected_n != n:
            raise RuntimeError(f"fixed n mismatch: {expected_n}/{n}")
        fixed = {int(node): int(label) for node, label in (line.split() for line in handle if line.strip())}
    if len(fixed) != expected_count:
        raise RuntimeError(f"fixed count mismatch: {len(fixed)}/{expected_count}")
    return fixed


def evaluate(request: Request, labels: list[int], gamma: int = GAMMA) -> dict:
    if len(labels) != request.n or any(not 0 <= label < request.k for label in labels):
        raise RuntimeError("invalid assignment")
    cut = violation = weighted_violation_shape10 = legal_reward10 = 0
    for (left, right), weight in request.pair_weights.items():
        distance = request.distances[labels[left]][labels[right]]
        cut += weight * int(distance >= 1)
        if distance > request.hmax:
            violation += weight
            weighted_violation_shape10 += weight * (10 + distance - request.hmax)
        else:
            legal_reward10 += 10 * weight * (request.hmax - distance)
    connectivity = hop_numerator = hop_count = 0
    for weight, source, drains in request.hyperedges:
        blocks = {labels[source], *(labels[drain] for drain in drains)}
        connectivity += weight * (len(blocks) - 1)
        for drain in drains:
            hop_numerator += weight * request.distances[labels[source]][labels[drain]]
            hop_count += 1
    loads = [0] * request.k
    for node, label in enumerate(labels):
        loads[label] += request.node_weights[node]
    return {
        "cut": cut,
        "connectivity": connectivity,
        "hop_numerator": hop_numerator,
        "hop_count": hop_count,
        "violation": violation,
        "weighted_violation_shape10": weighted_violation_shape10,
        "legal_reward10": legal_reward10,
        "native_energy10": BETA * weighted_violation_shape10 - legal_reward10 + 10 * gamma * connectivity,
        "capacity_overflow": sum(max(0, load - request.capacity) for load in loads),
        "loads": loads,
    }


def adjacency(request: Request) -> list[list[int]]:
    result = [[] for _ in range(request.n)]
    for left, right in request.pair_weights:
        result[left].append(right); result[right].append(left)
    return result


def violating_core(request: Request) -> set[int]:
    return {
        node
        for left, right in request.pair_weights
        if request.distances[request.labels[left]][request.labels[right]] > request.hmax
        for node in (left, right)
    }


def strict_domains(request: Request, movable: set[int], graph: list[list[int]]) -> dict[int, list[int]]:
    return {
        node: [
            label for label in range(request.k)
            if all(
                request.distances[label][request.labels[other]] <= request.hmax
                for other in graph[node] if other not in movable
            )
        ]
        for node in sorted(movable)
    }


def v2_closure(request: Request, fixed: dict[int, int], max_nodes: int = MAX_CLOSURE_NODES) -> dict:
    graph = adjacency(request)
    v1 = violating_core(request) - fixed.keys()
    domains = strict_domains(request, v1, graph)
    additions = {
        other
        for node, domain in domains.items() if not domain
        for other in graph[node]
        if other not in v1 and other not in fixed
    }
    room = max(0, max_nodes - len(v1))
    movable = set(v1) | set(sorted(additions)[:room])
    domains = strict_domains(request, movable, graph)
    return {
        "movable": sorted(movable),
        "domains": domains,
        "empty_nodes": sorted(node for node, domain in domains.items() if not domain),
        "bounded": len(movable) <= max_nodes,
        "one_hop_additions": sorted(movable - set(v1)),
    }


def selector(model, variable, node: int, label: int):
    value = model.new_bool_var(f"n{node}_is_{label}")
    model.add(variable == label).only_enforce_if(value)
    model.add(variable != label).only_enforce_if(value.negated())
    return value


def element_for_pair(model, request, label_vars, left, right, table, name):
    lv, rv = label_vars.get(left), label_vars.get(right)
    if lv is None and rv is None:
        return table[request.labels[left] * request.k + request.labels[right]]
    if lv is None:
        values = [table[request.labels[left] * request.k + label] for label in range(request.k)]
        out = model.new_int_var(min(values), max(values), name); model.add_element(rv, values, out); return out
    if rv is None:
        values = [table[label * request.k + request.labels[right]] for label in range(request.k)]
        out = model.new_int_var(min(values), max(values), name); model.add_element(lv, values, out); return out
    index = model.new_int_var(0, request.k * request.k - 1, f"{name}_idx")
    model.add(index == lv * request.k + rv)
    out = model.new_int_var(min(table), max(table), name); model.add_element(index, table, out); return out


def solve_v2(
    request: Request,
    fixed: dict[int, int],
    seconds: float = 30.0,
    gamma: int = GAMMA,
) -> tuple[list[int], dict]:
    started = time.perf_counter()
    before = evaluate(request, request.labels)
    closure = v2_closure(request, fixed)
    movable = set(closure["movable"]); domains = closure["domains"]
    base = {
        "schema": "MODEL_MODEL_P2_V2_PROPOSAL_V1",
        "before": before,
        "movable_nodes": len(movable),
        "one_hop_additions": len(closure["one_hop_additions"]),
        "empty_nodes": closure["empty_nodes"],
        "gamma": gamma,
    }
    if not movable or not closure["bounded"] or closure["empty_nodes"]:
        return list(request.labels), {**base, "status": "rollback_unusable_v2_closure", "seconds": time.perf_counter() - started}
    model = cp_model.CpModel()
    label_vars = {
        node: model.new_int_var_from_domain(cp_model.Domain.from_values(domains[node]), f"label_{node}")
        for node in sorted(movable)
    }
    selectors = {
        (node, label): selector(model, label_vars[node], node, label)
        for node in sorted(movable) for label in domains[node]
    }
    for label in range(request.k):
        removed = sum(request.node_weights[node] for node in movable if request.labels[node] == label)
        incoming = sum(
            request.node_weights[node] * selectors[node, label]
            for node in movable if label in domains[node]
        )
        model.add(before["loads"][label] - removed + incoming <= request.capacity)

    pair_cost_table10 = []
    for left_label in range(request.k):
        for right_label in range(request.k):
            distance = request.distances[left_label][right_label]
            pair_cost_table10.append(
                BETA * (10 + distance - request.hmax)
                if distance > request.hmax else -10 * (request.hmax - distance)
            )
    pair_terms = []
    for index, ((left, right), weight) in enumerate(request.pair_weights.items()):
        if left not in movable and right not in movable:
            continue
        pair_terms.append(weight * element_for_pair(model, request, label_vars, left, right, pair_cost_table10, f"pair_{index}"))

    con_terms = []
    for index, (weight, source, drains) in enumerate(request.hyperedges):
        nodes = (source, *drains)
        if not any(node in movable for node in nodes):
            continue
        candidate_labels = {
            label
            for node in nodes
            for label in (domains[node] if node in movable else (request.labels[node],))
        }
        used_terms = []
        for label in sorted(candidate_labels):
            if any(node not in movable and request.labels[node] == label for node in nodes):
                used_terms.append(1); continue
            terms = [selectors[node, label] for node in nodes if node in movable and label in domains[node]]
            if terms:
                used = model.new_bool_var(f"h{index}_uses_{label}")
                for term in terms:
                    model.add(used >= term)
                model.add(used <= sum(terms)); used_terms.append(used)
        con_terms.append(weight * (sum(used_terms) - 1))
    model.minimize(sum(pair_terms) + 10 * gamma * sum(con_terms))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(0.01, seconds)
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    status = solver.solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return list(request.labels), {
            **base,
            "status": "rollback_no_penalty_solution",
            "solver_status": solver.status_name(status),
            "seconds": time.perf_counter() - started,
        }
    candidate = list(request.labels)
    for node in movable:
        candidate[node] = int(solver.value(label_vars[node]))
    after = evaluate(request, candidate, gamma)
    fixed_mismatches = sum(candidate[node] != label for node, label in fixed.items())
    safe = after["capacity_overflow"] == 0 and fixed_mismatches == 0
    progress = after["violation"] < before["violation"] and after["native_energy10"] < before["native_energy10"]
    strict_zero = safe and progress and after["violation"] == 0
    status_name = (
        "accepted_strict_zero_final"
        if strict_zero else
        "proposal_requires_same_checkpoint_continuation"
        if safe and progress else
        "rollback_no_verified_progress"
    )
    if status_name == "rollback_no_verified_progress":
        candidate = list(request.labels); after = before
    return candidate, {
        **base,
        "status": status_name,
        "solver_status": solver.status_name(status),
        "objective_bound": solver.best_objective_bound,
        "after": after,
        "fixed_mismatches": fixed_mismatches,
        "changed_nodes": [node for node in range(request.n) if candidate[node] != request.labels[node]],
        "seconds": time.perf_counter() - started,
    }
