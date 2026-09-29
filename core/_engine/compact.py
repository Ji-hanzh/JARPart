"""Compact."""
#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from ortools.sat.python import cp_model


ATOMIC_MODEL_PATH = Path(__file__).resolve().parent / 'atomic.py'


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def compact_estimate(legacy_estimate, instance: dict, selected: list[int]) -> dict[str, int]:
    """Report compact proto size while preserving atomic_model tier classification."""

    legacy = legacy_estimate(instance, selected)
    selected_set = set(selected)
    pair_tables = 0
    fixed_pair_constraints = 0
    for left, right in instance["pair_weights"]:
        left_selected, right_selected = left in selected_set, right in selected_set
        if left_selected and right_selected:
            pair_tables += 1
        elif left_selected or right_selected:
            moving, fixed = (left, right) if left_selected else (right, left)
            fixed_label = instance["current_labels"][fixed]
            fixed_pair_constraints += sum(
                instance["distances"][label][fixed_label] > instance["hmax"]
                for label in instance["domains"][moving]
            )

    assignment_variables = legacy["assignment_variables"]
    presence_variables = legacy["hyperedge_presence_variables"]
    compact_variables = assignment_variables + len(selected) + 2 * pair_tables + presence_variables
    compact_constraints = (
        2 * len(selected) + instance["k"] + 3 + 2
        + pair_tables + fixed_pair_constraints + presence_variables
    )
    return {
        "variables_upper": compact_variables,
        "constraints_upper": compact_constraints,
        "assignment_variables": assignment_variables,
        "label_variables": len(selected),
        "pair_state_variables": 0,
        "pair_table_cost_variables": 2 * pair_tables,
        "pair_table_constraints": pair_tables,
        "pair_table_tuples": legacy["pair_state_variables"],
        "hyperedge_presence_variables": presence_variables,
        "legacy_variables_upper": legacy["variables_upper"],
        "legacy_constraints_upper": legacy["constraints_upper"],
        "legacy_pair_state_variables": legacy["pair_state_variables"],
    }


def compact_policy(legacy_policy, estimate: dict[str, int]) -> dict:
    """Keep every request in the same small/medium/large tier as atomic_model."""

    classification = {
        **estimate,
        "variables_upper": estimate["legacy_variables_upper"],
        "constraints_upper": estimate["legacy_constraints_upper"],
    }
    policy = legacy_policy(classification)
    policy.update(estimate)
    policy["classification_variables_upper"] = estimate["legacy_variables_upper"]
    policy["classification_constraints_upper"] = estimate["legacy_constraints_upper"]
    policy["representation"] = "compact_label_cost_table_v1"
    return policy


def build_model(base, instance: dict, selected: list[int], hint: list[int],
                locks: dict[str, int], objective: str, coefficients: dict[str, int],
                debt_text: str):
    """Equivalent model without one Boolean variable per compatible label pair."""

    selected_set = set(selected)
    model = cp_model.CpModel()
    x = {}
    block_label = {}
    for block in selected:
        choices = []
        for label in instance["domains"][block]:
            variable = model.new_bool_var(f"x_b{block}_f{label}")
            x[block, label] = variable
            choices.append(variable)
        model.add(base.linear_sum(choices) == 1)
        block_label[block] = model.new_int_var(0, instance["k"] - 1, f"label_b{block}")
        model.add(
            block_label[block] == base.linear_sum(
                label * x[block, label] for label in instance["domains"][block]
            )
        )

    for label in range(instance["k"]):
        fixed_load = sum(
            size for block, (size, current) in enumerate(
                zip(instance["block_sizes"], instance["current_labels"])
            )
            if block not in selected_set and current == label
        )
        model.add(
            fixed_load + base.linear_sum(
                instance["block_sizes"][block] * x[block, label]
                for block in selected if label in instance["domains"][block]
            ) <= instance["capacity"]
        )

    cut_constant = 0
    cut_terms = []
    hop_pair_terms = []
    for pair_index, ((left, right), weight) in enumerate(instance["pair_weights"].items()):
        left_selected, right_selected = left in selected_set, right in selected_set
        if not left_selected and not right_selected:
            cut_constant += weight * int(
                instance["current_labels"][left] != instance["current_labels"][right]
            )
            continue
        if left_selected and right_selected:
            rows = []
            for left_label in instance["domains"][left]:
                for right_label in instance["domains"][right]:
                    if instance["distances"][left_label][right_label] > instance["hmax"]:
                        continue
                    hop_cost = (
                        instance["directed_weights"].get((left, right), 0)
                        * instance["distances"][left_label][right_label]
                        + instance["directed_weights"].get((right, left), 0)
                        * instance["distances"][right_label][left_label]
                    )
                    rows.append((left_label, right_label, int(left_label != right_label), hop_cost))
            if not rows:
                model.add_bool_or([])
                continue
            different = model.new_bool_var(f"different_pair{pair_index}")
            hop_values = [row[3] for row in rows]
            hop_cost = model.new_int_var(min(hop_values), max(hop_values), f"hop_pair{pair_index}")
            model.add_allowed_assignments(
                [block_label[left], block_label[right], different, hop_cost], rows
            )
            cut_terms.append(weight * different)
            hop_pair_terms.append(hop_cost)
            continue

        moving, fixed = (left, right) if left_selected else (right, left)
        fixed_label = instance["current_labels"][fixed]
        for label in instance["domains"][moving]:
            if instance["distances"][label][fixed_label] > instance["hmax"]:
                model.add(x[moving, label] == 0)
            elif label != fixed_label:
                cut_terms.append(weight * x[moving, label])

    hop_constant = 0
    hop_terms = list(hop_pair_terms)
    for (source, drain), weight in instance["directed_weights"].items():
        source_selected, drain_selected = source in selected_set, drain in selected_set
        if source_selected and drain_selected:
            continue
        if not source_selected and not drain_selected:
            hop_constant += (
                weight
                * instance["distances"][instance["current_labels"][source]][instance["current_labels"][drain]]
            )
            continue
        moving = source if source_selected else drain
        for label in instance["domains"][moving]:
            source_label = label if source_selected else instance["current_labels"][source]
            drain_label = label if drain_selected else instance["current_labels"][drain]
            hop_terms.append(weight * instance["distances"][source_label][drain_label] * x[moving, label])

    con_constant = 0
    con_terms = []
    for edge_index, (blocks, weight) in enumerate(instance["hyperedge_weights"].items()):
        moving_blocks = [block for block in blocks if block in selected_set]
        if not moving_blocks:
            con_constant += weight * (
                len({instance["current_labels"][block] for block in blocks}) - 1
            )
            continue
        fixed_labels = {
            instance["current_labels"][block] for block in blocks if block not in selected_set
        }
        con_constant -= weight
        for label in range(instance["k"]):
            if label in fixed_labels:
                con_constant += weight
                continue
            supports = [
                x[block, label] for block in moving_blocks
                if label in instance["domains"][block]
            ]
            if supports:
                present = model.new_bool_var(f"y_e{edge_index}_f{label}")
                model.add_max_equality(present, supports)
                con_terms.append(weight * present)

    expressions = {
        "cut": cut_constant + base.linear_sum(cut_terms),
        "connectivity": con_constant + base.linear_sum(con_terms),
        "hop_numerator": hop_constant + base.linear_sum(hop_terms),
    }
    expressions["mfs_balanced_score"] = sum(
        coefficients[name] * expressions[name] for name in base.STANDARD_METRICS
    )
    limit = base.debt_limit(debt_text)
    if limit is not None:
        if not float(limit).is_integer():
            raise RuntimeError(f"compact_model in-model debt must be integral: {limit}")
        numerator = 100 + int(limit)
        for name in base.STANDARD_METRICS:
            model.add(100 * expressions[name] <= numerator * instance["incumbent_metrics"][name])
    for name, value in locks.items():
        model.add(expressions[name] == value)
    model.minimize(expressions[objective])
    for block in selected:
        for label in instance["domains"][block]:
            model.add_hint(x[block, label], int(hint[block] == label))
    validation = model.validate()
    if validation:
        raise RuntimeError(validation)
    return model, x, expressions
