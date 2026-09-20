#!/usr/bin/env python3
"""A4a deterministic, test-only writer for an existing leased PR.

NO PR creation, merge, deployment, arbitrary code execution, or credentials.
This adapter is dormant until a *separate installed project* configures and
verifies an unattended actor, an L2 policy, a durable canonical lease and a
reviewed event-driven workflow. The OneCompany product itself stays L1.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from autonomy_guard import level_violations
from onecompany_lib import CONTROL, ROOT, load_json

MECHANISM_ID = "github-actions-fixture-writer"
WU_PATTERN = re.compile(r"^WU[A-Za-z0-9._-]{1,58}$")
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def fixture_path(work_unit: str) -> str | None:
    if not isinstance(work_unit, str) or not WU_PATTERN.fullmatch(work_unit):
        return None
    return f"docs/onecompany-fixture/{work_unit}.md"


def preflight(
    request: dict[str, Any],
    config: dict[str, Any],
    queue: dict[str, Any],
    pull_request: dict[str, Any],
    *,
    repository: str,
    actions: bool,
    enabled: bool,
) -> list[str]:
    """Pure fail-closed plan; no file/network/token operations."""
    errors: list[str] = []
    lease = request.get("lease")
    if not isinstance(lease, dict):
        return ["canonical_implementation_lease_missing"]
    wu = request.get("work_unit")
    target = fixture_path(wu)
    if target is None:
        errors.append("invalid_fixture_work_unit")
    if request.get("capability") != "implementation":
        errors.append("test_worker_requires_implementation_capability")
    if request.get("unattended") is not True:
        errors.append("unattended_dispatch_required")
    if not actions:
        errors.append("github_actions_runtime_required")
    if not enabled:
        errors.append("fixture_worker_not_owner_enabled")
    errors.extend(level_violations(config, "implementation", unattended=True))
    if config.get("safety", {}).get("emergency_stop") is not False:
        errors.append("emergency_stop_or_unknown")
    configured_repo = config.get("project", {}).get("repository")
    if configured_repo != repository or request.get("repository") != repository:
        errors.append("repository_identity_mismatch")
    if lease.get("work_unit") != wu or lease.get("actor") != request.get("actor"):
        errors.append("canonical_lease_identity_mismatch")
    branch = lease.get("branch")
    pr = lease.get("pr")
    sha = lease.get("start_head")
    if not isinstance(branch, str) or not branch or branch.startswith("-"):
        errors.append("canonical_branch_invalid")
    if not isinstance(pr, int) or isinstance(pr, bool) or pr <= 0:
        errors.append("canonical_pr_invalid")
    if not isinstance(sha, str) or not SHA_PATTERN.fullmatch(sha):
        errors.append("canonical_head_invalid")
    trusted_ref = (lease.get("admission_snapshot") or {}).get("trusted_ref")
    if not isinstance(trusted_ref, str) or not SHA_PATTERN.fullmatch(trusted_ref):
        errors.append("protected_base_evidence_missing")
    plan = lease.get("planning_snapshot") or {}
    if plan.get("risk_class") != "LOW":
        errors.append("test_only_low_risk_work_unit_required")
    if target is not None and plan.get("write_scope") != [target]:
        errors.append("test_only_exact_fixture_write_scope_required")
    units = queue.get("work_units")
    item = next(
        (x for x in units if isinstance(x, dict) and x.get("id") == wu),
        None,
    ) if isinstance(units, list) else None
    if item is None:
        errors.append("work_unit_missing_from_protected_queue")
    elif item.get("branch") != branch or item.get("pr") != pr:
        errors.append("protected_queue_stream_binding_mismatch")
    elif item.get("status") not in {"READY", "LEASED", "IN_PROGRESS"}:
        errors.append("work_unit_not_implementation_ready")
    head = pull_request.get("head") or {}
    base = pull_request.get("base") or {}
    if (
        pull_request.get("state") != "open"
        or pull_request.get("draft") is True
        or pull_request.get("number") != pr
    ):
        errors.append("canonical_pr_not_open_or_ready")
    if (
        head.get("ref") != branch
        or head.get("sha") != sha
        or (head.get("repo") or {}).get("full_name") != repository
    ):
        errors.append("canonical_pr_head_changed_or_foreign")
    if (
        base.get("sha") != trusted_ref
        or (base.get("repo") or {}).get("full_name") != repository
        or base.get("ref") != config.get("project", {}).get("default_branch")
    ):
        errors.append("canonical_pr_base_changed_or_foreign")
    return sorted(set(errors))


def _cmd(*argv: str) -> str:
    result = subprocess.run(
        list(argv), cwd=ROOT, check=False, capture_output=True, text=True
    )
    if result.returncode != 0:
        # Never echo environment variables, access tokens, or raw subprocess
        # stderr into a durable issue/PR/event record.
        raise RuntimeError(f"fixture_worker_operation_failed:{argv[0]}")
    return result.stdout.strip()


def _live_pr(repository: str, number: int) -> dict[str, Any]:
    raw = _cmd("gh", "api", f"repos/{repository}/pulls/{number}")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError("fixture_worker_pr_response_invalid")
    return value


def invoke(request: dict[str, Any]) -> dict[str, Any]:
    """Create one deterministic fixture commit on the canonical existing PR.

    Non-fast-forward push fails rather than rewriting anyone else's commits.
    Repeated requests do not create another commit when the fixture exists.
    """
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    lease = request.get("lease")
    if not isinstance(lease, dict) or not isinstance(lease.get("pr"), int):
        raise RuntimeError("fixture_worker_canonical_lease_missing")
    config = load_json(CONTROL / "config.json")
    queue = load_json(CONTROL / "queue.json")
    live = _live_pr(repository, lease["pr"]) if repository else {}
    failures = preflight(
        request, config, queue, live,
        repository=repository,
        actions=os.environ.get("GITHUB_ACTIONS") == "true",
        enabled=os.environ.get("ONECOMPANY_A4_FIXTURE_ENABLED") == "true",
    )
    if failures:
        raise RuntimeError("fixture_worker_refused:" + ",".join(failures))
    branch = lease["branch"]
    expected_head = lease["start_head"]
    work_unit = request["work_unit"]
    relpath = fixture_path(work_unit)
    assert relpath is not None

    # The workflow checks out the default branch to execute reviewed code.
    # Its exact commit must be the same protected PR base used for admission.
    if _cmd("git", "rev-parse", "HEAD") != live["base"]["sha"]:
        raise RuntimeError("fixture_worker_checkout_not_trusted_base")
    if _cmd("git", "status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("fixture_worker_checkout_dirty")
    _cmd("git", "fetch", "--no-tags", "origin", f"refs/heads/{branch}")
    if _cmd("git", "rev-parse", "FETCH_HEAD") != expected_head:
        raise RuntimeError("fixture_worker_branch_moved_during_fetch")
    _cmd("git", "checkout", "--detach", expected_head)

    path = ROOT / relpath
    root = ROOT.resolve()
    if path.is_symlink() or (ROOT / "docs").is_symlink():
        raise RuntimeError("fixture_worker_unsafe_target")
    folder = path.parent
    if folder.is_symlink():
        raise RuntimeError("fixture_worker_unsafe_target")
    folder.mkdir(parents=True, exist_ok=True)
    if not folder.resolve().is_relative_to(root):
        raise RuntimeError("fixture_worker_target_escaped_checkout")
    body = (
        "# OneCompany isolated implementation fixture\n\n"
        f"Work Unit: {work_unit}\n"
        f"Lease: {lease['id']}\n"
        "Mode: deterministic test-only source edit; no deployment.\n"
    )
    if path.exists():
        if not path.is_file() or path.read_text(encoding="utf-8") != body:
            raise RuntimeError("fixture_worker_existing_path_conflict")
        return {
            "status": "DISPATCH_COMPLETED",
            "evidence": {"work_unit": work_unit, "pr": lease["pr"],
                         "branch": branch, "already_applied": True},
        }
    with path.open("x", encoding="utf-8") as handle:
        handle.write(body)
    _cmd("git", "config", "user.name", "onecompany-fixture-worker")
    _cmd("git", "config", "user.email",
         "41898282+github-actions[bot]@users.noreply.github.com")
    _cmd("git", "add", "--", relpath)
    if _cmd("git", "diff", "--cached", "--name-only") != relpath:
        raise RuntimeError("fixture_worker_staged_scope_mismatch")
    _cmd("git", "commit", "-m", f"test-only: exercise unattended {work_unit}")
    commit = _cmd("git", "rev-parse", "HEAD")
    # No --force. A simultaneous writer must not lose its commit.
    _cmd("git", "push", "origin", f"HEAD:refs/heads/{branch}")
    if _live_pr(repository, lease["pr"]).get("head", {}).get("sha") != commit:
        raise RuntimeError("fixture_worker_committed_head_not_observed")
    return {
        "status": "DISPATCH_COMPLETED",
        "evidence": {"work_unit": work_unit, "pr": lease["pr"],
                     "branch": branch, "head": commit,
                     "fixture_path": relpath},
    }
