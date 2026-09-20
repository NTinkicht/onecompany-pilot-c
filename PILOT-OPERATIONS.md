# Pilot C — native lease bound same-PR L2 repair

Disposable PUBLIC fixture, not a customer deployment. Reviewed source baseline: NTinkicht/OneCompany fix-live-p3-validator-python-syntax-20260920@3b73edd9015954bdb7932747b3513811a219f5ea. Native Team Room issue #1; publisher NTinkicht. Zero additional paid spend.

The project-local queue reserves expected canonical WU-C4 PR #7, branch onecompany-a4-wu-c4, exactly docs/onecompany-fixture/WU-C4.md. This is only a reservation; GitHub's ACTUAL PR number must be independently checked before any lease event or worker operation. If it differs, STOP, amend project-local base policy, and rebind to the actual PR — never relabel a foreign PR. Old setup fixture PRs #3 and #4 were closed unmerged before live dispatch; new #5 is reserved but not yet proven by this record. No lease, CI failure, review or merge exists at installation time.

Leave ONECOMPANY_A4_PRODUCER_ENABLED unset/false (WU-C4 is already PR-bound) and ONECOMPANY_L2_FIXTURE_REPAIR_ENABLED unset/false until actual initial claim PR, intentional failed exact-head CI log, native owner-published ROLE_LEASE_ASSIGNED admission, independent reviewer capacity and dispatcher readiness are separately established. Do not forge operational events or treat the reserved PR number as proof.

After verified native admission, owner opts in ONECOMPANY_L2_FIXTURE_REPAIR_ENABLED=true, ONECOMPANY_A4_APPROVED_DISPATCHER=NTinkicht, ONECOMPANY_A4_LOGICAL_ACTOR=fixture-bot. Repair workflow appends only Repair: complete to the exact WU-C4 fixture and fails closed on missing authority. Re-run CI on repaired SHA and verify independent non-author approval, controlled merge and ledger MERGED event before claiming L2.

Preflight note: setup PR #3 and superseded fixture PR #4 were closed unmerged without dispatch after upstream security hardening; this replacement reservation targets future PR #7. Never count #3 as pilot evidence.

Canonical replacement setup uses WU-C4 and onecompany-a4-wu-c4. The abandoned WU-C branch remains untouched for audit; never delete it or force-update it. Target PR #7 must be the sole WU-C4 experiment.

The original WU-C2 canonical branch was created by superseded PR #4 and cannot be adopted as a new exact-base one-commit claim. WU-C4 is a fresh independent fixture reservation for expected PR #7. The unused experimental branch `onecompany-a4-wu-c2-p3-fixed` was not used as a canonical PR or pilot proof.

Run #35507892719 on abandoned PR #5 was invalid for repair evidence: Python SyntaxError before intended marker; source #120 fixes the reviewed workflow. Superseded PR #5 remains closed unmerged; WU-C4 reserves actual expected PR #7 following this installation PR #6. Confirm both numbers before dispatch; exact-base fixture provenance must refer to merged installation main, not earlier WU-C3.
