#!/usr/bin/env python3
"""Read-only, run-bound verifier for two disposable A4 installations.

The actual producer result is recovered from the successful GitHub Actions
job's immutable log, never trusted from a self-asserted manifest field.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from pathlib import Path
from typing import Any

from a4_pr_producer import GitHub, Refused, SHA, REPO, branch_for, fixture_body

EVIDENCE_PREFIX = "A4_PRODUCER_EVIDENCE:"
WORKFLOW_PATH = ".github/workflows/onecompany-a4-pr-producer.yml"
PRODUCER_JOB = "bounded-first-pr"
PRODUCER_STEP = "Reserve one branch and create or reconcile one fixture PR"
# Both constants live in the verifier source, never in the pilot manifest.
TRUSTED_CI_WORKFLOW_PATH = ".github/workflows/onecompany-a4-fixture-validation.yml"
TRUSTED_CI_WORKFLOW_BLOB = "8480f5c8bd94187efe3ccb1effa9def51d15addd"
TRUSTED_CI_CHECK_NAME = "validate-fixture"
MAX_JOB_LOG_BYTES = 2_000_000
MAX_JOB_LOG_MEMBERS = 32


class _SafeLogRedirect(urllib.request.HTTPRedirectHandler):
    """Drop repository token on GitHub's signed, cross-host log redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise Refused("github_job_log_redirect_not_https")
        if urllib.parse.urlsplit(newurl).hostname != "api.github.com":
            redirected.remove_header("Authorization")
            redirected.remove_header("authorization")
        return redirected


def _job_log(api: GitHub, job_id: int) -> str:
    """Fetch the GitHub-hosted job log with a bounded, token-safe redirect."""
    if not isinstance(job_id, int) or isinstance(job_id, bool) or job_id <= 0:
        raise Refused("producer_job_id_invalid")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{api.repository}/actions/jobs/{job_id}/logs",
        headers={
            "Authorization": "Bearer " + api.token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.build_opener(_SafeLogRedirect()).open(
            request, timeout=20
        ) as response:
            raw = response.read(2_000_001)
    except (OSError, urllib.error.HTTPError, ValueError) as exc:
        raise Refused("github_job_log_unavailable") from exc
    if len(raw) > 2_000_000:
        raise Refused("github_job_log_too_large")
    return _decode_job_log(raw)


def _decode_job_log(raw: bytes) -> str:
    """Accept GitHub's job-log zip archive, or a raw UTF-8 log body.

    Uncompressed member sizes are summed before and after extraction so a
    small compressed archive cannot expand past MAX_JOB_LOG_BYTES.
    """
    if raw.startswith(b"PK"):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                names = [name for name in archive.namelist() if not name.endswith("/")]
                if not names:
                    raise Refused("github_job_log_empty")
                if len(names) > MAX_JOB_LOG_MEMBERS:
                    raise Refused("github_job_log_too_large")
                parts: list[str] = []
                total = 0
                for name in names:
                    info = archive.getinfo(name)
                    declared = int(info.file_size)
                    if declared < 0 or total + declared > MAX_JOB_LOG_BYTES:
                        raise Refused("github_job_log_too_large")
                    data = archive.read(name)
                    if len(data) != declared or total + len(data) > MAX_JOB_LOG_BYTES:
                        raise Refused("github_job_log_too_large")
                    total += len(data)
                    parts.append(data.decode("utf-8"))
        except Refused:
            raise
        except (OSError, UnicodeError, zipfile.BadZipFile, RuntimeError, ValueError, EOFError, zlib.error) as exc:
            raise Refused("github_job_log_not_utf8") from exc
        return "\n".join(parts)
    if len(raw) > MAX_JOB_LOG_BYTES:
        raise Refused("github_job_log_too_large")
    try:
        return raw.decode("utf-8")
    except UnicodeError as exc:
        raise Refused("github_job_log_not_utf8") from exc


def _job_evidence(log: str) -> dict[str, Any]:
    """Accept one complete JSON producer result from a job log, not prose."""
    matching = [
        line.split(EVIDENCE_PREFIX, 1)[1].strip()
        for line in log.splitlines()
        if EVIDENCE_PREFIX in line
    ]
    if len(matching) != 1:
        raise Refused("producer_job_evidence_missing_or_ambiguous")
    try:
        record = json.loads(matching[0])
    except json.JSONDecodeError as exc:
        raise Refused("producer_job_evidence_invalid") from exc
    if not isinstance(record, dict):
        raise Refused("producer_job_evidence_invalid")
    return record


def _positive_int(value: Any, refusal: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise Refused(refusal)
    return value


def _decode_content(payload: dict[str, Any]) -> bytes:
    if payload.get("encoding") != "base64" or not isinstance(payload.get("content"), str):
        raise Refused("fixture_content_unreadable")
    compact = re.sub(r"[ \t\r\n]", "", payload["content"])
    try:
        return base64.b64decode(compact, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise Refused("fixture_content_unreadable") from exc


def _require_object(value: Any, refusal: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise Refused(refusal)
    return value


def _require_list(value: Any, refusal: str) -> list[Any]:
    if not isinstance(value, list):
        raise Refused(refusal)
    return value


def _verify_installation(entry: dict[str, Any], token: str) -> dict[str, str]:
    repo = entry.get("repository")
    wu = entry.get("wu")
    expected_actor = entry.get("actor")
    base = entry.get("base_sha")
    run_id = _positive_int(entry.get("run_id"), "producer_run_id_invalid")
    number = _positive_int(entry.get("pr_number"), "pilot_pr_number_invalid")
    if not isinstance(repo, str) or not REPO.fullmatch(repo):
        raise Refused("pilot_repository_invalid")
    if not isinstance(wu, str) or not re.fullmatch(r"WU[A-Za-z0-9._-]{1,58}", wu):
        raise Refused("pilot_work_unit_invalid")
    if not isinstance(expected_actor, str) or not expected_actor:
        raise Refused("pilot_actor_invalid")
    if not isinstance(base, str) or not SHA.fullmatch(base):
        raise Refused("pilot_base_sha_invalid")
    api = GitHub(repo, token)
    metadata = _require_object(api.call("GET", "/"), "pilot_repository_metadata_invalid")
    if metadata.get("full_name") != repo or metadata.get("visibility") != "public" or metadata.get("private") is not False:
        raise Refused("pilot_repository_must_be_public")
    pr = _require_object(api.call("GET", f"/pulls/{number}"), "pilot_pr_invalid")
    if pr.get("state") != "open":
        raise Refused("pilot_pr_not_open")
    head = _require_object(pr.get("head"), "pilot_pr_head_invalid")
    base_ref = _require_object(pr.get("base"), "pilot_pr_base_invalid")
    head_repo = _require_object(head.get("repo"), "pilot_pr_head_invalid")
    base_repo = _require_object(base_ref.get("repo"), "pilot_pr_base_invalid")
    head_sha = head.get("sha")
    if head_repo.get("full_name") != repo or base_repo.get("full_name") != repo:
        raise Refused("pilot_pr_repository_mismatch")
    if base_ref.get("sha") != base or not isinstance(head_sha, str) or not SHA.fullmatch(head_sha):
        raise Refused("pilot_pr_lineage_mismatch")
    if head.get("ref") != branch_for(wu):
        raise Refused("pilot_branch_mismatch")

    compare = _require_object(api.call("GET", f"/compare/{base}...{head_sha}"), "pilot_compare_invalid")
    files = _require_list(compare.get("files"), "pilot_compare_invalid")
    commits = _require_list(compare.get("commits"), "pilot_compare_invalid")
    expected_path = f"docs/onecompany-fixture/{wu}.md"
    if compare.get("status") != "ahead" or len(commits) != 1 or len(files) != 1:
        raise Refused("pilot_fixture_scope_invalid")
    if files[0].get("filename") != expected_path or files[0].get("status") != "added":
        raise Refused("pilot_fixture_scope_invalid")

    fixture = _require_object(api.call("GET", f"/contents/{expected_path}?ref={head_sha}"), "fixture_content_unreadable")
    if _decode_content(fixture) != fixture_body(repo, wu, expected_actor, base).encode("utf-8"):
        raise Refused("pilot_fixture_content_invalid")

    run = _require_object(api.call("GET", f"/actions/runs/{run_id}"), "producer_run_invalid")
    if run.get("event") != "repository_dispatch" or run.get("conclusion") != "success" or run.get("head_sha") != base:
        raise Refused("producer_run_invalid")
    if run.get("path") != WORKFLOW_PATH:
        raise Refused("producer_run_workflow_invalid")
    run_repo = _require_object(run.get("repository"), "producer_run_invalid")
    if run_repo.get("full_name") != repo:
        raise Refused("producer_run_repository_mismatch")
    jobs_payload = _require_object(api.call("GET", f"/actions/runs/{run_id}/jobs?per_page=100"), "producer_jobs_invalid")
    jobs = _require_list(jobs_payload.get("jobs"), "producer_jobs_invalid")
    candidates = [job for job in jobs if isinstance(job, dict) and job.get("name") == PRODUCER_JOB]
    if len(candidates) != 1 or candidates[0].get("conclusion") != "success":
        raise Refused("producer_job_invalid")
    job = candidates[0]
    job_id = _positive_int(job.get("id"), "producer_job_id_invalid")
    steps = _require_list(job.get("steps"), "producer_job_invalid")
    producer_steps = [step for step in steps if isinstance(step, dict) and step.get("name") == PRODUCER_STEP]
    if len(producer_steps) != 1 or producer_steps[0].get("conclusion") != "success":
        raise Refused("producer_step_invalid")
    evidence = _job_evidence(_job_log(api, job_id))
    expected = {
        "repository": repo,
        "work_unit": wu,
        "actor": expected_actor,
        "pr": number,
        "head": head_sha,
        "base": base,
        "branch": branch_for(wu),
        "run_id": run_id,
        "run_attempt": run.get("run_attempt"),
    }
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise Refused("producer_job_evidence_mismatch")

    ci_workflow_path = entry.get("ci_workflow_path")
    check_name = entry.get("check_name")
    if ci_workflow_path != TRUSTED_CI_WORKFLOW_PATH or check_name != TRUSTED_CI_CHECK_NAME:
        raise Refused("trusted_ci_identity_mismatch")
    workflow_at_base = _require_object(api.call("GET", f"/contents/{TRUSTED_CI_WORKFLOW_PATH}?ref={base}"), "trusted_ci_workflow_missing")
    if workflow_at_base.get("type") != "file" or workflow_at_base.get("sha") != TRUSTED_CI_WORKFLOW_BLOB:
        raise Refused("trusted_ci_workflow_mismatch")
    checks_payload = _require_object(api.call("GET", f"/commits/{head_sha}/check-runs?per_page=100"), "pilot_checks_invalid")
    checks = _require_list(checks_payload.get("check_runs"), "pilot_checks_invalid")
    trusted = []
    for check in checks:
        if not isinstance(check, dict) or check.get("name") != TRUSTED_CI_CHECK_NAME or check.get("conclusion") != "success" or check.get("head_sha") != head_sha:
            continue
        app = check.get("app")
        if not isinstance(app, dict) or app.get("slug") != "github-actions":
            continue
        details = check.get("details_url")
        if isinstance(details, str):
            match = re.fullmatch(r"https://github\.com/[^/]+/[^/]+/actions/runs/(\d+)/job/\d+", details)
            if match:
                trusted.append(int(match.group(1)))
    if len(trusted) != 1:
        raise Refused("trusted_exact_head_ci_missing_or_ambiguous")
    ci_run = _require_object(api.call("GET", f"/actions/runs/{trusted[0]}"), "trusted_ci_run_invalid")
    ci_repo = _require_object(ci_run.get("repository"), "trusted_ci_run_invalid")
    if ci_run.get("conclusion") != "success" or ci_run.get("head_sha") != head_sha or ci_run.get("path") != TRUSTED_CI_WORKFLOW_PATH or ci_repo.get("full_name") != repo:
        raise Refused("trusted_exact_head_ci_invalid")
    return {"repository": repo, "owner": repo.split("/", 1)[0], "head": head_sha}


def verify_pair(entries: list[dict[str, Any]], token: str) -> dict[str, Any]:
    """One shared fail-closed verification path for CLI and offline tests."""
    if not isinstance(entries, list) or len(entries) != 2 or not all(
        isinstance(entry, dict) for entry in entries
    ):
        raise Refused("manifest_requires_exactly_two_pilots")
    verified = [_verify_installation(item, token) for item in entries]
    if (verified[0]["repository"] == verified[1]["repository"]
            or verified[0]["owner"] == verified[1]["owner"]):
        raise Refused("installations_must_be_distinct_repositories")
    return {"result": "TWO_REAL_ISOLATED_A4_PILOTS_VERIFIED",
            "installations": verified}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        print("A4_QUALIFY_REFUSED: missing_token", file=sys.stderr)
        return 2
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise Refused("manifest_invalid")
        pilots = manifest.get("pilots")
        verified = verify_pair(pilots, token)["installations"]
    except (OSError, UnicodeError, json.JSONDecodeError, Refused) as exc:
        reason = str(exc) if isinstance(exc, Refused) else "manifest_invalid"
        print("A4_QUALIFY_REFUSED: " + reason, file=sys.stderr)
        return 2
    print(json.dumps({"status": "TWO_REAL_ISOLATED_A4_PILOTS_VERIFIED", "pilots": verified}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
