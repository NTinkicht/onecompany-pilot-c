"""Durable GitHub Team Room ledger helpers for distributed OneCompany runs."""
from __future__ import annotations

import base64
import datetime as dt
import json
import re
import uuid
from typing import Any
from urllib.parse import quote

from capacity_lib import implementation_availability, implementation_capacity_limit
from onecompany_lib import (
    CONTROL,
    command_exists,
    github_repo_from_config,
    github_repo_from_remote,
    load_json,
    run,
)
from planning_lib import by_id, dependency_closure, implementation_admission_violations

MARKER = "<!-- onecompany-ledger-v1 -->"
EVENT_RE = re.compile(
    r"<!-- onecompany-ledger-v1 -->\s*```json\s*(\{.*?\})\s*```", re.DOTALL
)
ADMISSION_SCHEMA = "onecompany-lease-admission-v1"
LEASE_EVENT_TYPES = {"ROLE_LEASE_ASSIGNED", "ROLE_LEASE_TRANSFERRED"}
SHA40_RE = re.compile(r"^[0-9a-fA-F]{40}$")
TRUSTED_POLICY_PATHS = {
    "queue": ".onecompany/queue.json",
    "planning": ".onecompany/planning.json",
    "actors": ".onecompany/actors.json",
    "readiness": ".onecompany/readiness.json",
    "budget": ".onecompany/budget.json",
}
TRUSTED_CONFIG_PATH = ".onecompany/config.json"
TRUSTED_LEDGER_PATH = ".onecompany/ledger.json"

# Exact Git commit SHAs and merged PR facts are immutable. Cache only successful
# verification results; transient/negative failures are deliberately never cached.
_VERIFIED_MERGE_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}


def ledger_config() -> dict[str, Any]:
    return load_json(CONTROL / "ledger.json")


def _checkout_head() -> str:
    result = run(["git", "rev-parse", "HEAD"])
    head = result.stdout.strip().lower() if result.returncode == 0 else ""
    if not SHA40_RE.fullmatch(head):
        raise RuntimeError("cannot resolve exact checked-out Git commit for ledger activation")
    return head


def _assert_ledger_runtime_activation() -> None:
    """Require clean ledger policy from the current protected default-branch tip."""
    dirty = run(
        [
            "git",
            "diff",
            "--quiet",
            "HEAD",
            "--",
            ".onecompany/ledger.json",
            ".onecompany/config.json",
        ]
    )
    if dirty.returncode != 0:
        raise RuntimeError(
            "durable ledger activation requires clean ledger/config policy from the checked-out commit"
        )
    repo = _repository()
    checkout = _checkout_head()
    default_branch, tip = _default_branch_tip(repo)
    if checkout != tip:
        raise RuntimeError(
            "durable ledger activation requires current protected default-branch tip "
            f"{default_branch}@{tip}; checkout is {checkout}"
        )


def ledger_enabled() -> bool:
    enabled = bool(ledger_config().get("enabled"))
    if enabled:
        _assert_ledger_runtime_activation()
    return enabled


def _repository() -> str:
    config = load_json(CONTROL / "config.json")
    configured = github_repo_from_config(config)
    remote = github_repo_from_remote()
    if not remote:
        raise RuntimeError("cannot derive repository identity from git origin")
    if configured != remote:
        raise RuntimeError(
            f"candidate repository identity {configured!r} differs from git origin {remote!r}"
        )
    return remote


def _repo_and_issue() -> tuple[str, int]:
    repo = _repository()
    issue = ledger_config().get("issue_number")
    if not isinstance(issue, int) or issue <= 0:
        raise RuntimeError("ledger.issue_number must be configured")
    return repo, issue


def _trusted_publishers() -> set[str]:
    return set(ledger_config().get("trusted_publisher_logins", []))


def _gh_json(args: list[str]) -> Any:
    if not command_exists("gh"):
        raise RuntimeError("gh CLI is required for durable ledger access")
    result = run(["gh", *args])
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip() or result.stdout.strip() or "gh command failed"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"GitHub returned invalid JSON: {exc}") from exc


def _event_version_allowed(
    ledger: dict[str, Any], version: Any, comment_id: Any
) -> bool:
    current = int(ledger.get("event_format_version", 1))
    accepted = {
        int(value)
        for value in ledger.get("accepted_event_versions", [current])
        if isinstance(value, int)
        or (isinstance(value, str) and value.isdigit())
    }
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version not in accepted
    ):
        return False
    if version == current:
        return True
    if version == 1:
        try:
            return int(comment_id or 0) <= int(
                ledger.get("legacy_event_max_comment_id", 0) or 0
            )
        except (TypeError, ValueError):
            return False
    return False


def list_events() -> list[dict[str, Any]]:
    _assert_ledger_runtime_activation()
    repo, issue = _repo_and_issue()
    ledger = ledger_config()
    trusted = _trusted_publishers()
    if not trusted:
        raise RuntimeError("ledger has no trusted publisher logins")
    accepted_types = set(ledger.get("accepted_event_types", []))
    raw = _gh_json(
        [
            "api",
            "--paginate",
            "--slurp",
            f"repos/{repo}/issues/{issue}/comments?per_page=100",
        ]
    )
    pages = raw if isinstance(raw, list) else [raw]
    comments: list[dict[str, Any]] = []
    for page in pages:
        if isinstance(page, list):
            comments.extend(
                item for item in page if isinstance(item, dict)
            )
        elif isinstance(page, dict):
            comments.append(page)

    events: list[dict[str, Any]] = []
    for comment in comments:
        login = ((comment.get("user") or {}).get("login"))
        if login not in trusted:
            continue
        body = comment.get("body") or ""
        if MARKER not in body:
            continue
        match = EVENT_RE.search(body)
        if not match:
            raise RuntimeError(
                f"malformed trusted ledger event in comment {comment.get('id')}"
            )
        if (
            comment.get("updated_at")
            and comment.get("created_at")
            and comment.get("updated_at") != comment.get("created_at")
        ):
            raise RuntimeError(
                f"trusted ledger event was edited after append in comment {comment.get('id')}"
            )
        try:
            event = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"invalid JSON in trusted ledger comment {comment.get('id')}: {exc}"
            ) from exc
        if not _event_version_allowed(
            ledger, event.get("version"), comment.get("id")
        ):
            raise RuntimeError(
                "unsupported or post-cutoff legacy ledger event version in "
                f"comment {comment.get('id')}"
            )
        if event.get("type") not in accepted_types:
            raise RuntimeError(
                "unsupported ledger event type in comment "
                f"{comment.get('id')}: {event.get('type')}"
            )
        if not isinstance(event.get("event_id"), str) or not event.get("event_id"):
            raise RuntimeError(
                f"trusted ledger event missing event_id in comment {comment.get('id')}"
            )
        if not isinstance(event.get("actor"), str) or not event.get("actor"):
            raise RuntimeError(
                f"trusted ledger event missing actor in comment {comment.get('id')}"
            )
        if not isinstance(event.get("payload"), dict):
            raise RuntimeError(
                "trusted ledger event payload must be object in comment "
                f"{comment.get('id')}"
            )
        event["github_comment_id"] = comment.get("id")
        event["github_comment_url"] = comment.get("html_url")
        event["github_publisher"] = login
        event["github_created_at"] = comment.get("created_at")
        events.append(event)
    events.sort(
        key=lambda item: (
            item.get("github_created_at") or "",
            int(item.get("github_comment_id") or 0),
        )
    )
    return events


def _cache_get(
    cache: dict[tuple[Any, ...], Any] | None,
    key: tuple[Any, ...],
    loader,
):
    if cache is None:
        return loader()
    if key not in cache:
        cache[key] = loader()
    return cache[key]


def _trusted_json_at_ref(
    repo: str,
    path: str,
    ref: str,
    cache: dict[tuple[Any, ...], Any] | None = None,
) -> tuple[dict[str, Any], str]:
    def load() -> tuple[dict[str, Any], str]:
        encoded_path = quote(path, safe="/")
        encoded_ref = quote(ref, safe="")
        payload = _gh_json(
            ["api", f"repos/{repo}/contents/{encoded_path}?ref={encoded_ref}"]
        )
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"trusted policy path {path} at {ref} did not resolve to an object"
            )
        blob_sha = payload.get("sha")
        if payload.get("encoding") != "base64" or not isinstance(
            payload.get("content"), str
        ):
            raise RuntimeError(
                f"trusted policy path {path} at {ref} is not decodable base64"
            )
        if not isinstance(blob_sha, str) or not blob_sha:
            raise RuntimeError(
                f"trusted policy path {path} at {ref} has no blob identity"
            )
        try:
            value = json.loads(
                base64.b64decode(payload["content"]).decode("utf-8")
            )
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"trusted policy path {path} at {ref} is invalid JSON: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise RuntimeError(
                f"trusted policy path {path} at {ref} is not a JSON object"
            )
        return value, blob_sha

    return _cache_get(cache, ("trusted-json", repo, path, ref), load)


def _repository_metadata(
    repo: str, cache: dict[tuple[Any, ...], Any] | None = None
) -> dict[str, Any]:
    def load() -> dict[str, Any]:
        value = _gh_json(["api", f"repos/{repo}"])
        if not isinstance(value, dict):
            raise RuntimeError(
                "cannot resolve repository metadata for lease admission"
            )
        return value

    return _cache_get(cache, ("repo", repo), load)


def _default_branch_tip(
    repo: str, cache: dict[tuple[Any, ...], Any] | None = None
) -> tuple[str, str]:
    def load() -> tuple[str, str]:
        repository = _repository_metadata(repo, cache)
        default_branch = repository.get("default_branch")
        if not isinstance(default_branch, str) or not default_branch:
            raise RuntimeError(
                "cannot resolve repository default branch for lease admission"
            )
        branch = _gh_json(
            [
                "api",
                f"repos/{repo}/branches/{quote(default_branch, safe='')}",
            ]
        )
        tip = (
            ((branch or {}).get("commit") or {}).get("sha")
            if isinstance(branch, dict)
            else None
        )
        if not isinstance(tip, str) or not SHA40_RE.fullmatch(tip):
            raise RuntimeError(
                "cannot resolve default-branch tip for lease admission"
            )
        return default_branch, tip

    return _cache_get(cache, ("default-tip", repo), load)


def _compare(
    repo: str,
    base: str,
    head: str,
    cache: dict[tuple[Any, ...], Any] | None = None,
) -> dict[str, Any]:
    def load() -> dict[str, Any]:
        value = _gh_json(["api", f"repos/{repo}/compare/{base}...{head}"])
        if not isinstance(value, dict):
            raise RuntimeError(
                f"cannot compare GitHub commits {base}...{head}"
            )
        return value

    return _cache_get(cache, ("compare", repo, base, head), load)


def _pull_request(
    repo: str,
    pr: int,
    cache: dict[tuple[Any, ...], Any] | None = None,
) -> dict[str, Any]:
    def load() -> dict[str, Any]:
        value = _gh_json(["api", f"repos/{repo}/pulls/{pr}"])
        if not isinstance(value, dict):
            raise RuntimeError(f"cannot resolve GitHub PR #{pr}")
        return value

    return _cache_get(cache, ("pr", repo, pr), load)


def _assert_trusted_default_branch_history(
    repo: str,
    ref: str,
    cache: dict[tuple[Any, ...], Any] | None = None,
) -> None:
    if not SHA40_RE.fullmatch(ref):
        raise RuntimeError(
            "lease trusted_ref must be an exact 40-hex commit SHA"
        )

    def verify() -> bool:
        _default_branch, tip = _default_branch_tip(repo, cache)
        comparison = _compare(repo, ref, tip, cache)
        status = comparison.get("status")
        if status not in {"ahead", "identical"}:
            raise RuntimeError(
                f"lease trusted_ref {ref} is not verifiably in protected "
                f"default-branch history (status={status})"
            )
        return True

    _cache_get(cache, ("default-history", repo, ref), verify)


def _trusted_policy_snapshot(
    repo: str,
    ref: str,
    cache: dict[tuple[Any, ...], Any] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    def load() -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        _assert_trusted_default_branch_history(repo, ref, cache)
        docs: dict[str, dict[str, Any]] = {}
        blobs: dict[str, str] = {}
        for key, path in TRUSTED_POLICY_PATHS.items():
            docs[key], blobs[key] = _trusted_json_at_ref(
                repo, path, ref, cache
            )
        return docs, blobs

    return _cache_get(cache, ("policy-snapshot", repo, ref), load)


def _normalize_pr(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return int(value)
    return None


def _verify_pr_base_binding(
    repo: str,
    pr: int,
    trusted_ref: str,
    cache: dict[tuple[Any, ...], Any] | None = None,
) -> dict[str, Any]:
    value = _pull_request(repo, pr, cache)
    base = value.get("base") or {}
    base_sha = base.get("sha") if isinstance(base, dict) else None
    base_ref = base.get("ref") if isinstance(base, dict) else None
    default_branch, _tip = _default_branch_tip(repo, cache)
    if base_ref != default_branch:
        raise RuntimeError(
            f"lease PR #{pr} targets {base_ref!r}, not protected default branch "
            f"{default_branch!r}"
        )
    if base_sha != trusted_ref:
        raise RuntimeError(
            f"lease trusted_ref {trusted_ref} does not match GitHub PR #{pr} "
            f"base {base_sha}"
        )
    _assert_trusted_default_branch_history(repo, trusted_ref, cache)
    return value


def trusted_pr_base(pr: int) -> str:
    repo = _repository()
    cache: dict[tuple[Any, ...], Any] = {}
    value = _pull_request(repo, pr, cache)
    base = value.get("base") or {}
    base_sha = base.get("sha") if isinstance(base, dict) else None
    if not isinstance(base_sha, str) or not SHA40_RE.fullmatch(base_sha):
        raise RuntimeError(f"cannot resolve exact PR #{pr} base SHA")
    _verify_pr_base_binding(repo, pr, base_sha, cache)
    return base_sha


def _normalize_lifecycle_policy(ledger: dict[str, Any]) -> dict[str, Any]:
    configured = ledger.get("lease_lifecycle")
    if not isinstance(configured, dict):
        raise RuntimeError("base-trusted ledger has no lease_lifecycle policy")
    ttl = configured.get("ttl_seconds")
    kinds = configured.get("renewal_progress_kinds")
    implicit = configured.get("implicit_expiry_revokes_authority")
    if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl <= 0:
        raise RuntimeError("base-trusted lease TTL must be a positive integer")
    if (
        not isinstance(kinds, list)
        or not kinds
        or any(not isinstance(value, str) or not value for value in kinds)
    ):
        raise RuntimeError(
            "base-trusted renewal_progress_kinds must be a non-empty string array"
        )
    if implicit is not True:
        raise RuntimeError(
            "base-trusted lifecycle must revoke authority on implicit expiry"
        )
    return {
        "ttl_seconds": ttl,
        "renewal_progress_kinds": sorted(set(kinds)),
        "implicit_expiry_revokes_authority": True,
    }


def trusted_runtime_context(
    trusted_ref: str,
    pr: int,
    *,
    cache: dict[tuple[Any, ...], Any] | None = None,
) -> dict[str, Any]:
    """Load consequential runtime policy from the exact protected PR base."""
    repo = _repository()
    _verify_pr_base_binding(repo, pr, trusted_ref, cache)
    config, config_blob = _trusted_json_at_ref(
        repo, TRUSTED_CONFIG_PATH, trusted_ref, cache
    )
    ledger, ledger_blob = _trusted_json_at_ref(
        repo, TRUSTED_LEDGER_PATH, trusted_ref, cache
    )
    return {
        "config": config,
        "config_blob_sha": config_blob,
        "ledger": ledger,
        "ledger_blob_sha": ledger_blob,
        "lease_lifecycle": _normalize_lifecycle_policy(ledger),
    }


def trusted_lease_lifecycle(
    trusted_ref: str,
    pr: int,
    *,
    cache: dict[tuple[Any, ...], Any] | None = None,
) -> tuple[dict[str, Any], str]:
    context = trusted_runtime_context(trusted_ref, pr, cache=cache)
    return dict(context["lease_lifecycle"]), str(context["ledger_blob_sha"])


def _normalized_planning_snapshot(
    item: dict[str, Any], work_map: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    wu = str(item.get("id") or "")
    if not wu:
        raise RuntimeError("trusted work unit has no id")
    return {
        "write_scope": list(item.get("write_scope", [])),
        "resource_locks": list(item.get("resource_locks", [])),
        "parallelism": item.get("parallelism", "auto"),
        "risk_class": item.get("risk_class", "MEDIUM"),
        "dependencies": [str(value) for value in item.get("dependencies", [])],
        "dependency_closure": sorted(dependency_closure(work_map, wu)),
    }


def trusted_admission_context(
    trusted_ref: str,
    actor: str,
    work_unit: str | None,
    active: list[dict[str, Any]],
    *,
    pr: int | None = None,
    cache: dict[tuple[Any, ...], Any] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Reconstruct admission authority from the exact platform-bound PR base."""
    try:
        del active
        repo = _repository()
        if pr is None:
            raise RuntimeError("v2 implementation lease requires a PR number")
        _verify_pr_base_binding(repo, pr, trusted_ref, cache)
        docs, blobs = _trusted_policy_snapshot(repo, trusted_ref, cache)
        runtime = trusted_runtime_context(trusted_ref, pr, cache=cache)

        actors = docs["actors"].get("actors", [])
        readiness = docs["readiness"].get("actors", [])
        actor_record = next(
            (
                item
                for item in actors
                if isinstance(item, dict) and item.get("id") == actor
            ),
            None,
        )
        ready = next(
            (
                item
                for item in readiness
                if isinstance(item, dict) and item.get("actor_id") == actor
            ),
            None,
        )
        if actor_record is None:
            hard_reasons = ["unknown_actor"]
            actor_limit = 0
        else:
            _slots, reasons = implementation_availability(
                actor_record,
                ready,
                docs["budget"],
                [],
            )
            hard_reasons = sorted(
                {reason for reason in reasons if reason != "actor_capacity"}
            )
            actor_limit = implementation_capacity_limit(ready)

        work_map = by_id(docs["queue"].get("work_units", []))
        work_item = None
        planning_snapshot = None
        if work_unit is not None:
            work_item = work_map.get(work_unit)
            if work_item is None:
                raise RuntimeError(
                    f"work unit {work_unit} does not exist at trusted_ref {trusted_ref}"
                )
            mapped_pr = _normalize_pr(work_item.get("pr"))
            if mapped_pr != pr:
                raise RuntimeError(
                    f"work unit {work_unit} is mapped to PR {mapped_pr}, not lease PR {pr}"
                )
            if work_item.get("status") != "READY":
                raise RuntimeError(
                    f"work unit {work_unit} is not READY at trusted_ref {trusted_ref} "
                    f"(status={work_item.get('status')})"
                )
            planning_snapshot = _normalized_planning_snapshot(
                work_item, work_map
            )

        return {
            "trusted_ref": trusted_ref,
            "policy_blobs": blobs,
            "planning": docs["planning"],
            "work_map": work_map,
            "work_item": work_item,
            "planning_snapshot": planning_snapshot,
            "actor_limit": actor_limit,
            "actor_eligible": not hard_reasons,
            "actor_ineligibility_reasons": hard_reasons,
            "config": runtime["config"],
            "config_blob_sha": runtime["config_blob_sha"],
            "ledger": runtime["ledger"],
            "ledger_blob_sha": runtime["ledger_blob_sha"],
            "lease_lifecycle": runtime["lease_lifecycle"],
        }, None
    except Exception as exc:
        return None, str(exc)


def clear_verified_merge_cache() -> None:
    _VERIFIED_MERGE_CACHE.clear()


def _verify_merged_event(
    event: dict[str, Any],
    cache: dict[tuple[Any, ...], Any] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Verify a MERGED claim against GitHub before it can unlock dependencies."""
    try:
        payload = event.get("payload") or {}
        pr = _normalize_pr(payload.get("pr"))
        work_unit = payload.get("work_unit")
        merge_sha = payload.get("merge_sha")
        if pr is None:
            raise RuntimeError("MERGED event requires a positive PR number")
        if not isinstance(work_unit, str) or not work_unit:
            raise RuntimeError("MERGED event requires work_unit")
        if not isinstance(merge_sha, str) or not SHA40_RE.fullmatch(merge_sha):
            raise RuntimeError("MERGED event requires exact merge_sha")

        repo = _repository()
        immutable_key = (
            repo,
            pr,
            work_unit,
            merge_sha,
            payload.get("approved_base"),
            payload.get("approved_head"),
        )
        cached = _VERIFIED_MERGE_CACHE.get(immutable_key)
        if cached is not None:
            return dict(cached), None

        pr_doc = _pull_request(repo, pr, cache)
        if not pr_doc.get("merged_at"):
            raise RuntimeError(f"GitHub PR #{pr} is not merged")
        if pr_doc.get("merge_commit_sha") != merge_sha:
            raise RuntimeError(
                f"MERGED event merge_sha {merge_sha} does not match GitHub PR #{pr} "
                f"merge commit {pr_doc.get('merge_commit_sha')}"
            )
        base = pr_doc.get("base") or {}
        head = pr_doc.get("head") or {}
        base_sha = base.get("sha") if isinstance(base, dict) else None
        head_sha = head.get("sha") if isinstance(head, dict) else None
        default_branch, _tip = _default_branch_tip(repo, cache)
        if (
            base.get("ref") if isinstance(base, dict) else None
        ) != default_branch:
            raise RuntimeError(
                f"merged PR #{pr} did not target protected default branch"
            )
        if not isinstance(base_sha, str) or not SHA40_RE.fullmatch(base_sha):
            raise RuntimeError(f"cannot resolve merged PR #{pr} base SHA")
        if not isinstance(head_sha, str) or not SHA40_RE.fullmatch(head_sha):
            raise RuntimeError(f"cannot resolve merged PR #{pr} head SHA")
        if payload.get("approved_base") not in {None, "", base_sha}:
            raise RuntimeError(
                "MERGED event approved_base differs from GitHub PR base"
            )
        if payload.get("approved_head") not in {None, "", head_sha}:
            raise RuntimeError(
                "MERGED event approved_head differs from GitHub PR head"
            )
        _assert_trusted_default_branch_history(repo, base_sha, cache)
        _assert_trusted_default_branch_history(repo, merge_sha, cache)

        queue, queue_blob = _trusted_json_at_ref(
            repo, TRUSTED_POLICY_PATHS["queue"], base_sha, cache
        )
        item = by_id(queue.get("work_units", [])).get(work_unit)
        if item is None:
            raise RuntimeError(
                f"MERGED work unit {work_unit} does not exist at verified PR base {base_sha}"
            )
        if _normalize_pr(item.get("pr")) != pr:
            raise RuntimeError(
                f"MERGED work unit {work_unit} is not mapped to GitHub PR #{pr} at verified base"
            )
        context = {
            "pr": pr,
            "work_unit": work_unit,
            "approved_base": base_sha,
            "approved_head": head_sha,
            "merge_sha": merge_sha,
            "queue_blob_sha": queue_blob,
            "merged_at": pr_doc.get("merged_at"),
        }
        _VERIFIED_MERGE_CACHE[immutable_key] = dict(context)
        return context, None
    except Exception as exc:
        return None, str(exc)


def _v2_payload_error(
    event_type: str, actor: str, payload: dict[str, Any]
) -> str | None:
    if event_type not in LEASE_EVENT_TYPES:
        return None
    admission = payload.get("admission_snapshot")
    if not isinstance(admission, dict):
        return "v2 implementation lease event requires admission_snapshot"
    if admission.get("schema") != ADMISSION_SCHEMA:
        return (
            f"unsupported lease admission schema: {admission.get('schema')!r}"
        )
    if admission.get("actor") != actor:
        return "lease admission actor does not match event actor"
    trusted_ref = admission.get("trusted_ref")
    if not isinstance(trusted_ref, str) or not SHA40_RE.fullmatch(trusted_ref):
        return "lease admission trusted_ref must be an exact 40-hex commit SHA"
    policy_blobs = admission.get("policy_blobs")
    if not isinstance(policy_blobs, dict) or any(
        not isinstance(policy_blobs.get(key), str) or not policy_blobs.get(key)
        for key in TRUSTED_POLICY_PATHS
    ):
        return (
            "lease admission policy_blobs must bind every trusted admission policy file"
        )
    limit = admission.get("actor_limit")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        return "lease admission actor_limit must be a positive integer"
    dependencies = admission.get("dependencies")
    if not isinstance(dependencies, list) or any(
        not isinstance(value, str) for value in dependencies
    ):
        return "lease admission dependencies must be a string array"
    if (
        event_type == "ROLE_LEASE_ASSIGNED"
        and admission.get("transfer_source_lease_id") not in {None, ""}
    ):
        return "assignment admission cannot name a transfer source"
    if (
        event_type == "ROLE_LEASE_TRANSFERRED"
        and admission.get("transfer_source_lease_id")
        != payload.get("old_lease_id")
    ):
        return "transfer admission source does not match old_lease_id"
    return None


def post_event(
    event_type: str, actor: str, payload: dict[str, Any]
) -> dict[str, Any]:
    ledger = ledger_config()
    if not ledger.get("enabled"):
        raise RuntimeError("durable ledger is disabled")
    _assert_ledger_runtime_activation()
    if event_type not in set(ledger.get("accepted_event_types", [])):
        raise RuntimeError(f"unsupported ledger event type: {event_type}")
    trusted = _trusted_publishers()
    if not trusted:
        raise RuntimeError(
            "ledger requires at least one trusted publisher login"
        )
    version = int(ledger.get("event_format_version", 1))
    if version >= 2:
        payload_error = _v2_payload_error(event_type, actor, payload)
        if payload_error:
            raise RuntimeError(payload_error)
    repo, issue = _repo_and_issue()
    event = {
        "version": version,
        "event_id": str(uuid.uuid4()),
        "type": event_type,
        "actor": actor,
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "payload": payload,
    }
    body = (
        f"{MARKER}\n```json\n"
        f"{json.dumps(event, separators=(',', ':'), ensure_ascii=False)}\n```\n"
    )
    posted = _gh_json(
        [
            "api",
            "--method",
            "POST",
            f"repos/{repo}/issues/{issue}/comments",
            "-f",
            f"body={body}",
        ]
    )
    login = ((posted.get("user") or {}).get("login"))
    if login not in trusted:
        comment_id = posted.get("id")
        if comment_id:
            run(
                [
                    "gh",
                    "api",
                    "--method",
                    "DELETE",
                    f"repos/{repo}/issues/comments/{comment_id}",
                ]
            )
        raise RuntimeError(
            f"ledger publisher {login!r} is not trusted; event rejected"
        )
    event["github_comment_id"] = posted.get("id")
    event["github_comment_url"] = posted.get("html_url")
    event["github_publisher"] = login
    event["github_created_at"] = posted.get("created_at")
    return event


def _conflict_id(event: dict[str, Any], reason: str) -> str:
    return f"{event.get('event_id') or '<missing>'}:{reason}"


def project_view(view: dict[str, Any], pr: int) -> dict[str, Any]:
    """Purely project one already-derived global ledger view to one PR."""
    result = dict(view)
    active = [
        item
        for item in view.get("active_leases", [])
        if item.get("pr") == pr
    ]
    authors_by_pr = view.get("material_authors_by_pr", {})
    authors = authors_by_pr.get(pr)
    if authors is None:
        authors = authors_by_pr.get(str(pr), [])
    gates = view.get("gates_by_pr", {})
    gate = gates.get(pr)
    if gate is None:
        gate = gates.get(str(pr))
    allowed_ids = {str(item.get("id") or "") for item in active}
    eligibility = {
        key: value
        for key, value in view.get("current_actor_eligibility", {}).items()
        if key in allowed_ids
    }
    result["active_leases"] = active
    result["material_authors"] = sorted(
        {str(value) for value in (authors or []) if value}
    )
    result["current_gate"] = gate
    result["current_actor_eligibility"] = eligibility
    return result


def derive(
    events: list[dict[str, Any]],
    pr: int | None = None,
    *,
    enforce_actor_policy: bool = True,
    verify_admission_provenance: bool | None = None,
) -> dict[str, Any]:
    """Replay durable events into the authoritative coordination view."""
    if verify_admission_provenance is None:
        verify_admission_provenance = enforce_actor_policy

    active: dict[str, dict[str, Any]] = {}
    authors_by_pr: dict[int, set[str]] = {}
    gates_by_pr: dict[int, dict[str, Any]] = {}
    rejected_claims: list[dict[str, Any]] = []
    open_integrity_conflicts: dict[str, dict[str, Any]] = {}
    resolved_conflict_ids: set[str] = set()
    seen_event_ids: set[str] = set()
    known_implementation_leases: set[str] = set()
    merged_work_units: set[str] = set()
    verified_merged_work_units: set[str] = set()
    provenance_cache: dict[tuple[Any, ...], Any] = {}
    verified_merges: dict[str, dict[str, Any]] = {}
    merge_errors: dict[str, str] = {}

    if verify_admission_provenance:
        for candidate_event in events:
            if candidate_event.get("type") != "MERGED":
                continue
            event_id = str(candidate_event.get("event_id") or "")
            context, error = _verify_merged_event(
                candidate_event, provenance_cache
            )
            if context is not None:
                verified_merges[event_id] = context
            else:
                merge_errors[event_id] = (
                    error or "cannot verify MERGED event"
                )

    try:
        ledger_settings = ledger_config()
    except Exception:
        ledger_settings = {}
    legacy_limits = {
        str(actor): int(limit)
        for actor, limit in (
            ledger_settings.get("legacy_v1_actor_limits") or {}
        ).items()
        if isinstance(limit, int)
        and not isinstance(limit, bool)
        and limit > 0
    }
    legacy_unknown_limit = int(
        ledger_settings.get("legacy_v1_unknown_actor_limit", 1) or 1
    )
    if legacy_unknown_limit <= 0:
        legacy_unknown_limit = 1

    try:
        current_planning = load_json(CONTROL / "planning.json")
    except Exception:
        current_planning = {
            "parallel_execution": {
                "enabled": False,
                "max_concurrent_implementation_streams": 1,
                "require_write_scope_for_parallel": True,
                "critical_risk_default": "serialize",
            }
        }
    try:
        readiness_doc = load_json(CONTROL / "readiness.json")
    except Exception:
        readiness_doc = {"actors": []}
    readiness_by_actor = {
        str(item.get("actor_id")): item
        for item in readiness_doc.get("actors", [])
        if isinstance(item, dict) and item.get("actor_id")
    }
    try:
        actors_doc = load_json(CONTROL / "actors.json")
    except Exception:
        actors_doc = {"actors": []}
    actors_by_id = {
        str(item.get("id")): item
        for item in actors_doc.get("actors", [])
        if isinstance(item, dict) and item.get("id")
    }
    try:
        budget_doc = load_json(CONTROL / "budget.json")
    except Exception:
        budget_doc = {}

    def normalize_pr(value: Any) -> int | None:
        return _normalize_pr(value)

    def implementations() -> list[dict[str, Any]]:
        return [
            item
            for item in active.values()
            if item.get("role") == "implementation"
        ]

    def snapshot_item(actor_payload: dict[str, Any]) -> dict[str, Any]:
        snapshot = actor_payload.get("planning_snapshot")
        if not isinstance(snapshot, dict):
            snapshot = {}
        return {"id": actor_payload.get("work_unit"), **snapshot}

    def record_integrity_conflict(
        event: dict[str, Any], reason: str, **details: Any
    ) -> None:
        conflict = {
            "conflict_id": _conflict_id(event, reason),
            "event_id": event.get("event_id"),
            "type": event.get("type"),
            "reason": reason,
            **details,
        }
        open_integrity_conflicts[str(conflict["conflict_id"])] = conflict

    def record_rejected_claim(
        event: dict[str, Any],
        lease_id: str,
        payload: dict[str, Any],
        violations: list[dict[str, Any]],
        **details: Any,
    ) -> None:
        rejected_claims.append(
            {
                "event_id": event.get("event_id"),
                "type": event.get("type"),
                "work_unit": payload.get("work_unit"),
                "rejected_lease_id": lease_id,
                "violations": violations,
                **details,
            }
        )

    def v2_admission(
        event: dict[str, Any],
        actor: str | None,
        payload: dict[str, Any],
        source_lease: dict[str, Any] | None = None,
    ) -> tuple[
        dict[str, Any] | None,
        list[dict[str, Any]],
        str | None,
        int | None,
        dict[str, Any],
    ]:
        if int(event.get("version") or 1) < 2:
            return None, [], None, None, current_planning
        actor_id = str(actor or "")
        error = _v2_payload_error(
            str(event.get("type") or ""), actor_id, payload
        )
        if error:
            return None, [], error, None, current_planning
        admission = payload["admission_snapshot"]
        planning_snapshot = payload.get("planning_snapshot")
        if not isinstance(planning_snapshot, dict):
            return (
                None,
                [],
                "v2 implementation lease requires planning_snapshot",
                None,
                current_planning,
            )
        recorded_dependencies = sorted(
            {
                str(value)
                for value in admission.get("dependencies", [])
                if value
            }
        )
        payload_dependencies = sorted(
            {
                str(value)
                for value in planning_snapshot.get("dependencies", [])
                if value
            }
        )
        if recorded_dependencies != payload_dependencies:
            return (
                None,
                [],
                "lease admission dependency set differs from planning snapshot",
                None,
                current_planning,
            )

        trusted_planning = current_planning
        actor_limit = int(admission.get("actor_limit"))
        derived_actor_eligible = admission.get("actor_eligible") is True
        derived_actor_reasons = list(
            admission.get("actor_ineligibility_reasons") or []
        )
        snapshot_closure = planning_snapshot.get("dependency_closure")
        authoritative_dependencies = (
            sorted({str(value) for value in snapshot_closure if value})
            if isinstance(snapshot_closure, list)
            else recorded_dependencies
        )

        if verify_admission_provenance:
            trusted_ref = str(admission.get("trusted_ref") or "")
            assignment = event.get("type") == "ROLE_LEASE_ASSIGNED"
            event_pr = normalize_pr(payload.get("pr"))
            context, context_error = trusted_admission_context(
                trusted_ref,
                actor_id,
                str(payload.get("work_unit")) if assignment else None,
                implementations(),
                pr=event_pr,
                cache=provenance_cache,
            )
            if context is None:
                return (
                    None,
                    [],
                    f"cannot verify lease admission trusted_ref: {context_error}",
                    None,
                    current_planning,
                )
            if admission.get("policy_blobs") != context.get("policy_blobs"):
                return (
                    None,
                    [],
                    "lease admission policy blob identities do not match trusted_ref",
                    None,
                    current_planning,
                )
            if actor_limit != context.get("actor_limit"):
                return (
                    None,
                    [],
                    "lease admission actor_limit differs from base-trusted actor policy",
                    None,
                    current_planning,
                )
            if (admission.get("actor_eligible") is True) != bool(
                context.get("actor_eligible")
            ):
                return (
                    None,
                    [],
                    "lease admission actor_eligible differs from base-trusted actor policy",
                    None,
                    current_planning,
                )
            recorded_reasons = sorted(
                {
                    str(value)
                    for value in admission.get(
                        "actor_ineligibility_reasons", []
                    )
                }
            )
            if recorded_reasons != sorted(
                context.get("actor_ineligibility_reasons", [])
            ):
                return (
                    None,
                    [],
                    "lease admission actor eligibility reasons differ from base-trusted actor policy",
                    None,
                    current_planning,
                )
            derived_actor_eligible = bool(context.get("actor_eligible"))
            derived_actor_reasons = list(
                context.get("actor_ineligibility_reasons", [])
            )
            trusted_planning = context.get("planning") or current_planning
            if assignment:
                authoritative_snapshot = context.get("planning_snapshot")
                if planning_snapshot != authoritative_snapshot:
                    return (
                        None,
                        [],
                        "lease planning snapshot differs from base-trusted versioned work unit",
                        None,
                        trusted_planning,
                    )
                authoritative_dependencies = sorted(
                    {
                        str(value)
                        for value in (authoritative_snapshot or {}).get(
                            "dependency_closure", []
                        )
                        if value
                    }
                )
            elif (
                source_lease is not None
                and planning_snapshot != source_lease.get("planning_snapshot")
            ):
                return (
                    None,
                    [],
                    "transfer planning snapshot differs from canonical source lease",
                    None,
                    trusted_planning,
                )

        violations: list[dict[str, Any]] = []
        if not derived_actor_eligible:
            violations.append(
                {
                    "reason": "actor_implementation_ineligible_at_admission",
                    "actor": actor_id,
                    "details": derived_actor_reasons,
                }
            )
        if event.get("type") == "ROLE_LEASE_ASSIGNED":
            completed = (
                verified_merged_work_units
                if verify_admission_provenance
                else merged_work_units
            )
            unfinished = sorted(
                set(authoritative_dependencies) - completed
            )
            if unfinished:
                violations.append(
                    {
                        "reason": "dependency_not_durably_complete_at_admission",
                        "work_unit": payload.get("work_unit"),
                        "dependencies": unfinished,
                    }
                )
        return admission, violations, None, actor_limit, trusted_planning

    def add_lease(
        lease_id: str,
        actor: str | None,
        payload: dict[str, Any],
        event: dict[str, Any],
        source_lease: dict[str, Any] | None = None,
    ) -> bool:
        role = payload.get("role", "implementation")
        admission: dict[str, Any] | None = None
        if role == "implementation":
            known_implementation_leases.add(lease_id)
            candidate = snapshot_item(payload)
            (
                admission,
                frozen_violations,
                admission_error,
                verified_limit,
                trusted_planning,
            ) = v2_admission(event, actor, payload, source_lease)
            if admission_error:
                record_integrity_conflict(
                    event,
                    "invalid_lease_admission_evidence",
                    lease_id=lease_id,
                    detail=admission_error,
                )
                return False
            actor_limit = (
                int(verified_limit)
                if verified_limit is not None
                else legacy_limits.get(
                    str(actor or ""), legacy_unknown_limit
                )
            )
            violations = [
                *frozen_violations,
                *implementation_admission_violations(
                    candidate,
                    implementations(),
                    trusted_planning,
                    None,
                    actor=actor,
                    actor_limit=actor_limit,
                ),
            ]
            if violations:
                record_rejected_claim(event, lease_id, payload, violations)
                return False

        event_pr = normalize_pr(payload.get("pr"))
        active[lease_id] = {
            "id": lease_id,
            "role": role,
            "actor": actor,
            "work_unit": payload.get("work_unit"),
            "branch": payload.get("branch"),
            "pr": event_pr,
            "start_head": payload.get("start_head"),
            "planning_snapshot": payload.get("planning_snapshot", {}),
            "admission_snapshot": admission
            or payload.get("admission_snapshot"),
            "status": "active",
            "event": event,
        }
        if role == "implementation" and event_pr is not None and actor:
            authors_by_pr.setdefault(event_pr, set()).add(str(actor))
        return True

    for event in events:
        event_id = str(event.get("event_id") or "")
        if event_id in seen_event_ids:
            record_integrity_conflict(
                event, "duplicate_event_id_ignored"
            )
            continue
        if event_id:
            seen_event_ids.add(event_id)

        event_type = event.get("type")
        actor = event.get("actor")
        payload = event.get("payload") or {}
        event_pr = normalize_pr(payload.get("pr"))
        if event_pr is not None:
            authors_by_pr.setdefault(event_pr, set())

        if event_type == "ROLE_LEASE_ASSIGNED":
            if payload.get("lease_id"):
                add_lease(
                    str(payload.get("lease_id")), actor, payload, event
                )
        elif event_type == "ROLE_LEASE_RELEASED":
            if payload.get("lease_id"):
                active.pop(str(payload.get("lease_id")), None)
        elif event_type == "ROLE_LEASE_TRANSFERRED":
            old_id = str(payload.get("old_lease_id") or "")
            new_id = str(payload.get("new_lease_id") or "")
            old = active.get(old_id)
            if old is None:
                if old_id in known_implementation_leases:
                    if new_id:
                        known_implementation_leases.add(new_id)
                    record_rejected_claim(
                        event,
                        new_id,
                        payload,
                        [
                            {
                                "reason": "transfer_source_no_longer_active",
                                "old_lease_id": old_id,
                            }
                        ],
                        old_lease_id=old_id,
                    )
                else:
                    record_integrity_conflict(
                        event,
                        "transfer_source_unknown",
                        old_lease_id=old_id,
                        new_lease_id=new_id,
                    )
                continue
            if old.get("role") != "implementation":
                record_integrity_conflict(
                    event,
                    "transfer_source_not_implementation",
                    old_lease_id=old_id,
                    new_lease_id=new_id,
                )
                continue
            if int(event.get("version") or 1) >= 2:
                admission = payload.get("admission_snapshot")
                if (
                    not isinstance(admission, dict)
                    or admission.get("transfer_source_lease_id") != old_id
                ):
                    record_integrity_conflict(
                        event,
                        "invalid_lease_admission_evidence",
                        lease_id=new_id,
                        detail=(
                            "transfer admission does not bind active source lease"
                        ),
                    )
                    continue
                if payload.get("planning_snapshot") != old.get(
                    "planning_snapshot"
                ):
                    record_integrity_conflict(
                        event,
                        "invalid_lease_admission_evidence",
                        lease_id=new_id,
                        detail=(
                            "transfer planning snapshot differs from canonical source lease"
                        ),
                    )
                    continue
            active.pop(old_id, None)
            if new_id and not add_lease(
                new_id, actor, payload, event, old
            ):
                active[old_id] = old
        elif (
            event_type == "MATERIAL_AUTHOR"
            and event_pr is not None
            and actor
        ):
            authors_by_pr[event_pr].add(str(actor))
        elif event_type == "GATE" and event_pr is not None:
            gates_by_pr[event_pr] = {
                "pr": event_pr,
                "work_unit": payload.get("work_unit"),
                "sha": payload.get("sha"),
                "base_sha": payload.get("base_sha"),
                "reviewer_actor": actor,
                "review_id": payload.get("review_id"),
                "reviewer_login": payload.get("reviewer_login"),
                "review_identity": payload.get("review_identity"),
                "verdict": payload.get("verdict"),
                "material_authors": payload.get("material_authors", []),
                "evidence": payload.get("evidence", []),
                "required_checks": payload.get("required_checks", []),
                "scope_verified": payload.get("scope_verified") is True,
                "changed_files": payload.get("changed_files", []),
                "summary": payload.get("summary", ""),
                "stale": False,
                "github_comment_url": event.get("github_comment_url"),
                "github_publisher": event.get("github_publisher"),
                "timestamp": event.get("github_created_at")
                or event.get("timestamp"),
            }
        elif event_type == "INTEGRITY_CONFLICT_RESOLVED":
            conflict_id = payload.get("conflict_id")
            if isinstance(conflict_id, str) and conflict_id:
                resolved_conflict_ids.add(conflict_id)
                open_integrity_conflicts.pop(conflict_id, None)
        elif event_type == "MERGED":
            work_unit = payload.get("work_unit")
            if verify_admission_provenance:
                verified = verified_merges.get(event_id)
                if verified is None:
                    record_integrity_conflict(
                        event,
                        "invalid_merged_evidence",
                        detail=merge_errors.get(
                            event_id, "MERGED event was not verified"
                        ),
                    )
                    continue
                verified_work_unit = str(
                    verified.get("work_unit") or ""
                )
                merged_work_units.add(verified_work_unit)
                verified_merged_work_units.add(verified_work_unit)
            elif isinstance(work_unit, str) and work_unit:
                merged_work_units.add(work_unit)
                verified_merged_work_units.add(work_unit)

    for gate_pr, gate in gates_by_pr.items():
        current_authors = set(authors_by_pr.get(gate_pr, set()))
        gated_authors = set(gate.get("material_authors") or [])
        reasons: list[str] = []
        if gated_authors != current_authors:
            reasons.append("material_authorship_changed")
        if gate.get("reviewer_actor") in current_authors:
            reasons.append("reviewer_is_now_material_author")
        if reasons:
            gate["stale"] = True
            gate["stale_reasons"] = reasons

    current_actor_eligibility: dict[str, dict[str, Any]] = {}
    if enforce_actor_policy:
        all_active = implementations()
        for lease in all_active:
            lease_id = str(lease.get("id") or "")
            actor_id = str(lease.get("actor") or "")
            actor_record = actors_by_id.get(actor_id)
            if actor_record is None:
                reasons = ["unknown_actor"]
            else:
                _slots, reasons = implementation_availability(
                    actor_record,
                    readiness_by_actor.get(actor_id),
                    budget_doc,
                    all_active,
                    exclude_lease_id=lease_id,
                )
                reasons = sorted(set(reasons))
            current_actor_eligibility[lease_id] = {
                "actor": actor_id,
                "eligible": not reasons,
                "reasons": reasons,
            }

    unresolved_integrity_conflicts = list(
        open_integrity_conflicts.values()
    )
    result = {
        "active_leases": list(active.values()),
        "material_authors": sorted(
            {
                author
                for values in authors_by_pr.values()
                for author in values
            }
        ),
        "material_authors_by_pr": {
            key: sorted(values) for key, values in authors_by_pr.items()
        },
        "current_gate": None,
        "gates_by_pr": gates_by_pr,
        "rejected_claims": rejected_claims,
        "integrity_conflicts": unresolved_integrity_conflicts,
        "conflicts": unresolved_integrity_conflicts,
        "resolved_conflict_ids": sorted(resolved_conflict_ids),
        "merged_work_units": sorted(merged_work_units),
        "verified_merged_work_units": sorted(
            verified_merged_work_units
        ),
        "current_actor_eligibility": current_actor_eligibility,
    }
    if pr is not None:
        return project_view(result, pr)
    return result
