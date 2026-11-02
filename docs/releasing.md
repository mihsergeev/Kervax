# Release checks

Run targeted tests while developing. Push the final candidate early so the full
CI runs while publication privacy checks and production backups are prepared in
parallel. Do not run a second full local suite for a commit already covered by CI.
After a fix, rerun affected local tests; CI remains the full-suite authority.

The release tag must point to the exact commit with successful `main` push CI in
this repository. `ops/release-ci.py` checks the workflow identity, every required
job and test/audit step, and completion times within 24 hours. Missing, skipped,
failed, stale or unrelated evidence blocks publication. A retry of only failed
jobs can reuse recent successful jobs from the same run and commit.

The tag workflow checks that evidence instead of running the complete suite
again. It still builds and scans the actual release images for vulnerabilities
and secrets before publishing them. Dependency download caches never replace
installation, tests or audits. Superseded branch CI is cancelled automatically.

The release coordinator must keep these checks mandatory:

1. Before each publication, scan the outgoing tree and commits, notes, assets and
   metadata for secrets, personal information and private infrastructure details.
   Evidence is valid only for the reviewed commit and exact notes/assets. A change
   invalidates the corresponding check. Private registries and inventories stay
   outside the public repository.
2. Back up **every configured production panel** and verify restoration before
   deployment. Bind the results to the current release and inventory. A missing
   or failed backup blocks deployment. Run independent panel backups in parallel.
3. Deploy one canary, verify it, then deploy the remaining panels in parallel.
   Finish with a live check of **all configured panels**: version, schema, data
   counters, errors, scheduler heartbeat, frontend, agent signatures and hashes.
   Do not report completion while any panel fails or remains unchecked.

Production targets come only from the private `ops/prods`. This workflow adds no
permission to skip privacy, backups, restore verification or live panel checks.
Publish English release notes and send the same changes in Russian to the user.
