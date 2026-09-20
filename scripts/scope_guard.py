#!/usr/bin/env python3
"""Live PR scope/base guards used by review and merge paths."""
from __future__ import annotations

import fnmatch
import json
from typing import Any

from onecompany_lib import run


def _normalize(value: str) -> str:
    return value.replace("\\", "/").strip().lstrip("./").rstrip("/")


def scope_covers_path(pattern: str, path: str) -> bool:
    """Return True when a declared write scope covers a changed repository path.

    Non-glob scopes are treated as either an exact file or a directory prefix. This
    intentionally fails closed for empty patterns/paths.
    """
    scope = _normalize(pattern)
    candidate = _normalize(path)
    if not scope or not candidate:
        return False
    if scope in {"*", "**", "**/*"}:
        return True
    if any(token in scope for token in ("*", "?", "[")):
        return fnmatch.fnmatchcase(candidate, scope)
    return candidate == scope or candidate.startswith(scope + "/")


def undeclared_paths(paths: list[str], write_scope: list[str]) -> list[str]:
    scopes = [str(item) for item in write_scope if str(item).strip()]
    if not scopes:
        return sorted(set(paths))
    return sorted({path for path in paths if not any(scope_covers_path(scope, path) for scope in scopes)})


def changed_files(repo: str, pr: int) -> tuple[list[str] | None, str | None]:
    result = run(["gh", "pr", "diff", str(pr), "--repo", repo, "--name-only"])
    if result.returncode != 0:
        return None, result.stderr.strip() or result.stdout.strip() or "unknown diff error"
    return [line.strip() for line in result.stdout.splitlines() if line.strip()], None


def live_pr(repo: str, pr: int) -> tuple[dict[str, Any] | None, str | None]:
    result = run([
        "gh", "pr", "view", str(pr), "--repo", repo, "--json",
        "headRefOid,baseRefOid,baseRefName,state,isDraft,mergeStateStatus",
    ])
    if result.returncode != 0:
        return None, result.stderr.strip() or result.stdout.strip() or "cannot read live PR"
    try:
        return json.loads(result.stdout), None
    except json.JSONDecodeError as exc:
        return None, f"invalid live PR JSON: {exc}"


def scope_errors(paths: list[str], work_unit: dict[str, Any] | None) -> list[str]:
    if not isinstance(work_unit, dict):
        return ["work unit could not be resolved for live PR scope verification"]
    scopes = work_unit.get("write_scope", [])
    if not isinstance(scopes, list) or not scopes:
        return ["work unit has no declared write_scope; exact live scope cannot be verified"]
    undeclared = undeclared_paths(paths, [str(item) for item in scopes])
    if undeclared:
        return ["live PR modifies paths outside declared write_scope: " + ", ".join(undeclared)]
    return []
