"""Anytime."""
#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

from ortools.sat.python import cp_model


COMPACT_MODEL_PATH = Path(__file__).resolve().parent / 'compact.py'


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def certificate_name(has_solution: bool, gap: float | None, limit: float) -> str:
    """Separate a real zero-gap proof from CP-SAT's tolerance-based OPTIMAL status."""

    if not has_solution:
        return "NO_SOLUTION"
    if gap is not None and gap <= 1e-12:
        return "OPTIMAL"
    if gap is not None and limit > 0.0 and gap <= limit + 1e-12:
        return "CERTIFIED_1PCT"
    return "FEASIBLE_UNCERTIFIED"


def solve_stage(atomic_model, base, parent, instance: dict, selected: list[int], hint: list[int],
                locks: dict[str, int], objective: str, coefficients: dict[str, int],
                debt_text: str, policy: dict):
    """Return every exact-safe incumbent and label its actual certificate."""

    model, variables, expressions = base.build_model(
        instance, selected, hint, locks, objective, coefficients, debt_text
    )
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = policy["max_time_seconds"]
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 237
    if policy["relative_gap_limit"]:
        solver.parameters.relative_gap_limit = policy["relative_gap_limit"]
    started = time.perf_counter()
    status = solver.solve(model)
    elapsed = time.perf_counter() - started
    has_solution = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    gap = (
        atomic_model.relative_gap(solver.objective_value, solver.best_objective_bound)
        if has_solution else None
    )
    certificate = certificate_name(has_solution, gap, policy["relative_gap_limit"])
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
        "wall_seconds": elapsed,
        "representation": "compact_label_cost_table_v1",
    }
    if not has_solution:
        return row, None, None

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
    values = {name: int(solver.value(expression)) for name, expression in expressions.items()}
    if any(measured[name] != values[name] for name in expressions):
        raise RuntimeError(f"compact model metric mismatch {measured} {values}")

    expanded = base.expand_labels(instance, labels)
    exact = parent.evaluate(instance["dataset"], expanded)
    if exact["violation"] or exact["capacity_overflow"]:
        raise RuntimeError(f"unsafe compact feasible candidate {exact}")
    exact_scored = dict(exact)
    exact_scored["mfs_balanced_score"] = base.score_value(exact, coefficients)
    row["feasible_metrics"] = measured
    row["feasible_exact_metrics"] = exact_scored
    row["feasible_assignment_hash"] = parent.assignment_hash(expanded)
    return row, labels, values


def optimize(atomic_model, base, parent, request, membership: list[int], tag: str):
    """Keep the best exact-safe candidate; continue tie-breaks only after zero gap."""

    legacy = parent.build_instance(request, membership, tag)
    instance = atomic_model.build_atomic_instance(request, membership, tag, legacy)
    current = base.metrics(instance, instance["current_labels"])
    exact_current = parent.evaluate(request, request.labels)
    for name in base.STANDARD_METRICS + ("violation", "capacity_overflow"):
        if current[name] != exact_current[name]:
            raise RuntimeError(f"atomic reconstruction mismatch {name}: {current[name]} != {exact_current[name]}")
    coefficients = base.score_coefficients(current)
    current = base.metrics(instance, instance["current_labels"], coefficients)
    instance["incumbent_metrics"] = current
    selected = [block for block, domain in enumerate(instance["domains"]) if len(domain) > 1]
    estimate = atomic_model.estimate_model_size(instance, selected)
    policy = atomic_model.model_policy(estimate)

    stages = []
    locks: dict[str, int] = {}
    hint = list(instance["current_labels"])
    best_labels = list(instance["current_labels"])
    best_metrics = current
    best_debt = base.debt_diagnostic(current, current, base.debt_limit(atomic_model.DEBT_PCT))
    safe_candidates = 0
    stopped_after = None

    if selected and policy["tier"] != "large_vcycle_deferred":
        for objective in ("mfs_balanced_score", "cut", "hop_numerator"):
            stage, labels, values = solve_stage(
                atomic_model, base, parent, instance, selected, hint, locks, objective,
                coefficients, atomic_model.DEBT_PCT, policy,
            )
            stages.append(stage)
            if labels is None:
                stopped_after = f"{objective}:no_solution"
                break

            candidate = base.metrics(instance, labels, coefficients)
            debt = base.debt_diagnostic(current, candidate, base.debt_limit(atomic_model.DEBT_PCT))
            policy_improved = (
                base.policy_key(candidate, atomic_model.OBJECTIVE_MODE)
                < base.policy_key(current, atomic_model.OBJECTIVE_MODE)
            )
            eligible = policy_improved and debt["accepted"]
            stage["policy_improved"] = policy_improved
            stage["debt_diagnostic"] = debt
            stage["safe_anytime_eligible"] = eligible
            if eligible:
                safe_candidates += 1
                if base.policy_key(candidate, atomic_model.OBJECTIVE_MODE) < base.policy_key(
                    best_metrics, atomic_model.OBJECTIVE_MODE
                ):
                    best_labels, best_metrics, best_debt = labels, candidate, debt

            # A non-zero gap does not justify locking this objective for another stage.
            if not stage["proven_exact"]:
                stopped_after = f"{objective}:{stage['certificate']}"
                break
            hint = labels
            locks[objective] = values[objective]

    accepted = best_labels != instance["current_labels"]
    expanded = base.expand_labels(instance, best_labels)
    exact = parent.evaluate(request, expanded)
    if exact["violation"] or exact["capacity_overflow"]:
        raise RuntimeError(f"unsafe anytime candidate {exact}")
    exact_scored = dict(exact)
    exact_scored["mfs_balanced_score"] = base.score_value(exact, coefficients)
    if any(
        exact_scored[name] != base.metrics(instance, best_labels, coefficients)[name]
        for name in base.STANDARD_METRICS
    ):
        raise RuntimeError("anytime expanded evaluator mismatch")

    # The complete proposal is already represented by the last exact-safe stage row.
    proposed_exact = stages[-1].get("feasible_exact_metrics", exact_current) if stages else exact_current
    source = (
        "large_vcycle_deferred" if policy["tier"] == "large_vcycle_deferred"
        else "cpsat_safe_anytime" if accepted
        else "incumbent"
    )
    scope = {
        "name": "atomic_full",
        "atom_limit": None,
        "selected_families": len(selected),
        "selected_atoms": len(selected),
        "skipped_families": 0,
        "partial_selected_families": 0,
        "single_move_prefilter": False,
        "target_domain": "all_fpga_labels_except_fixed",
    }
    return expanded, {
        "tag": tag,
        "nodes": request.n,
        "blocks": len(instance["block_sizes"]),
        "movable_blocks": len(selected),
        "selected_blocks": len(selected),
        "selected_block_ids": selected,
        "anytime_model_policy": {
            "detector": atomic_model.DETECTOR,
            "scope": "atomic_full",
            "objective": atomic_model.OBJECTIVE_MODE,
            "debt_pct": atomic_model.DEBT_PCT,
            "uncertified_acceptance": "exact_safe_policy_improving",
            "tie_break_continuation": "zero_gap_only",
            "stage_seconds": policy["max_time_seconds"],
            "large_path": "deferred_vcycle",
        },
        "scope_policy": scope,
        "model_policy": policy,
        "atomic_family_keys": [list(key) for key in instance["atomic_family_keys"]],
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
        "cpsat_metrics": stages[-1].get("feasible_metrics", current) if stages else current,
        "proposed_source": "cpsat" if stages else source,
        "proposed_metrics": proposed_exact,
        "proposed_assignment_hash": (
            stages[-1].get("feasible_assignment_hash", parent.assignment_hash(request.labels))
            if stages else parent.assignment_hash(request.labels)
        ),
        "accepted_source": source,
        "accepted_metrics": exact_scored,
        "changed": accepted,
        "safe_candidates": safe_candidates,
        "stopped_after": stopped_after,
        "stages_complete": len(stages) == 3 and all(stage["proven_exact"] for stage in stages),
        "stage_results": stages,
        "assignment_hash": parent.assignment_hash(expanded),
    }
