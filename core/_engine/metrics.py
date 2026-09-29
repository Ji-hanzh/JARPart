"""Metrics."""
#!/usr/bin/env python3

from __future__ import annotations

from pathlib import Path

from ortools.sat.python import cp_model


FIXED_MODEL_PATH = Path(__file__).resolve().parent / 'fixed_input.py'
COMMUNITY_MODEL_PATH = Path(__file__).resolve().parent / 'community.py'
SCORE_SCALE = 1_000_000_000
OBJECTIVES = ("lexicographic", "mfs_balanced")
DEBTS = ("0", "1", "5", "10", "inf")
STANDARD_METRICS = ("connectivity", "cut", "hop_numerator")
DETECTORS = ("pair", "fastgreedy")
SCOPES = ("fragment48", "family48")
FROZEN_OBJECTIVE = "mfs_balanced"
FROZEN_DEBT = "5"


def linear_sum(values):
    values = list(values)
    return cp_model.LinearExpr.sum(values) if values else 0


def score_coefficients(incumbent: dict) -> dict[str, int]:
    return {
        "connectivity": max(1, round(0.50 * SCORE_SCALE / max(1, incumbent["connectivity"]))),
        "cut": max(1, round(0.25 * SCORE_SCALE / max(1, incumbent["cut"]))),
        "hop_numerator": max(1, round(0.25 * SCORE_SCALE / max(1, incumbent["hop_numerator"]))),
    }


def score_value(metrics: dict, coefficients: dict[str, int]) -> int:
    return sum(coefficients[name] * metrics[name] for name in STANDARD_METRICS)


def metrics(instance: dict, labels: list[int], coefficients: dict[str, int] | None = None) -> dict:
    cut = sum(
        weight for (left, right), weight in instance["pair_weights"].items()
        if labels[left] != labels[right]
    )
    violation = sum(
        weight for (left, right), weight in instance["pair_weights"].items()
        if instance["distances"][labels[left]][labels[right]] > instance["hmax"]
    )
    connectivity = sum(
        weight * (len({labels[block] for block in blocks}) - 1)
        for blocks, weight in instance["hyperedge_weights"].items()
    )
    hop_numerator = sum(
        weight * instance["distances"][labels[source]][labels[drain]]
        for (source, drain), weight in instance["directed_weights"].items()
    )
    loads = [0] * instance["k"]
    for block, label in enumerate(labels):
        loads[label] += instance["block_sizes"][block]
    result = {
        "cut": cut,
        "connectivity": connectivity,
        "hop_numerator": hop_numerator,
        "hop": hop_numerator / instance["hop_count"] if instance["hop_count"] else 0.0,
        "violation": violation,
        "capacity_overflow": sum(max(load - instance["capacity"], 0) for load in loads),
        "loads": loads,
    }
    if coefficients is not None:
        result["mfs_balanced_score"] = score_value(result, coefficients)
    return result


def policy_key(row: dict, mode: str) -> tuple[int, ...]:
    if mode == "lexicographic":
        return row["connectivity"], row["cut"], row["hop_numerator"]
    return (
        row["mfs_balanced_score"], row["cut"],
        row["hop_numerator"], row["connectivity"],
    )


def debt_limit(text: str) -> float | None:
    return None if text == "inf" else float(text)


def debt_diagnostic(before: dict, after: dict, limit: float | None) -> dict:
    debts = {
        name: max(0.0, 100.0 * (after[name] - before[name]) / before[name]) if before[name] else 0.0
        for name in STANDARD_METRICS
    }
    accepted = limit is None or max(debts.values()) <= limit + 1e-12
    return {"limit_pct": limit, "component_debt_pct": debts, "max_debt_pct": max(debts.values()), "accepted": accepted}


def expand_labels(instance: dict, block_labels: list[int]) -> list[int]:
    return [block_labels[block] for block in instance["block_of_node"]]
