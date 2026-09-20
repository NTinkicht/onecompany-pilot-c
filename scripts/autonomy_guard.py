"""Reusable autonomy-level floor for consequential unattended capabilities.

A configured mechanism and an active lease are necessary but not sufficient:
only the adopting project's owner-approved autonomy policy grants a worker
permission to write or advance automatically.
"""
from __future__ import annotations

from typing import Any

LEVELS = {"L0": 0, "L1": 1, "L2": 2, "L3": 3, "L4": 4, "L5": 5}
MUTATING_CAPABILITIES = frozenset({"implementation", "ci_remediation", "merge_execution"})


def level_violations(
    config: dict[str, Any], capability: str, *, unattended: bool
) -> list[str]:
    """Fail closed for unsupported level or a mutating unattended operation."""
    autonomy = config.get("autonomy")
    if not isinstance(autonomy, dict):
        return ["autonomy_policy_unavailable"]
    level = autonomy.get("level")
    if level not in LEVELS:
        return ["autonomy_level_invalid_or_unverified"]

    if not unattended:
        return []
    if capability in {"implementation", "ci_remediation"}:
        if LEVELS[level] < 2:
            return ["unattended_implementation_requires_approved_L2"]
    elif capability == "merge_execution":
        if LEVELS[level] < 3:
            return ["unattended_merge_requires_approved_L3"]
    return []


def continuous_selection_violations(config: dict[str, Any]) -> list[str]:
    """Starting the next WU without a new human prompt is an L4 permission."""
    reasons = level_violations(config, "implementation", unattended=True)
    if reasons:
        return reasons
    autonomy = config["autonomy"]
    if LEVELS[autonomy["level"]] < 4:
        reasons.append("continuous_work_selection_requires_approved_L4")
    if autonomy.get("continue_when_ready_work_exists") is not True:
        reasons.append("continuous_work_selection_disabled")
    return reasons
