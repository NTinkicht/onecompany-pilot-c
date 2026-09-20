"""Lease lifetime, renewal, reaping and cache-independent coordination views.

Admission decides whether a lease ever became canonical. This layer decides
whether that canonical lease still has implementation authority. Durable lease
lifetime is frozen from the exact protected admission base; later candidate or
legitimate policy edits cannot retroactively lengthen historical leases.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import ledger_lib
from onecompany_lib import ROOT

RENEW_EVENT = "ROLE_LEASE_RENEWED"
REAP_EVENT = "ROLE_LEASE_REAPED"
LIFECYCLE_EVENT_TYPES = {RENEW_EVENT, REAP_EVENT}
DEFAULT_TTL_SECONDS = 14_400
SHA40_RE = ledger_lib.SHA40_RE


def _utc(value: dt.datetime | None = None) -> dt.datetime:
    current = value or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    return current.astimezone(dt.timezone.utc)


def _iso(value: dt.datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _utc(
            dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        )
    except ValueError:
        return None


def event_time(event: dict[str, Any]) -> dt.datetime:
    value = _parse_time(event.get("github_created_at")) or _parse_time(
        event.get("timestamp")
    )
    if value is None:
        raise RuntimeError(
            f"coordination event {event.get('event_id')} has no valid timestamp"
        )
    return value


def lifecycle_policy() -> dict[str, Any]:
    """Local/non-durable lifecycle policy; durable leases never trust this live value."""
    try:
        ledger = ledger_lib.ledger_config()
    except Exception:
        ledger = {}
    configured = ledger.get("lease_lifecycle")
    if not isinstance(configured, dict):
        configured = {}
    ttl = configured.get("ttl_seconds", DEFAULT_TTL_SECONDS)
    if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl <= 0:
        ttl = DEFAULT_TTL_SECONDS
    kinds = configured.get("renewal_progress_kinds", ["pr_head"])
    if not isinstance(kinds, list):
        kinds = ["pr_head"]
    return {
        "ttl_seconds": ttl,
        "renewal_progress_kinds": sorted(
            {str(item) for item in kinds if item}
        ),
        "implicit_expiry_revokes_authority": configured.get(
            "implicit_expiry_revokes_authority", True
        )
        is True,
    }


def lease_fingerprint(lease: dict[str, Any]) -> str:
    immutable = {
        "id": lease.get("id"),
        "role": lease.get("role"),
        "actor": lease.get("actor"),
        "work_unit": lease.get("work_unit"),
        "branch": lease.get("branch"),
        "pr": lease.get("pr"),
        "start_head": lease.get("start_head"),
        "parent_lease_id": lease.get("parent_lease_id"),
        "planning_snapshot": lease.get("planning_snapshot", {}),
        "admission_snapshot": lease.get("admission_snapshot"),
        "lifecycle_policy": lease.get("lifecycle_policy"),
        "lifecycle_policy_blob_sha": lease.get("lifecycle_policy_blob_sha"),
    }
    canonical = json.dumps(
        immutable,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _local_event_path() -> Path:
    return ROOT / ".git" / "onecompany" / "lease-events.jsonl"


def local_events(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or _local_event_path()
    if not target.exists():
        return []
    events: list[dict[str, Any]] = []
    with target.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"local coordination log is corrupt at line {number}: {exc}"
                ) from exc
            if not isinstance(event, dict):
                raise RuntimeError(
                    f"local coordination log line {number} is not an object"
                )
            events.append(event)
    events.sort(
        key=lambda item: (
            event_time(item),
            int(item.get("local_sequence") or 0),
            str(item.get("event_id") or ""),
        )
    )
    return events


def _append_local_event(
    event_type: str,
    actor: str,
    payload: dict[str, Any],
    *,
    now: dt.datetime | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    target = path or _local_event_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = local_events(target)
    event = {
        "version": 1,
        "event_id": str(uuid.uuid4()),
        "type": event_type,
        "actor": actor,
        "timestamp": _iso(_utc(now)),
        "payload": payload,
        "local_sequence": len(existing) + 1,
    }
    with target.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(
            json.dumps(
                event,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
        )
        handle.flush()
    return event


def coordination_events(*, local_path: Path | None = None) -> list[dict[str, Any]]:
    if ledger_lib.ledger_enabled():
        return ledger_lib.list_events()
    return local_events(local_path)


def append_coordination_event(
    event_type: str,
    actor: str,
    payload: dict[str, Any],
    *,
    now: dt.datetime | None = None,
    local_path: Path | None = None,
) -> dict[str, Any]:
    if ledger_lib.ledger_enabled():
        return ledger_lib.post_event(event_type, actor, payload)
    return _append_local_event(
        event_type,
        actor,
        payload,
        now=now,
        path=local_path,
    )


def _normalize_pr(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return int(value)
    return None


def verify_pr_head_progress(
    pr: int,
    previous_head: str,
    new_head: str,
    *,
    cache: dict[tuple[Any, ...], Any] | None = None,
) -> tuple[bool, str | None]:
    """Verify genuine forward progress on the same PR lineage."""
    if not SHA40_RE.fullmatch(previous_head or "") or not SHA40_RE.fullmatch(
        new_head or ""
    ):
        return False, "progress heads must be exact 40-hex SHAs"
    if previous_head == new_head:
        return False, "heartbeat_or_same_head_is_not_progress"
    try:
        repo = ledger_lib._repository()
        pr_doc = ledger_lib._pull_request(repo, pr, cache)
        current_head = ((pr_doc.get("head") or {}).get("sha"))
        if not isinstance(current_head, str) or not SHA40_RE.fullmatch(
            current_head
        ):
            return False, "cannot resolve current PR head"
        forward = ledger_lib._compare(
            repo, previous_head, new_head, cache
        )
        if forward.get("status") != "ahead":
            return (
                False,
                "new head is not strictly ahead of previous head "
                f"(status={forward.get('status')})",
            )
        ancestry = ledger_lib._compare(
            repo, new_head, current_head, cache
        )
        if ancestry.get("status") not in {"ahead", "identical"}:
            return (
                False,
                "recorded progress head is not on the current PR-head lineage",
            )
        return True, None
    except Exception as exc:
        return False, str(exc)


def _policy_for_event(
    event: dict[str, Any],
    payload: dict[str, Any],
    *,
    durable: bool,
    cache: dict[tuple[Any, ...], Any],
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    if not durable:
        return lifecycle_policy(), None, None
    admission = payload.get("admission_snapshot")
    if not isinstance(admission, dict):
        return None, None, "durable lease has no admission snapshot"
    trusted_ref = admission.get("trusted_ref")
    event_pr = _normalize_pr(payload.get("pr"))
    if (
        not isinstance(trusted_ref, str)
        or not SHA40_RE.fullmatch(trusted_ref)
        or event_pr is None
    ):
        return None, None, "durable lease has no exact base-trusted lifecycle context"
    try:
        policy, blob_sha = ledger_lib.trusted_lease_lifecycle(
            trusted_ref,
            event_pr,
            cache=cache,
        )
    except Exception as exc:
        return None, None, str(exc)
    return policy, blob_sha, None


def _lease_from_event(
    event: dict[str, Any],
    payload: dict[str, Any],
    *,
    policy: dict[str, Any],
    policy_blob_sha: str | None,
) -> dict[str, Any]:
    issued = event_time(event)
    lease_id = payload.get("new_lease_id") or payload.get("lease_id")
    ttl_seconds = int(policy["ttl_seconds"])
    lease = {
        "id": lease_id,
        "role": payload.get("role", "implementation"),
        "actor": event.get("actor"),
        "work_unit": payload.get("work_unit"),
        "branch": payload.get("branch"),
        "pr": _normalize_pr(payload.get("pr")),
        "start_head": payload.get("start_head"),
        "parent_lease_id": payload.get("parent_lease_id")
        or payload.get("old_lease_id"),
        "planning_snapshot": payload.get("planning_snapshot", {}),
        "admission_snapshot": payload.get("admission_snapshot"),
        "lifecycle_policy": copy.deepcopy(policy),
        "lifecycle_policy_blob_sha": policy_blob_sha,
        "status": "active",
        "issued_at": _iso(issued),
        "expires_at": _iso(
            issued + dt.timedelta(seconds=ttl_seconds)
        ),
        "last_progress_head": payload.get("start_head"),
        "last_progress_at": _iso(issued),
        "event": event,
    }
    return lease


def _rejected_base_ids(base: dict[str, Any]) -> set[str]:
    values = {
        str(item.get("rejected_lease_id"))
        for item in base.get("rejected_claims", [])
        if item.get("rejected_lease_id")
    }
    for conflict in base.get(
        "integrity_conflicts", base.get("conflicts", [])
    ):
        for key in ("lease_id", "new_lease_id", "rejected_lease_id"):
            if conflict.get(key):
                values.add(str(conflict[key]))
    return values


def _is_expired(lease: dict[str, Any], instant: dt.datetime) -> bool:
    expires = _parse_time(lease.get("expires_at"))
    return expires is None or instant >= expires


def derive_lifecycle(
    events: list[dict[str, Any]],
    pr: int | None = None,
    *,
    now: dt.datetime | None = None,
    enforce_actor_policy: bool = True,
    verify_admission_provenance: bool | None = None,
    durable: bool | None = None,
) -> dict[str, Any]:
    """Return one canonical view with admission + frozen lifetime authority."""
    is_durable = ledger_lib.ledger_enabled() if durable is None else durable
    if verify_admission_provenance is None:
        verify_admission_provenance = is_durable and enforce_actor_policy
    base = ledger_lib.derive(
        events,
        None,
        enforce_actor_policy=(enforce_actor_policy if is_durable else False),
        verify_admission_provenance=(
            verify_admission_provenance if is_durable else False
        ),
    )
    rejected_ids = _rejected_base_ids(base)
    local_policy = lifecycle_policy()
    instant = _utc(now)
    active: dict[str, dict[str, Any]] = {}
    history: dict[str, dict[str, Any]] = {}
    authors_by_pr: dict[int, set[str]] = {}
    lifecycle_rejected: list[dict[str, Any]] = []
    explicitly_reaped: list[dict[str, Any]] = []
    progress_cache: dict[tuple[Any, ...], Any] = {}
    lifecycle_policy_cache: dict[tuple[Any, ...], Any] = {}

    def reject(event: dict[str, Any], reason: str, **details: Any) -> None:
        lifecycle_rejected.append(
            {
                "event_id": event.get("event_id"),
                "type": event.get("type"),
                "reason": reason,
                **details,
            }
        )

    def remember_author(
        event: dict[str, Any], payload: dict[str, Any]
    ) -> None:
        event_pr = _normalize_pr(payload.get("pr"))
        actor = event.get("actor")
        if event_pr is not None and isinstance(actor, str) and actor:
            authors_by_pr.setdefault(event_pr, set()).add(actor)

    for event in events:
        payload = (
            event.get("payload")
            if isinstance(event.get("payload"), dict)
            else {}
        )
        kind = event.get("type")
        at = event_time(event)

        if kind == "MATERIAL_AUTHOR":
            remember_author(event, payload)
            continue

        if kind == "ROLE_LEASE_ASSIGNED":
            lease_id = str(payload.get("lease_id") or "")
            if not lease_id or lease_id in rejected_ids:
                continue
            policy, blob_sha, policy_error = _policy_for_event(
                event,
                payload,
                durable=is_durable,
                cache=lifecycle_policy_cache,
            )
            if policy is None:
                reject(
                    event,
                    "lifecycle_policy_unverified",
                    lease_id=lease_id,
                    detail=policy_error,
                )
                continue
            lease = _lease_from_event(
                event,
                payload,
                policy=policy,
                policy_blob_sha=blob_sha,
            )
            active[lease_id] = lease
            history[lease_id] = lease
            remember_author(event, payload)
            continue

        if kind == "ROLE_LEASE_RELEASED":
            lease_id = str(payload.get("lease_id") or "")
            lease = active.pop(lease_id, None)
            if lease is not None:
                lease["status"] = "released"
                lease["released_at"] = _iso(at)
                lease["release_reason"] = payload.get("reason")
            continue

        if kind == "ROLE_LEASE_TRANSFERRED":
            old_id = str(payload.get("old_lease_id") or "")
            new_id = str(payload.get("new_lease_id") or "")
            if not new_id or new_id in rejected_ids:
                continue
            old = active.get(old_id)
            if old is None:
                reject(
                    event,
                    "lifecycle_transfer_source_not_active",
                    old_lease_id=old_id,
                    new_lease_id=new_id,
                )
                continue
            if _is_expired(old, at):
                old["status"] = "expired"
                active.pop(old_id, None)
                reject(
                    event,
                    "lifecycle_transfer_source_expired",
                    old_lease_id=old_id,
                    new_lease_id=new_id,
                )
                continue
            policy, blob_sha, policy_error = _policy_for_event(
                event,
                payload,
                durable=is_durable,
                cache=lifecycle_policy_cache,
            )
            if policy is None:
                reject(
                    event,
                    "lifecycle_policy_unverified",
                    lease_id=new_id,
                    detail=policy_error,
                )
                continue
            old["status"] = "transferred"
            old["released_at"] = _iso(at)
            active.pop(old_id, None)
            lease = _lease_from_event(
                event,
                payload,
                policy=policy,
                policy_blob_sha=blob_sha,
            )
            lease["parent_lease_id"] = old_id
            active[new_id] = lease
            history[new_id] = lease
            remember_author(event, payload)
            continue

        if kind == RENEW_EVENT:
            lease_id = str(payload.get("lease_id") or "")
            lease = active.get(lease_id)
            if lease is None:
                reject(event, "renew_source_not_active", lease_id=lease_id)
                continue
            if _is_expired(lease, at):
                lease["status"] = "expired"
                active.pop(lease_id, None)
                reject(event, "renew_after_expiry", lease_id=lease_id)
                continue
            if event.get("actor") != lease.get("actor"):
                reject(event, "renew_actor_mismatch", lease_id=lease_id)
                continue
            if payload.get("lease_fingerprint") != lease_fingerprint(lease):
                reject(
                    event,
                    "renew_immutable_lease_mismatch",
                    lease_id=lease_id,
                )
                continue
            lease_policy = lease.get("lifecycle_policy")
            if not isinstance(lease_policy, dict):
                reject(
                    event,
                    "renew_lifecycle_policy_missing",
                    lease_id=lease_id,
                )
                continue
            allowed_progress = set(
                lease_policy.get("renewal_progress_kinds", [])
            )
            progress_kind = str(payload.get("progress_kind") or "")
            if progress_kind not in allowed_progress:
                reject(
                    event,
                    "renew_progress_kind_not_allowed",
                    lease_id=lease_id,
                )
                continue
            previous = str(payload.get("previous_head") or "")
            new_head = str(payload.get("new_head") or "")
            if previous != str(lease.get("last_progress_head") or ""):
                reject(
                    event,
                    "renew_previous_head_not_current",
                    lease_id=lease_id,
                )
                continue
            if progress_kind == "pr_head":
                if is_durable:
                    lease_pr = _normalize_pr(lease.get("pr"))
                    if lease_pr is None:
                        reject(
                            event, "renew_pr_missing", lease_id=lease_id
                        )
                        continue
                    valid, error = verify_pr_head_progress(
                        lease_pr,
                        previous,
                        new_head,
                        cache=progress_cache,
                    )
                    if not valid:
                        reject(
                            event,
                            "renew_progress_not_verified",
                            lease_id=lease_id,
                            detail=error,
                        )
                        continue
                elif (
                    not SHA40_RE.fullmatch(previous)
                    or not SHA40_RE.fullmatch(new_head)
                    or previous == new_head
                ):
                    reject(
                        event,
                        "heartbeat_or_invalid_local_progress",
                        lease_id=lease_id,
                    )
                    continue
            old_expiry = _parse_time(lease.get("expires_at"))
            ttl = int(lease_policy.get("ttl_seconds") or 0)
            if ttl <= 0:
                reject(
                    event,
                    "renew_lifecycle_ttl_invalid",
                    lease_id=lease_id,
                )
                continue
            proposed = at + dt.timedelta(seconds=ttl)
            if old_expiry is None or proposed <= old_expiry:
                reject(
                    event, "renewal_not_monotonic", lease_id=lease_id
                )
                continue
            lease["expires_at"] = _iso(proposed)
            lease["last_progress_head"] = new_head
            lease["last_progress_at"] = _iso(at)
            lease.setdefault("renewals", []).append(
                {
                    "event_id": event.get("event_id"),
                    "progress_kind": progress_kind,
                    "previous_head": previous,
                    "new_head": new_head,
                    "renewed_at": _iso(at),
                    "expires_at": _iso(proposed),
                    "ttl_seconds": ttl,
                    "lifecycle_policy_blob_sha": lease.get(
                        "lifecycle_policy_blob_sha"
                    ),
                }
            )
            continue

        if kind == REAP_EVENT:
            lease_id = str(payload.get("lease_id") or "")
            lease = active.get(lease_id)
            if lease is None:
                reject(event, "reap_source_not_active", lease_id=lease_id)
                continue
            if not _is_expired(lease, at):
                reject(event, "reap_before_expiry", lease_id=lease_id)
                continue
            lease["status"] = "reaped"
            lease["reaped_at"] = _iso(at)
            lease["reap_reason"] = payload.get("reason") or "lease_expired"
            active.pop(lease_id, None)
            explicitly_reaped.append(copy.deepcopy(lease))
            continue

    expired: list[dict[str, Any]] = []
    for lease_id, lease in list(active.items()):
        lease_policy = lease.get("lifecycle_policy")
        implicit = (
            isinstance(lease_policy, dict)
            and lease_policy.get("implicit_expiry_revokes_authority") is True
        )
        if implicit and _is_expired(lease, instant):
            lease["status"] = "expired"
            active.pop(lease_id, None)
            expired.append(copy.deepcopy(lease))

    active_values = [copy.deepcopy(value) for value in active.values()]
    if pr is not None:
        active_values = [
            item for item in active_values if item.get("pr") == pr
        ]
        authors = sorted(authors_by_pr.get(pr, set()))
    else:
        authors = sorted(
            {
                actor
                for values in authors_by_pr.values()
                for actor in values
            }
        )

    result = copy.deepcopy(base)
    result["active_leases"] = active_values
    result["material_authors"] = authors
    result["material_authors_by_pr"] = {
        key: sorted(values) for key, values in authors_by_pr.items()
    }
    result["expired_leases"] = [
        item for item in expired if pr is None or item.get("pr") == pr
    ]
    result["reaped_leases"] = [
        item
        for item in explicitly_reaped
        if pr is None or item.get("pr") == pr
    ]
    result["lease_history"] = [
        copy.deepcopy(item)
        for item in history.values()
        if pr is None or item.get("pr") == pr
    ]
    result["lifecycle_rejected_claims"] = lifecycle_rejected
    result["lease_lifecycle_policy"] = (
        local_policy
        if not is_durable
        else {"source": "per_lease_base_trusted"}
    )

    gates = copy.deepcopy(result.get("gates_by_pr", {}))
    for gate_pr, gate in gates.items():
        current_authors = set(authors_by_pr.get(int(gate_pr), set()))
        gated_authors = set(gate.get("material_authors") or [])
        reasons: list[str] = []
        if gated_authors != current_authors:
            reasons.append("material_authorship_changed")
        if gate.get("reviewer_actor") in current_authors:
            reasons.append("reviewer_is_now_material_author")
        gate["stale"] = bool(reasons)
        if reasons:
            gate["stale_reasons"] = reasons
        else:
            gate.pop("stale_reasons", None)
    result["gates_by_pr"] = gates
    result["current_gate"] = gates.get(pr) if pr is not None else None

    active_ids = {str(item.get("id") or "") for item in active_values}
    result["current_actor_eligibility"] = {
        key: value
        for key, value in result.get(
            "current_actor_eligibility", {}
        ).items()
        if key in active_ids
    }
    return result


def coordination_view(
    pr: int | None = None,
    *,
    now: dt.datetime | None = None,
    local_path: Path | None = None,
) -> dict[str, Any]:
    events = coordination_events(local_path=local_path)
    return derive_lifecycle(
        events,
        pr,
        now=now,
        durable=ledger_lib.ledger_enabled(),
    )


def renewal_payload(
    lease: dict[str, Any], new_head: str
) -> dict[str, Any]:
    return {
        "lease_id": lease.get("id"),
        "pr": lease.get("pr"),
        "work_unit": lease.get("work_unit"),
        "progress_kind": "pr_head",
        "previous_head": lease.get("last_progress_head")
        or lease.get("start_head"),
        "new_head": new_head,
        "lease_fingerprint": lease_fingerprint(lease),
    }


def reap_payload(
    lease: dict[str, Any], reason: str = "lease_expired"
) -> dict[str, Any]:
    return {
        "lease_id": lease.get("id"),
        "pr": lease.get("pr"),
        "work_unit": lease.get("work_unit"),
        "lease_fingerprint": lease_fingerprint(lease),
        "reason": reason,
    }
