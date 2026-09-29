"""Repair joint."""
#!/usr/bin/env python3
from __future__ import annotations

import struct
import time
from pathlib import Path

from ortools.sat.python import cp_model

from repair_closure import adaptive_closure
from repair_local import Request, element_for_pair, evaluate, selector


SCORE_SCALE = 1_000_000_000
DEBT_NUMERATOR = 105
DEBT_DENOMINATOR = 100


def read_membership(path: Path, n: int) -> list[int]:
    raw = Path(path).read_bytes()
    if len(raw) != 4 * n:
        raise RuntimeError(f"membership length={len(raw)}/{4*n}")
    return list(struct.unpack(f"<{n}I", raw))


def objective_weights(table: str) -> tuple[float, float, float]:
    # final_model: all tables use the same declared joint objective.
    return (0.5, 0.25, 0.25)


def coefficient(before: int, weight: float) -> int:
    return 0 if weight == 0 else max(1, round(weight * SCORE_SCALE / max(1, before)))


def _sum(terms):
    return sum(terms) if terms else 0


def solve_joint(
    request: Request,
    fixed: dict[int, int],
    membership: list[int],
    hint: list[int],
    *,
    table: str,
    seconds: float = 60.0,
) -> tuple[list[int], dict]:
    started = time.perf_counter()
    before = evaluate(request, request.labels, gamma=15)
    closure = adaptive_closure(request, fixed)
    movable = set(closure["movable"])
    domains = closure["domains"]
    base = {
        "schema": "JOINT_MODEL_P2_JOINT_PROPOSAL_V1",
        "before": before,
        "table": table,
        "movable_nodes": len(movable),
        "closure_source": "CLOSURE_MODEL_ADAPTIVE_BLOCKER_FRONTIER_DEPTH2_CAP512",
        "closure_diagnostics": closure.get("diagnostics", {}),
        "hard_constraints": [
            "PAIR_DISTANCE_LE_HMAX", "CAPACITY", "FIXED_IMMUTABLE",
            "CONNECTIVITY_DEBT_LE_5_PERCENT", "CUT_DEBT_LE_5_PERCENT",
            "HOP_DEBT_LE_5_PERCENT",
        ],
        "primary_objective": "DECLARED_NORMALIZED_CON_CUT_HOP",
        "secondary_objective": "COMMUNITY_ATOM_ESCAPE_THEN_CHANGED_NODES",
    }
    if len(membership) != request.n:
        raise RuntimeError("community membership node count mismatch")
    if not movable or not closure["bounded"] or closure["empty_nodes"]:
        return list(request.labels), {
            **base,
            "status": "rollback_unusable_adaptive_closure",
            "seconds": time.perf_counter() - started,
        }

    model = cp_model.CpModel()
    label_vars = {
        node: model.new_int_var_from_domain(
            cp_model.Domain.from_values(domains[node]), f"label_{node}"
        )
        for node in sorted(movable)
    }
    selectors = {
        (node, label): selector(model, label_vars[node], node, label)
        for node in sorted(movable) for label in domains[node]
    }

    for label in range(request.k):
        removed = sum(
            request.node_weights[node] for node in movable
            if request.labels[node] == label
        )
        incoming = _sum([
            request.node_weights[node] * selectors[node, label]
            for node in movable if label in domains[node]
        ])
        model.add(before["loads"][label] - removed + incoming <= request.capacity)

    allowed = [
        (left_label, right_label)
        for left_label in range(request.k)
        for right_label in range(request.k)
        if request.distances[left_label][right_label] <= request.hmax
    ]
    for left, right in request.pair_weights:
        if left in movable and right in movable:
            model.add_allowed_assignments([label_vars[left], label_vars[right]], allowed)

    cut_constant = 0
    cut_terms = []
    cut_table = [int(left != right) for left in range(request.k) for right in range(request.k)]
    hop_constant = 0
    hop_terms = []
    distance_table = [request.distances[left][right] for left in range(request.k) for right in range(request.k)]
    for index, ((left, right), weight) in enumerate(request.pair_weights.items()):
        if left not in movable and right not in movable:
            cut_constant += weight * int(request.labels[left] != request.labels[right])
        else:
            cut_terms.append(weight * element_for_pair(
                model, request, label_vars, left, right, cut_table, f"cut_{index}"
            ))

    for index, (weight, source, drains) in enumerate(request.hyperedges):
        for drain_index, drain in enumerate(drains):
            if source not in movable and drain not in movable:
                hop_constant += weight * request.distances[request.labels[source]][request.labels[drain]]
            else:
                hop_terms.append(weight * element_for_pair(
                    model, request, label_vars, source, drain, distance_table,
                    f"hop_{index}_{drain_index}",
                ))

    con_constant = 0
    con_terms = []
    for index, (weight, source, drains) in enumerate(request.hyperedges):
        nodes = (source, *drains)
        if not any(node in movable for node in nodes):
            con_constant += weight * (len({request.labels[node] for node in nodes}) - 1)
            continue
        candidates = {
            label for node in nodes
            for label in (domains[node] if node in movable else (request.labels[node],))
        }
        used_terms = []
        for label in sorted(candidates):
            if any(node not in movable and request.labels[node] == label for node in nodes):
                used_terms.append(1)
                continue
            terms = [
                selectors[node, label] for node in nodes
                if node in movable and label in domains[node]
            ]
            if terms:
                used = model.new_bool_var(f"h{index}_uses_{label}")
                for term in terms:
                    model.add(used >= term)
                model.add(used <= _sum(terms))
                used_terms.append(used)
        con_terms.append(weight * (_sum(used_terms) - 1))

    cut_expr = cut_constant + _sum(cut_terms)
    con_expr = con_constant + _sum(con_terms)
    hop_expr = hop_constant + _sum(hop_terms)
    for expr, field in (
        (cut_expr, "cut"), (con_expr, "connectivity"), (hop_expr, "hop_numerator")
    ):
        model.add(DEBT_DENOMINATOR * expr <= DEBT_NUMERATOR * before[field])

    con_weight, cut_weight, hop_weight = objective_weights(table)
    coefficients = {
        "connectivity": coefficient(before["connectivity"], con_weight),
        "cut": coefficient(before["cut"], cut_weight),
        "hop_numerator": coefficient(before["hop_numerator"], hop_weight),
    }
    primary = (
        coefficients["connectivity"] * con_expr
        + coefficients["cut"] * cut_expr
        + coefficients["hop_numerator"] * hop_expr
    )

    changed_terms = []
    forced_changed = 0
    for node in sorted(movable):
        incumbent = request.labels[node]
        if incumbent not in domains[node]:
            forced_changed += request.node_weights[node]
        else:
            changed = model.new_bool_var(f"changed_{node}")
            model.add(changed + selectors[node, incumbent] == 1)
            changed_terms.append(request.node_weights[node] * changed)
    changed_expr = forced_changed + _sum(changed_terms)

    all_family_sizes = {}
    movable_families = {}
    for node in range(request.n):
        key = (membership[node], request.labels[node])
        all_family_sizes[key] = all_family_sizes.get(key, 0) + 1
        if node in movable:
            movable_families.setdefault(key, []).append(node)
    escape_terms = []
    anchored_families = free_families = 0
    for family_index, (key, nodes) in enumerate(sorted(movable_families.items())):
        original_label = key[1]
        anchored = len(nodes) < all_family_sizes[key] or any(node in fixed for node in nodes)
        if anchored:
            anchored_families += 1
            for node in nodes:
                if original_label in domains[node]:
                    escape_terms.append(request.node_weights[node] * (1 - selectors[node, original_label]))
                else:
                    escape_terms.append(request.node_weights[node])
            continue
        free_families += 1
        atom = model.new_int_var(0, request.k - 1, f"community_atom_{family_index}")
        for node in nodes:
            same = model.new_bool_var(f"community_same_{family_index}_{node}")
            model.add(label_vars[node] == atom).only_enforce_if(same)
            model.add(label_vars[node] != atom).only_enforce_if(same.negated())
            escape_terms.append(request.node_weights[node] * (1 - same))
    escape_expr = _sum(escape_terms)

    hint_count = 0
    if len(hint) == request.n:
        for node in sorted(movable):
            if hint[node] in domains[node]:
                model.add_hint(label_vars[node], hint[node])
                hint_count += 1

    primary_solver = cp_model.CpSolver()
    primary_solver.parameters.max_time_in_seconds = max(0.01, seconds * 0.8)
    primary_solver.parameters.num_search_workers = 1
    primary_solver.parameters.random_seed = 0
    model.minimize(primary)
    primary_status = primary_solver.solve(model)
    if primary_status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return list(request.labels), {
            **base,
            "status": "rollback_no_joint_quality_solution",
            "primary_solver_status": primary_solver.status_name(primary_status),
            "hinted_nodes": hint_count,
            "seconds": time.perf_counter() - started,
        }

    chosen = {node: int(primary_solver.value(variable)) for node, variable in label_vars.items()}
    primary_value = int(round(primary_solver.objective_value))
    model.add(primary <= primary_value)
    model.clear_hints()
    for node, value in chosen.items():
        model.add_hint(label_vars[node], value)
    tie_scale = sum(request.node_weights[node] for node in movable) + 1
    model.minimize(escape_expr * tie_scale + changed_expr)
    tie_solver = cp_model.CpSolver()
    tie_solver.parameters.max_time_in_seconds = max(0.01, seconds * 0.2)
    tie_solver.parameters.num_search_workers = 1
    tie_solver.parameters.random_seed = 0
    tie_status = tie_solver.solve(model)
    solver = tie_solver if tie_status in (cp_model.OPTIMAL, cp_model.FEASIBLE) else primary_solver

    candidate = list(request.labels)
    for node, variable in label_vars.items():
        candidate[node] = int(solver.value(variable))
    after = evaluate(request, candidate, gamma=15)
    fixed_mismatches = sum(candidate[node] != label for node, label in fixed.items())
    safe = (
        after["violation"] == 0 and after["capacity_overflow"] == 0
        and fixed_mismatches == 0
        and all(DEBT_DENOMINATOR * after[field] <= DEBT_NUMERATOR * before[field]
                for field in ("connectivity", "cut", "hop_numerator"))
    )
    changed_nodes = [node for node in range(request.n) if candidate[node] != request.labels[node]]
    if not safe:
        candidate = list(request.labels)
    return candidate, {
        **base,
        "status": "accepted_strict_zero_joint" if safe else "rollback_failed_exact_python_gate",
        "primary_solver_status": primary_solver.status_name(primary_status),
        "tie_solver_status": tie_solver.status_name(tie_status),
        "primary_objective_value": primary_value,
        "objective_coefficients": coefficients,
        "objective_weights": {"connectivity": con_weight, "cut": cut_weight, "hop_numerator": hop_weight},
        "hinted_nodes": hint_count,
        "community_anchored_families": anchored_families,
        "community_free_families": free_families,
        "raw_proposed_metrics": after,
        "after": after if safe else before,
        "fixed_mismatches": fixed_mismatches,
        "changed_nodes": changed_nodes if safe else [],
        "seconds": time.perf_counter() - started,
    }
