# Pilot C — native lease bound same-PR L2 repair

Public disposable fixture, no customer deployment, no extra paid spend. Source review: NTinkicht/OneCompany PR #120 merged to main@46f28cbbf70e0277798bb21baa16d2294529796b.

## Live execution and policy
- ONECOMPANY_A4_PRODUCER_ENABLED remains unset/false: Pilot C uses a separately prebound canonical PR.
- ONECOMPANY_L2_FIXTURE_REPAIR_ENABLED remains unset/false until the native owner-published active lease and actual intentional failing Actions run are both independently verified.
- ONECOMPANY_A4_APPROVED_DISPATCHER=NTinkicht; ONECOMPANY_A4_LOGICAL_ACTOR=fixture-bot; ONECOMPANY_EMERGENCY_STOP=false (or stop unset) are project-local opt-in inputs.
- Team Room issue #1 uses trusted publisher NTinkicht; no routine named PR reviewer.
- Reviewed validation Git blob: 9af08865958561dcd6f124b4dc91368d38fec049. Reviewed L2 worker Git blob: 35589c56dce02bcb647dba8daf14c6f57117a1ef.

## Capacity/readiness provenance
Previous lease acquire for WU-C4/PR #7 was correctly refused because target trusted readiness lacked repository_access.read, capacity.observed_at and capacity.evidence. No durable event was published. Public GitHub-hosted Actions run https://github.com/NTinkicht/onecompany-pilot-c/actions/runs/35509002954 at 2026-09-20T11:51:50–11:51:57Z demonstrates actual repository checkout, included runner, and one working stream. Job https://github.com/NTinkicht/onecompany-pilot-c/actions/runs/35509002954/job/106073529001 confirms checkout passed. Record exactly observed single-stream capacity, not fabricated throughput or other owners' evidence.

## New exact-base trial
This reviewed configuration reserves WU-C5, branch onecompany-a4-wu-c5, expected PR #9, and only docs/onecompany-fixture/WU-C5.md. Prior PRs #3/#4/#5/#7 are closed unmerged; #7's intended failure cannot be reused with a different base. After merging installation PR #8, confirm actual PR number, exact base/head; dispatch a NEW onecompany-l2-fixture-validation.yml run at onecompany-a4-wu-c5; verify one runtime L2_FIXTURE_INTENTIONAL_FAILURE:fixture_ci_repair_not_complete marker.

Acquire genuine native active implementation lease for WU-C5/PR #9 from clean current target main checkout; verify Team Room owner-published v2 ROLE_LEASE_ASSIGNED with exact admission policy blobs/base/head. Only then enable and dispatch scoped repair; revalidate repaired head, obtain independent non-author AI review, perform governed merge and owner-published v2 MERGED event. Do not claim P3 until read-only qualification passes.
