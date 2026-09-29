"""Recovery."""
#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

from repair_joint import read_membership, solve_joint
from repair_local import evaluate, read_fixed, read_request, solve_v2


def assignment_hash(values: list[int]) -> int:
    result = 1469598103934665603
    for value in values:
        result ^= value + 1
        result = (result * 1099511628211) & ((1 << 64) - 1)
    return result


def atomic_text(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def write_output(path: Path, labels: list[int], result: dict) -> None:
    digest = assignment_hash(labels)
    bridge_status = "accepted_strict_zero" if result["accepted_strict_zero"] else result["status"]
    lines = [
        f"CORE_REPAIR_OUTPUT_V1 {len(labels)} {int(bool(result['changed']))} {digest} {bridge_status}",
        *(str(label) for label in labels),
    ]
    atomic_text(path, "\n".join(lines) + "\n")
    metadata = {**result, "status": bridge_status, "assignment_hash": digest}
    atomic_text(path.with_suffix(path.suffix + ".json"), json.dumps(metadata, indent=2, sort_keys=True) + "\n")


def run(
    input_path: Path,
    fixed_path: Path,
    output_path: Path,
    tag: str,
    p2_seconds: float,
    joint_seconds: float,
    membership_path: Path,
    table: str,
) -> int:
    request = read_request(input_path)
    fixed = read_fixed(fixed_path, request.n)
    gamma = 15
    before = evaluate(request, request.labels, gamma)
    p2_labels, p2 = solve_v2(request, fixed, p2_seconds, gamma=gamma)
    p2_metrics = p2.get("after", before)
    p2_strict = p2.get("status") == "accepted_strict_zero_final"
    safe_progress = (
        p2_metrics.get("capacity_overflow") == 0
        and int(p2.get("fixed_mismatches", 0)) == 0
        and p2_metrics.get("violation", before["violation"]) < before["violation"]
    )
    hint = p2_labels if safe_progress else list(request.labels)
    joint = None
    route = "P2_STRICT_ZERO"
    accepted = p2_labels
    if not p2_strict:
        route = "P2_JOINT_FROM_P2_HINT" if safe_progress else "P2_JOINT_FROM_A_HINT"
        membership = read_membership(membership_path, request.n)
        joint_labels, joint = solve_joint(
            request, fixed, membership, hint, table=table, seconds=joint_seconds
        )
        if joint.get("status") == "accepted_strict_zero_joint":
            accepted = joint_labels
        else:
            route = "ATOMIC_FALLBACK_A"
            accepted = list(request.labels)
    accepted_metrics = evaluate(request, accepted, gamma)
    strict = (
        accepted_metrics["violation"] == 0
        and accepted_metrics["capacity_overflow"] == 0
        and all(accepted[node] == label for node, label in fixed.items())
    )
    if not strict:
        accepted = list(request.labels)
        accepted_metrics = before
        route = "ATOMIC_FALLBACK_A"
    result = {
        "schema": "JOINT_MODEL_UNIFIED_REPAIR_BRIDGE_RESULT_V1",
        "tag": tag,
        "table": table,
        "route": route,
        "status": "accepted_strict_zero" if strict else "rollback_to_checkpoint_a",
        "p2_status": p2.get("status"),
        "p2_safe_progress": safe_progress,
        "p2": p2,
        "joint": joint,
        "hint_source": "P2_SAFE_PROGRESS" if safe_progress else "CHECKPOINT_A",
        "changed": accepted != request.labels,
        "accepted_strict_zero": strict,
        "incumbent_metrics": before,
        "proposed_metrics": joint.get("raw_proposed_metrics", before) if joint else p2_metrics,
        "accepted_metrics": accepted_metrics,
        "fixed_mismatches": sum(accepted[node] != label for node, label in fixed.items()),
        "changed_nodes": [node for node in range(request.n) if accepted[node] != request.labels[node]],
        "commit_authority": "CXX_FULL_GRAPH_EXACT_GATE",
    }
    write_output(output_path, accepted, result)
    return 0
