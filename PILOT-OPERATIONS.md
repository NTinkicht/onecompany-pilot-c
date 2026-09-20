# Pilot C — native lease bound same-PR L2 repair

Disposable PUBLIC fixture, not a customer deployment. Reviewed source baseline: NTinkicht/OneCompany main@97eea23b8c36c5cea5b4a17b9c60621fa4130c61. Native Team Room issue #1; publisher NTinkicht. Zero additional paid spend.

The project-local queue reserves expected canonical WU-C PR #3, branch onecompany-a4-wu-c, exactly docs/onecompany-fixture/WU-C.md. This is only a reservation; GitHub's ACTUAL PR number must be independently checked before any lease event or worker operation. If it differs, STOP, amend project-local base policy, and rebind to the actual PR — never relabel a foreign PR. No fixture PR, lease, CI failure, review or merge exists at installation time.

Leave ONECOMPANY_A4_PRODUCER_ENABLED unset/false (WU-C is already PR-bound) and ONECOMPANY_L2_FIXTURE_REPAIR_ENABLED unset/false until actual initial claim PR, intentional failed exact-head CI log, native owner-published ROLE_LEASE_ASSIGNED admission, independent reviewer capacity and dispatcher readiness are separately established. Do not forge operational events or treat the reserved PR number as proof.

After verified native admission, owner opts in ONECOMPANY_L2_FIXTURE_REPAIR_ENABLED=true, ONECOMPANY_A4_APPROVED_DISPATCHER=NTinkicht, ONECOMPANY_A4_LOGICAL_ACTOR=fixture-bot. Repair workflow appends only Repair: complete to the exact WU-C fixture and fails closed on missing authority. Re-run CI on repaired SHA and verify independent non-author approval, controlled merge and ledger MERGED event before claiming L2.
