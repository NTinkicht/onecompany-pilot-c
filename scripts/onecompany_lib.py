#!/usr/bin/env python3
"""Shared dependency-free helpers for OneCompany tooling."""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / ".onecompany"


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, value: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, path)


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def run(command: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd or ROOT),
        check=False,
        text=True,
        capture_output=True,
    )


def autonomy_number(level: str) -> int:
    if len(level) == 2 and level.startswith("L") and level[1].isdigit():
        return int(level[1])
    raise ValueError(f"invalid autonomy level: {level}")


def active_implementation_leases(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        lease
        for lease in state.get("active_leases", [])
        if lease.get("status") == "active" and lease.get("role") == "implementation"
    ]


def budget_allows(cost_class: str, budget: dict[str, Any]) -> bool:
    classes = budget.get("cost_classes", {})
    if cost_class in classes.get("allowed", []):
        return True
    if cost_class in classes.get("forbidden", []):
        return False
    if cost_class in classes.get("conditionally_allowed", []):
        return budget.get("ai", {}).get("additional_monthly_spend_cap", 0) > 0
    return budget.get("ai", {}).get("unknown_cost_behavior") == "allow_within_cap"


def github_repo_from_config(config: dict[str, Any]) -> str | None:
    value = config.get("project", {}).get("repository")
    return value if isinstance(value, str) and "/" in value else None


def github_repo_from_remote() -> str | None:
    """Resolve owner/name from the checked-out git origin rather than candidate policy."""
    result = run(["git", "remote", "get-url", "origin"])
    if result.returncode != 0:
        return None
    remote = result.stdout.strip()
    if not remote:
        return None
    patterns = (
        r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$",
        r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$",
    )
    for pattern in patterns:
        match = re.search(pattern, remote)
        if match:
            return f"{match.group(1)}/{match.group(2)}"
    return None


def emergency_stop_active(config: dict[str, Any] | None = None) -> bool:
    """Return true if either repository or out-of-band containment is asserted.

    STOP assertion is intentionally permissionless and monotonic-safe. A host,
    operator, or incident wrapper can freeze OneCompany without trusting the
    repository checkout by exporting ``ONECOMPANY_EMERGENCY_STOP`` or by
    pointing ``ONECOMPANY_EMERGENCY_STOP_FILE`` at an external sentinel file.
    Repository code cannot override an asserted external stop.
    """
    external = str(os.environ.get("ONECOMPANY_EMERGENCY_STOP", "")).strip().casefold()
    if external in {"1", "true", "yes", "on", "stop", "stopped"}:
        return True
    sentinel = str(os.environ.get("ONECOMPANY_EMERGENCY_STOP_FILE", "")).strip()
    if sentinel:
        try:
            if Path(sentinel).expanduser().exists():
                return True
        except OSError:
            return True
    document = config if config is not None else load_json(CONTROL / "config.json")
    return document.get("safety", {}).get("emergency_stop") is True


def governance_config() -> dict[str, Any]:
    return load_json(CONTROL / "governance.json")


def path_matches_any(path: str, patterns: list[str]) -> bool:
    normalized = path.replace("\\", "/")
    return any(fnmatch.fnmatchcase(normalized, pattern) for pattern in patterns)


def protected_control_plane_paths(paths: list[str]) -> list[str]:
    governance = governance_config().get("control_plane", {})
    patterns = governance.get("protected_paths", [])
    return sorted(path for path in paths if path_matches_any(path, patterns))


def always_human_paths(paths: list[str]) -> list[str]:
    governance = governance_config().get("control_plane", {})
    patterns = governance.get("always_human_paths", [])
    return sorted(path for path in paths if path_matches_any(path, patterns))
