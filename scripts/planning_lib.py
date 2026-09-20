#!/usr/bin/env python3
"""Deterministic planning, priority, dependency, and parallel-safety helpers."""
from __future__ import annotations

import fnmatch
from typing import Any

from scope_guard import scope_covers_path

DONE = {"MERGED", "DONE"}
ACTIVEISH = {"LEASED", "IN_PROGRESS", "CI_PENDING", "REVIEW_PENDING", "REMEDIATION", "MERGE_READY"}

DEFAULT_PRIORITY_WEIGHTS = {
    "business_value": 1.0,
    "time_criticality": 1.0,
    "risk_reduction": 1.0,
    "dependency_unlock": 1.0,
}


def by_id(work: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item.get("id")): item for item in work if isinstance(item.get("id"), str) and item.get("id")}


def dependency_closure(work_map: dict[str, dict[str, Any]], start: str) -> set[str]:
    result: set[str] = set()
    stack = list(work_map.get(start, {}).get("dependencies", []))
    while stack:
        node = str(stack.pop())
        if node in result:
            continue
        result.add(node)
        stack.extend(work_map.get(node, {}).get("dependencies", []))
    return result


def priority_score(item: dict[str, Any], planning: dict[str, Any]) -> float:
    inputs = item.get("priority_inputs")
    if not isinstance(inputs, dict):
        return float(item.get("priority", 0) or 0)
    policy = planning.get("prioritization", {})
    weights = dict(DEFAULT_PRIORITY_WEIGHTS)
    if isinstance(policy.get("weights"), dict):
        for key in weights:
            try:
                weights[key] = float(policy["weights"].get(key, weights[key]))
            except (TypeError, ValueError):
                pass
    numerator = 0.0
    for key, weight in weights.items():
        try:
            numerator += max(0.0, float(inputs.get(key, 0))) * weight
        except (TypeError, ValueError):
            pass
    estimate = item.get("estimate") if isinstance(item.get("estimate"), dict) else {}
    try:
        job_size = max(float(estimate.get("job_size", inputs.get("job_size", 1))), 0.1)
    except (TypeError, ValueError):
        job_size = 1.0
    try:
        confidence = float(estimate.get("confidence", inputs.get("confidence", 1.0)))
    except (TypeError, ValueError):
        confidence = 1.0
    if confidence > 1:
        confidence /= 100.0
    confidence = min(max(confidence, 0.0), 1.0)
    return confidence * numerator / job_size


def estimated_job_size(item: dict[str, Any]) -> float:
    estimate = item.get("estimate") if isinstance(item.get("estimate"), dict) else {}
    inputs = item.get("priority_inputs") if isinstance(item.get("priority_inputs"), dict) else {}
    try:
        return max(float(estimate.get("job_size", inputs.get("job_size", 1))), 0.1)
    except (TypeError, ValueError):
        return 1.0


def _scope_prefix(pattern: str) -> str:
    value = pattern.replace("\\", "/").strip().lstrip("./")
    wildcard_positions = [value.find(ch) for ch in ("*", "?", "[") if value.find(ch) >= 0]
    if wildcard_positions:
        value = value[: min(wildcard_positions)]
    return value.rstrip("/")


def _has_wildcards(pattern: str) -> bool:
    return any(ch in pattern for ch in ("*", "?", "["))


def scopes_overlap(left: str, right: str) -> bool:
    a = left.replace("\\", "/").strip().lstrip("./").rstrip("/")
    b = right.replace("\\", "/").strip().lstrip("./").rstrip("/")
    if not a or not b:
        return True
    if a in {"*", "**", "**/*"} or b in {"*", "**", "**/*"}:
        return True
    if scope_covers_path(a, b) or scope_covers_path(b, a):
        return True
    aw, bw = _has_wildcards(a), _has_wildcards(b)
    if aw and fnmatch.fnmatchcase(b, a):
        return True
    if bw and fnmatch.fnmatchcase(a, b):
        return True
    ap, bp = _scope_prefix(a), _scope_prefix(b)
    if not ap or not bp:
        return True
    return ap == bp or ap.startswith(bp + "/") or bp.startswith(ap + "/")


def locks_overlap(left: str, right: str) -> bool:
    a, b = left.strip(), right.strip()
    if not a or not b:
        return False
    if a == "*" or b == "*":
        return True
    if a == b:
        return True
    if a.endswith(":*") and b.startswith(a[:-1]):
        return True
    if b.endswith(":*") and a.startswith(b[:-1]):
        return True
    return False


def work_item_for_lease(lease: dict[str, Any], work_map: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Resolve an active WU using the immutable lease snapshot before current queue data.

    Once implementation authority is granted, a later queue edit must not silently
    shrink the scope/locks/risk/dependencies that protect concurrent scheduling.
    An explicit re-plan should release/re-acquire the lease with a new snapshot.
    """
    wu = str(lease.get("work_unit") or "")
    snapshot = lease.get("planning_snapshot")
    if isinstance(snapshot, dict) and snapshot:
        return {"id": wu, **snapshot}
    if wu and wu in work_map:
        return work_map[wu]
    return {
        "id": wu,
        "parallelism": "serial",
        "write_scope": [],
        "resource_locks": ["*"],
        "risk_class": "CRITICAL",
        "dependencies": [],
        "dependency_closure": [],
    }


def _dependency_view(
    item: dict[str, Any],
    work_map: dict[str, dict[str, Any]] | None,
) -> tuple[set[str], bool]:
    """Return dependency closure and whether it is provably complete.

    The authoritative queue graph wins over any embedded cache. Immutable lease
    snapshots may carry a persisted closure; a legacy snapshot without one remains
    unknown and therefore serializes fail-closed.
    """
    item_id = str(item.get("id") or "")
    if work_map and item_id and item_id in work_map and item is work_map[item_id]:
        return dependency_closure(work_map, item_id), True

    closure = item.get("dependency_closure")
    if isinstance(closure, list):
        return {str(value) for value in closure if value}, True
    return set(), False


def work_units_conflict(
    left: dict[str, Any],
    right: dict[str, Any],
    planning: dict[str, Any],
    work_map: dict[str, dict[str, Any]] | None = None,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    left_id, right_id = str(left.get("id", "")), str(right.get("id", ""))
    if left_id and left_id == right_id:
        return True, ["same_work_unit"]
    parallel = planning.get("parallel_execution", {})
    if not parallel.get("enabled", False):
        return True, ["parallel_execution_disabled"]
    if left.get("parallelism") == "serial" or right.get("parallelism") == "serial":
        reasons.append("serial_policy")
    if left.get("risk_class") == "CRITICAL" or right.get("risk_class") == "CRITICAL":
        if parallel.get("critical_risk_default") == "serialize":
            reasons.append("critical_risk_serialized")

    left_closure, left_complete = _dependency_view(left, work_map)
    right_closure, right_complete = _dependency_view(right, work_map)
    if not left_complete or not right_complete:
        reasons.append("unknown_dependency_closure")
    if right_id and right_id in left_closure:
        reasons.append("dependency_relationship")
    if left_id and left_id in right_closure:
        reasons.append("dependency_relationship")

    if work_map and left_id and right_id and left is work_map.get(left_id) and right is work_map.get(right_id):
        if right_id in dependency_closure(work_map, left_id) or left_id in dependency_closure(work_map, right_id):
            reasons.append("dependency_relationship")

    left_locks = [str(v) for v in left.get("resource_locks", [])]
    right_locks = [str(v) for v in right.get("resource_locks", [])]
    if any(locks_overlap(a, b) for a in left_locks for b in right_locks):
        reasons.append("resource_lock_overlap")
    left_scope = [str(v) for v in left.get("write_scope", [])]
    right_scope = [str(v) for v in right.get("write_scope", [])]
    require_scope = parallel.get("require_write_scope_for_parallel", True)
    if require_scope and (not left_scope or not right_scope):
        reasons.append("unknown_write_scope")
    elif any(scopes_overlap(a, b) for a in left_scope for b in right_scope):
        reasons.append("write_scope_overlap")
    return bool(reasons), sorted(set(reasons))


def implementation_admission_violations(
    candidate: dict[str, Any],
    active_leases: list[dict[str, Any]],
    planning: dict[str, Any],
    work_map: dict[str, dict[str, Any]] | None = None,
    *,
    actor: str | None = None,
    actor_limit: int | None = None,
) -> list[dict[str, Any]]:
    """Return deterministic implementation-admission violations.

    This is the shared admission decision used by both local lease acquisition and
    durable-ledger arbitration. Durable callers rely on immutable lease snapshots;
    local callers may additionally provide the complete work graph.
    """
    work_map = work_map or {}
    active = [item for item in active_leases if item.get("role", "implementation") == "implementation"]
    violations: list[dict[str, Any]] = []
    candidate_id = str(candidate.get("id") or "")

    same_wu = next((item for item in active if str(item.get("work_unit") or item.get("id") or "") == candidate_id), None)
    if same_wu is not None:
        violations.append(
            {
                "reason": "implementation_lease_already_active_for_wu",
                "work_unit": candidate_id,
                "with_lease_id": same_wu.get("id"),
            }
        )

    limit = int(planning.get("parallel_execution", {}).get("max_concurrent_implementation_streams", 1) or 1)
    if len(active) >= limit:
        violations.append({"reason": "implementation_wip_limit_reached", "limit": limit, "active": len(active)})

    if actor and actor_limit is not None:
        actor_active = sum(1 for item in active if item.get("actor") == actor)
        if actor_active >= actor_limit:
            violations.append(
                {
                    "reason": "actor_implementation_capacity_reached",
                    "actor": actor,
                    "limit": actor_limit,
                    "active": actor_active,
                }
            )

    for lease in active:
        other = work_item_for_lease(lease, work_map)
        if str(other.get("id") or "") == candidate_id:
            continue
        conflict, reasons = work_units_conflict(candidate, other, planning, work_map or None)
        if conflict:
            violations.append(
                {
                    "reason": "implementation_work_unit_conflict",
                    "with_work_unit": other.get("id"),
                    "with_lease_id": lease.get("id"),
                    "details": reasons,
                }
            )
    return violations


def dependency_ready(item: dict[str, Any], work_map: dict[str, dict[str, Any]], durable_done: set[str] | None = None) -> tuple[bool, list[str], list[str]]:
    """Require the authoritative transitive dependency closure to be complete."""
    durable_done = durable_done or set()
    item_id = str(item.get("id") or "")
    if item_id and item_id in work_map:
        required = dependency_closure(work_map, item_id)
    else:
        required = {str(dep) for dep in item.get("dependencies", []) if dep}
    missing: list[str] = []
    unsatisfied: list[str] = []
    for dep in sorted(required):
        if dep in durable_done:
            continue
        if dep not in work_map:
            missing.append(dep)
        elif work_map[dep].get("status") not in DONE:
            unsatisfied.append(dep)
    return not missing and not unsatisfied, missing, unsatisfied


def active_work_items(active_leases: list[dict[str, Any]], work_map: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [work_item_for_lease(lease, work_map) for lease in active_leases if isinstance(lease.get("work_unit"), str)]


def critical_path(work: list[dict[str, Any]]) -> dict[str, Any]:
    work_map = by_id(work)
    visiting: set[str] = set()
    memo: dict[str, tuple[float, list[str]]] = {}

    def duration(item: dict[str, Any]) -> float:
        return estimated_job_size(item)

    def solve(node: str) -> tuple[float, list[str]]:
        if node in memo:
            return memo[node]
        if node in visiting:
            raise ValueError(f"dependency cycle involving {node}")
        visiting.add(node)
        item = work_map[node]
        best_len, best_path = 0.0, []
        for dep in item.get("dependencies", []):
            if dep not in work_map:
                continue
            length, path = solve(str(dep))
            if length > best_len:
                best_len, best_path = length, path
        visiting.remove(node)
        result = (best_len + duration(item), [*best_path, node])
        memo[node] = result
        return result

    best = (0.0, [])
    for node in sorted(work_map):
        candidate = solve(node)
        if candidate[0] > best[0]:
            best = candidate
    return {"job_size": best[0], "path": best[1]}


def rank_work(work: list[dict[str, Any]], planning: dict[str, Any]) -> list[dict[str, Any]]:
    """Rank deterministically according to the declared reference tie-breaker policy."""
    critical_ids = set(critical_path(work).get("path", [])) if work else set()
    return sorted(
        work,
        key=lambda item: (
            -priority_score(item, planning),
            0 if item.get("id") in critical_ids else 1,
            -int(item.get("priority", 0) or 0),
            estimated_job_size(item),
            str(item.get("id", "")),
        ),
    )


def select_parallel_set(
    work: list[dict[str, Any]],
    planning: dict[str, Any],
    active_leases: list[dict[str, Any]] | None = None,
    durable_done: set[str] | None = None,
    include_proposed: bool = False,
) -> dict[str, Any]:
    active_leases = active_leases or []
    durable_done = durable_done or set()
    work_map = by_id(work)
    active_items = active_work_items(active_leases, work_map)
    parallel = planning.get("parallel_execution", {})
    limit = int(parallel.get("max_concurrent_implementation_streams", 1) or 1)
    available = max(limit - len(active_items), 0)
    statuses = {"READY"} | ({"PROPOSED"} if include_proposed else set())
    candidates = [item for item in work if item.get("status") in statuses and item.get("id") not in durable_done]
    candidate_ids = {item.get("id") for item in candidates}
    ranked = [item for item in rank_work(work, planning) if item.get("id") in candidate_ids]
    selected: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for item in ranked:
        ready, missing, unsatisfied = dependency_ready(item, work_map, durable_done)
        if not ready:
            blocked.append({"id": item.get("id"), "reasons": ["dependency_not_ready"], "missing": missing, "unsatisfied": unsatisfied})
            continue
        conflicts: list[str] = []
        for other in [*active_items, *selected]:
            conflict, reasons = work_units_conflict(item, other, planning, work_map)
            if conflict:
                conflicts.extend(f"{other.get('id')}:{reason}" for reason in reasons)
        if conflicts:
            blocked.append({"id": item.get("id"), "reasons": sorted(set(conflicts))})
            continue
        if len(selected) < available:
            selected.append(item)
        else:
            blocked.append({"id": item.get("id"), "reasons": ["wip_limit"]})
    return {
        "limit": limit,
        "active_count": len(active_items),
        "available_slots": available,
        "selected": selected,
        "blocked": blocked,
        "ranked": ranked,
    }
