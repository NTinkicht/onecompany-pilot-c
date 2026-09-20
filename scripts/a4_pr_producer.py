#!/usr/bin/env python3
"""A4c: project-scoped, deterministic first-PR producer for disposable pilots.

This module is dormant in the OneCompany source installation. A target must
separately enable the disabled workflow and a verified L2 fixture-only actor.
It creates no merge/release/deployment/database authority.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from fixture_actions_adapter import fixture_path
from onecompany_lib import CONTROL, emergency_stop_active, load_json

SHA = re.compile(r"^[0-9a-f]{40}$")
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class Refused(RuntimeError):
    """No mutation is permitted or an attempted mutation is indeterminate."""


class ApiFailure(Refused):
    def __init__(self, status: int):
        """Initialize the strict GitHub client or a sanitized HTTP status error."""
        super().__init__(f"github_api_status_{status}")
        self.status = status


class GitHub:
    def __init__(self, repository: str, token: str):
        """Initialize the strict GitHub client or a sanitized HTTP status error."""
        if not REPO.fullmatch(repository) or not token:
            raise Refused("repository_identity_or_github_token_missing")
        self.repository = repository
        self.token = token

    def call(self, method: str, path: str, payload: dict | None = None) -> Any:
        """Call the repository-scoped REST API without disclosing response secrets."""
        if (not path.startswith("/") or "://" in path
                or (path == "/" and method != "GET")):
            raise Refused("invalid_api_path")
        if method != "GET" and emergency_stop_active():
            raise Refused("out_of_band_emergency_stop_active")
        # GET / is the canonical repository metadata endpoint. GitHub REST
        # does not guarantee that a trailing slash resolves the same route.
        endpoint = "" if path == "/" else path
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            "https://api.github.com/repos/" + self.repository + endpoint,
            data=data,
            method=method,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as result:
                raw = result.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            # Do not print a response body; it could include private content.
            raise ApiFailure(exc.code) from None
        except (OSError, ValueError) as exc:
            raise Refused("github_result_uncertain_reconcile_before_retry") from exc


def branch_for(wu: str) -> str:
    """Derive one deterministic safe branch identifier for the fixture WU."""
    if fixture_path(wu) is None:
        raise Refused("invalid_fixture_work_unit")
    return "onecompany-a4-" + wu.lower()


def fixture_body(repo: str, wu: str, actor: str, base: str) -> str:
    """Encode exact project/WU/actor/base identity in the fixture content."""
    return (
        "# OneCompany A4 isolated claim and implementation fixture\n\n"
        f"Repository: {repo}\nWork Unit: {wu}\nActor: {actor}\n"
        f"Base SHA: {base}\n"
        "Scope: deterministic test-only fixture; no deployment or release.\n"
    )


def preflight(
    config: dict, queue: dict, readiness: dict, dispatch: dict,
    budget: dict, actors: dict,
    *, repo: str, actor: str, wu: str, base: str,
    actions: bool, enabled: bool,
) -> tuple[str, str]:
    """Require exact project-local permissions and a READY, unbound LOW-risk WU."""
    errors: list[str] = []
    target = fixture_path(wu)
    branch = branch_for(wu)
    if not actions or not enabled:
        errors.append("unattended_fixture_producer_disabled")
    if config.get("autonomy", {}).get("level") not in {"L2", "L3", "L4", "L5"}:
        errors.append("project_l2_approval_missing")
    if (config.get("safety", {}).get("emergency_stop") is not False
            or emergency_stop_active(config)):
        errors.append("emergency_stop_or_unknown")
    costs = budget.get("ai", {})
    ci = budget.get("ci", {})
    if (costs.get("additional_monthly_spend_cap") != 0
        or costs.get("allow_new_paid_vendor") is not False
        or costs.get("unknown_cost_behavior") != "forbid"
        or costs.get("allow_paid_fallback") is not False
        or costs.get("allow_overage") is not False
        or costs.get("allow_auto_topup") is not False
        or ci.get("runner_cost_policy") != "included_or_free_only"):
        errors.append("zero_extra_spend_preflight_failed")
    project = config.get("project", {})
    if (project.get("repository") != repo
        or not isinstance(project.get("default_branch"), str)
        or not re.fullmatch(r"[A-Za-z0-9._/-]+", project["default_branch"])):
        errors.append("project_identity_or_default_branch_mismatch")
    if not SHA.fullmatch(base):
        errors.append("trusted_base_sha_missing")
    matching = [x for x in queue.get("work_units", [])
                if isinstance(x, dict) and x.get("id") == wu]
    if len(matching) != 1:
        errors.append("wu_missing_or_duplicated")
    else:
        item = matching[0]
        if item.get("status") != "READY" or item.get("risk_class") != "LOW":
            errors.append("wu_not_ready_low_risk")
        if item.get("branch") != branch or item.get("pr") is not None:
            errors.append("wu_not_canonically_pre_pr_bound")
        if item.get("write_scope") != [target]:
            errors.append("wu_not_exact_fixture_scope")
        if item.get("dependencies"):
            errors.append("fixture_dependencies_require_durable_proof")
    for other in queue.get("work_units", []):
        if isinstance(other, dict) and other.get("id") != wu and other.get("branch") == branch:
            errors.append("branch_reserved_by_another_wu")
    people = [x for x in readiness.get("actors", []) if x.get("actor_id") == actor]
    if len(people) != 1:
        errors.append("actor_not_in_verified_registry")
    else:
        person = people[0]
        unavailable = person.get("temporarily_unavailable_capabilities", [])
        if (not isinstance(unavailable, list)
            or any(not isinstance(value, str) for value in unavailable)
            or "implementation" in unavailable):
            errors.append("actor_unattended_write_unverified")
        if (person.get("setup_state") != "ready"
            or "implementation" not in person.get("verified_capabilities", [])
            or person.get("repository_access", {}).get("write") is not True
            or person.get("unattended", {}).get("verified") is not True
            or person.get("capacity", {}).get("measured") is not True
            or person.get("capacity", {}).get("implementation_streams", 0) < 1):
            errors.append("actor_unattended_write_unverified")
    roster = [x for x in actors.get("actors", [])
              if isinstance(x, dict) and x.get("id") == actor]
    allowed = budget.get("cost_classes", {}).get("allowed", [])
    if len(roster) != 1 or not isinstance(allowed, list):
        errors.append("actor_cost_class_not_verified")
    else:
        cost_class = roster[0].get("cost_class")
        if (roster[0].get("enabled") is not True
            or roster[0].get("configured") is not True
            or "implementation" not in roster[0].get("capabilities", [])
            or cost_class not in {"FREE_ALLOWANCE", "LOCAL", "INCLUDED_SUBSCRIPTION"}
            or cost_class not in allowed):
            errors.append("actor_cost_class_not_verified")
    routes = [x for x in dispatch.get("actors", []) if x.get("actor_id") == actor]
    mechanisms = (routes[0].get("mechanisms", []) if len(routes) == 1 else [])
    if not any(
        m.get("id") == "github-actions-a4-pr-producer"
        and m.get("kind") == "github_action"
        and m.get("configured") is True and m.get("unattended") is True
        and "implementation" in m.get("capabilities", [])
        for m in mechanisms
    ):
        errors.append("producer_dispatch_route_not_verified")
    if errors:
        raise Refused(",".join(sorted(set(errors))))
    assert target is not None
    return branch, target


def _api_branch(api: GitHub, branch: str) -> str | None:
    """Read an exact branch SHA, treating missing or malformed refs safely."""
    try:
        ref = api.call("GET", "/git/ref/heads/" + branch)
    except ApiFailure as exc:
        if exc.status == 404:
            return None
        raise
    obj = ref.get("object") if isinstance(ref, dict) else None
    result = obj.get("sha") if isinstance(obj, dict) else None
    if not isinstance(result, str) or not SHA.fullmatch(result):
        raise Refused("branch_ref_ambiguous")
    return result


def _matching_pulls(api: GitHub, branch: str) -> list[dict]:
    """Inventory all historical PRs for a canonical branch without truncation."""
    owner = api.repository.split("/", 1)[0]
    query = urllib.parse.urlencode({
        "state": "all", "head": owner + ":" + branch,
        "per_page": "100",
    })
    result = api.call("GET", "/pulls?" + query)
    if (not isinstance(result, list) or len(result) >= 100
            or any(not isinstance(pr, dict) for pr in result)):
        raise Refused("pull_request_inventory_ambiguous")
    return result


def _verify_claim(api: GitHub, head: str, base: str, target: str, body: str) -> None:
    """Reject a prior claim unless its ancestry, content and diff are exact."""
    commit = api.call("GET", "/git/commits/" + head)
    parents = commit.get("parents") if isinstance(commit, dict) else None
    if (not isinstance(parents, list) or len(parents) != 1
            or not isinstance(parents[0], dict)
            or parents[0].get("sha") != base):
        raise Refused("pre_existing_branch_not_exact_claim")
    path = urllib.parse.quote(target, safe="/")
    record = api.call("GET", "/contents/" + path + "?ref=" + head)
    if (not isinstance(record, dict) or record.get("type") != "file"
            or record.get("encoding") != "base64"):
        raise Refused("claim_fixture_missing_or_invalid")
    encoded = record.get("content")
    if not isinstance(encoded, str):
        raise Refused("claim_fixture_unreadable")
    try:
        existing = base64.b64decode(encoded, validate=False).decode("utf-8")
    except (UnicodeError, ValueError) as exc:
        raise Refused("claim_fixture_unreadable") from exc
    if existing != body:
        raise Refused("pre_existing_branch_claim_conflict")
    comparison = api.call("GET", "/compare/" + base + "..." + head)
    changed = comparison.get("files") if isinstance(comparison, dict) else None
    if (not isinstance(changed, list) or len(changed) != 1
        or not isinstance(changed[0], dict)
        or changed[0].get("filename") != target
        or changed[0].get("status") != "added"):
        raise Refused("claim_diff_outside_exact_fixture_scope")


def _create_claim(api: GitHub, base: str, branch: str, target: str, body: str) -> str:
    """Create one atomic branch claim and reconcile uncertain ref creation."""
    try:
        api.call("GET", "/contents/" + urllib.parse.quote(target, safe="/") + "?ref=" + base)
    except ApiFailure as exc:
        if exc.status != 404:
            raise
    else:
        raise Refused("fixture_target_already_exists_at_trusted_base")
    base_commit = api.call("GET", "/git/commits/" + base)
    base_tree = base_commit.get("tree") if isinstance(base_commit, dict) else None
    tree_sha = base_tree.get("sha") if isinstance(base_tree, dict) else None
    if not isinstance(tree_sha, str) or not SHA.fullmatch(tree_sha):
        raise Refused("base_tree_unavailable")
    blob = api.call("POST", "/git/blobs", {"content": body, "encoding": "utf-8"})
    blob_sha = blob.get("sha") if isinstance(blob, dict) else None
    if not isinstance(blob_sha, str) or not SHA.fullmatch(blob_sha):
        raise Refused("claim_blob_uncertain_reconcile_before_retry")
    tree = api.call("POST", "/git/trees", {
        "base_tree": tree_sha,
        "tree": [{"path": target, "mode": "100644", "type": "blob", "sha": blob_sha}],
    })
    new_tree_sha = tree.get("sha") if isinstance(tree, dict) else None
    if not isinstance(new_tree_sha, str) or not SHA.fullmatch(new_tree_sha):
        raise Refused("claim_tree_uncertain_reconcile_before_retry")
    commit = api.call("POST", "/git/commits", {
        "message": "test-only: reserve and implement " + branch,
        "tree": new_tree_sha, "parents": [base],
    })
    proposed = commit.get("sha") if isinstance(commit, dict) else None
    if not isinstance(proposed, str) or not SHA.fullmatch(proposed):
        raise Refused("claim_commit_uncertain_reconcile_before_retry")
    try:
        api.call("POST", "/git/refs", {
            "ref": "refs/heads/" + branch, "sha": proposed,
        })
    except (ApiFailure, Refused):
        # Branch creation can succeed even if the response is lost. Read back
        # the single canonical ref; never blindly create a competing branch.
        current = _api_branch(api, branch)
        if current is None:
            raise Refused("claim_creation_uncertain_reconcile_before_retry")
        _verify_claim(api, current, base, target, body)
        return current
    current = _api_branch(api, branch)
    if current != proposed:
        raise Refused("canonical_claim_ref_moved")
    return proposed


def produce(api: GitHub, *, config: dict, queue: dict, readiness: dict,
            dispatch: dict, budget: dict, actors: dict, repo: str, actor: str, wu: str,
            checkout_sha: str, actions: bool, enabled: bool) -> dict:
    """Reserve one Git ref atomically, then create/adopt one canonical open PR."""
    if api.repository != repo:
        raise Refused("project_identity_mismatch")
    meta = api.call("GET", "/")
    if (not isinstance(meta, dict) or meta.get("private") is not False
            or meta.get("visibility") != "public"):
        raise Refused("public_disposable_runner_requirement_not_proven")
    default = meta.get("default_branch")
    expected_default = config.get("project", {}).get("default_branch")
    if default != expected_default or not isinstance(default, str):
        raise Refused("trusted_default_branch_mismatch")
    base_data = api.call("GET", "/git/ref/heads/" + default)
    base_object = base_data.get("object") if isinstance(base_data, dict) else None
    base = base_object.get("sha") if isinstance(base_object, dict) else None
    if not isinstance(base, str) or base != checkout_sha:
        raise Refused("trusted_checkout_or_base_moved")
    branch, target = preflight(
        config, queue, readiness, dispatch, budget, actors, repo=repo, actor=actor, wu=wu,
        base=base, actions=actions, enabled=enabled,
    )
    body = fixture_body(repo, wu, actor, base)
    # Unmanaged existing PRs/branches cannot be overwritten or reclassified.
    found = _matching_pulls(api, branch)
    if len(found) > 1:
        raise Refused("duplicate_canonical_pr_inventory")
    head = _api_branch(api, branch)
    if head is None:
        if found:
            raise Refused("pr_exists_without_canonical_branch")
        head = _create_claim(api, base, branch, target, body)
    _verify_claim(api, head, base, target, body)
    found = _matching_pulls(api, branch)
    if len(found) > 1:
        raise Refused("duplicate_canonical_pr_inventory")
    if found:
        pr = found[0]
        pr_base = pr.get("base") if isinstance(pr, dict) else None
        if not isinstance(pr_base, dict) or pr_base.get("ref") != default:
            raise Refused("pr_targets_foreign_base")
        if pr.get("state") != "open" or pr.get("draft") is True:
            raise Refused("canonical_pr_closed_or_draft")
    else:
        if _api_branch(api, default) != base:
            raise Refused("base_moved_before_pr_creation")
        try:
            pr = api.call("POST", "/pulls", {
                "title": f"test-only: A4 isolated producer {wu}",
                "head": branch, "base": default,
                "body": f"Disposable A4 pilot for {wu}; no deployment or merge authority.",
                "draft": False,
            })
        except (ApiFailure, Refused):
            # A successful API mutation can lose its response. Never retry POST
            # without inventory reconciliation and a fresh independent run.
            raise Refused("pr_creation_uncertain_reconcile_before_retry") from None
    if not isinstance(pr, dict):
        raise Refused("pr_creation_uncertain_reconcile_before_retry")
    number = pr.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise Refused("pr_creation_uncertain_reconcile_before_retry")
    live = api.call("GET", "/pulls/" + str(number))
    if not isinstance(live, dict):
        raise Refused("created_pr_exact_identity_drift")
    live_head = live.get("head")
    live_base = live.get("base")
    if not isinstance(live_head, dict) or not isinstance(live_base, dict):
        raise Refused("created_pr_exact_identity_drift")
    head_repo = live_head.get("repo")
    base_repo = live_base.get("repo")
    if not isinstance(head_repo, dict) or not isinstance(base_repo, dict):
        raise Refused("created_pr_exact_identity_drift")
    if (live.get("state") != "open"
        or live.get("draft") is True
        or live_head.get("sha") != head
        or live_head.get("ref") != branch
        or head_repo.get("full_name") != repo
        or live_base.get("sha") != base
        or live_base.get("ref") != default
        or base_repo.get("full_name") != repo):
        raise Refused("created_pr_exact_identity_drift")
    return {
        "status": "PR_CREATED_OR_RECONCILED",
        "repository": repo, "work_unit": wu, "actor": actor,
        "branch": branch, "pr": number, "head": head,
        "base": base, "fixture_path": target,
        "note": "Source CI, independent review and merge remain separate gates.",
    }


def main() -> int:
    """Execute the reviewed adapter and print run-bound producer evidence."""
    try:
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        token = os.environ.get("GH_TOKEN", "")
        actor = os.environ.get("A4_ACTOR", "")
        wu = os.environ.get("A4_WORK_UNIT", "")
        run_id = os.environ.get("GITHUB_RUN_ID", "")
        run_attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
        if not (run_id.isdecimal() and int(run_id) > 0
                and run_attempt.isdecimal() and int(run_attempt) > 0):
            raise Refused("immutable_github_run_identity_missing")
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True,
            check=True,
        ).stdout.strip()
        answer = produce(
            GitHub(repo, token),
            config=load_json(CONTROL / "config.json"),
            queue=load_json(CONTROL / "queue.json"),
            readiness=load_json(CONTROL / "readiness.json"),
            dispatch=load_json(CONTROL / "dispatch.json"),
            budget=load_json(CONTROL / "budget.json"),
            actors=load_json(CONTROL / "actors.json"),
            repo=repo, actor=actor, wu=wu, checkout_sha=sha,
            actions=os.environ.get("GITHUB_ACTIONS") == "true",
            enabled=os.environ.get("ONECOMPANY_A4_PRODUCER_ENABLED") == "true",
        )
    except (Refused, subprocess.CalledProcessError, OSError) as exc:
        print("A4_REFUSED: " + str(exc), file=sys.stderr)
        return 2
    answer["run_id"] = int(run_id)
    answer["run_attempt"] = int(run_attempt)
    print("A4_PRODUCER_EVIDENCE:" + json.dumps(answer, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
