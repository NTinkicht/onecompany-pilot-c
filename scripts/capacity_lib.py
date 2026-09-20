#!/usr/bin/env python3
"""Shared actor execution-capacity and dispatch-availability helpers."""
from __future__ import annotations

from typing import Any

from onecompany_lib import budget_allows


def capacity_measurement_errors(ready: dict[str, Any] | None) -> list[str]:
    capacity = (ready or {}).get("capacity", {})
    reasons: list[str] = []
    if capacity.get("measured") is not True:
        reasons.append("capacity_unmeasured")
    observed_at = capacity.get("observed_at")
    if not isinstance(observed_at, str) or not observed_at.strip():
        reasons.append("capacity_observed_at_missing")
    evidence = capacity.get("evidence")
    if not isinstance(evidence, list) or not any(isinstance(item, str) and item.strip() for item in evidence):
        reasons.append("capacity_evidence_missing")
    streams = capacity.get("implementation_streams")
    if not isinstance(streams, int) or isinstance(streams, bool) or streams < 0:
        reasons.append("capacity_stream_count_invalid")
    return reasons


def implementation_capacity_limit(ready: dict[str, Any] | None) -> int:
    """Return measured capacity only; an unmeasured default grants zero slots."""
    if capacity_measurement_errors(ready):
        return 0
    return int((ready or {}).get("capacity", {}).get("implementation_streams", 0))


def implementation_active_count(
    actor_id: str,
    active: list[dict[str, Any]],
    exclude_lease_id: str | None = None,
) -> int:
    return sum(
        1
        for lease in active
        if lease.get("role") == "implementation"
        and lease.get("actor") == actor_id
        and lease.get("id") != exclude_lease_id
    )


def configured_dispatch_exists(
    dispatch_doc: dict[str, Any],
    actor_id: str,
    capability: str,
    unattended: bool,
) -> bool:
    actor_entry = next(
        (
            item
            for item in dispatch_doc.get("actors", [])
            if item.get("actor_id") == actor_id
        ),
        None,
    )
    if not actor_entry:
        return False
    return any(
        mechanism.get("configured") is True
        and capability in mechanism.get("capabilities", [])
        and (not unattended or mechanism.get("unattended") is True)
        for mechanism in actor_entry.get("mechanisms", [])
    )


def implementation_availability(
    actor: dict[str, Any],
    ready: dict[str, Any] | None,
    budget: dict[str, Any],
    active: list[dict[str, Any]],
    *,
    dispatch_doc: dict[str, Any] | None = None,
    require_unattended: bool = False,
    exclude_lease_id: str | None = None,
) -> tuple[int, list[str]]:
    """Return measured free implementation slots and hard ineligibility reasons.

    Capacity is a circuit breaker only. It never creates spending permission,
    readiness, repository access, a lease, or a dispatch path.
    """
    reasons: list[str] = []
    actor_id = str(actor.get("id") or "")
    if not actor_id:
        return 0, ["missing_actor_id"]
    if not actor.get("enabled"):
        reasons.append("disabled")
    if not actor.get("configured"):
        reasons.append("not_configured")
    if "implementation" not in actor.get("capabilities", []):
        reasons.append("implementation_not_declared")
    if not budget_allows(actor.get("cost_class", "UNKNOWN_COST"), budget):
        reasons.append("forbidden_by_budget")

    if ready is None:
        reasons.append("missing_readiness")
        return 0, reasons

    if ready.get("setup_state") not in {"ready", "degraded"}:
        reasons.append(f"setup_state:{ready.get('setup_state')}")
    if "implementation" not in ready.get("verified_capabilities", []):
        reasons.append("implementation_not_verified")
    if "implementation" in ready.get("temporarily_unavailable_capabilities", []):
        reasons.append("implementation_temporarily_unavailable")
    access = ready.get("repository_access", {})
    if not access.get("read"):
        reasons.append("repository_read_not_verified")
    if not access.get("write"):
        reasons.append("repository_write_not_verified")

    reasons.extend(capacity_measurement_errors(ready))

    if require_unattended:
        unattended = ready.get("unattended", {})
        if unattended.get("configured") is not True or unattended.get("verified") is not True:
            reasons.append("unattended_not_verified")
        if (
            dispatch_doc is None
            or not configured_dispatch_exists(
                dispatch_doc,
                actor_id,
                "implementation",
                True,
            )
        ):
            reasons.append("unattended_implementation_dispatch_missing")

    limit = implementation_capacity_limit(ready)
    current = implementation_active_count(actor_id, active, exclude_lease_id)
    free = max(limit - current, 0)
    if free <= 0:
        reasons.append("actor_capacity")

    hard_reasons = [reason for reason in reasons if reason != "actor_capacity"]
    return (free if not hard_reasons else 0), sorted(set(reasons))


def implementation_pool(
    actors_doc: dict[str, Any],
    readiness_doc: dict[str, Any],
    budget: dict[str, Any],
    active: list[dict[str, Any]],
    *,
    dispatch_doc: dict[str, Any] | None = None,
    require_unattended: bool = False,
) -> dict[str, Any]:
    """Return aggregate executable implementation capacity using one policy path."""
    readiness = {
        item.get("actor_id"): item
        for item in readiness_doc.get("actors", [])
        if item.get("actor_id")
    }
    available: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    total_slots = 0
    for actor in actors_doc.get("actors", []):
        actor_id = str(actor.get("id") or "")
        slots, reasons = implementation_availability(
            actor,
            readiness.get(actor_id),
            budget,
            active,
            dispatch_doc=dispatch_doc,
            require_unattended=require_unattended,
        )
        if slots > 0:
            available.append({"actor": actor_id, "free_slots": slots})
            total_slots += slots
        else:
            rejected.append({"actor": actor_id, "reasons": sorted(set(reasons))})
    return {
        "free_slots": total_slots,
        "actors": available,
        "rejected": rejected,
        "unattended_required": require_unattended,
    }
