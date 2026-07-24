# AGENTS.md

Entry point for any agent working on this repo.

## Handling Greptile on pull requests

Greptile reviews every PR and leaves inline comments tagged by severity: `[P1]` (bug / correctness, must fix), `[P2]` (should fix), `[P3]` (optional).
The green "Greptile Review" check does **not** mean there are no findings, so never merge on the check colour alone - always read the comments.

Before merging any PR:

1. Fetch the comments (with `--paginate` so later pages of findings are not missed): `gh api --paginate repos/ivzc07/bienwabot/pulls/<N>/comments`
2. For every P1 and P2: diagnose it against the actual code and confirm it is real, then fix it - do not "fix" a finding without verifying it. If it is wrong or not worth fixing, dismiss it by replying on the comment thread with the reason.
3. Reply on each thread noting what you did (fix commit or dismissal reason) so nothing is silently ignored.

Greptile re-reviews on every new commit, so re-check its comments after each push, not just once.
Greptile's behaviour is configured in `.greptile/config.json` at the repo root.

## The tests gate

Do not merge unless the `test` check (`.github/workflows/tests.yml`) has completed successfully; pending, missing, or cancelled also blocks the merge.
The status check is named after the workflow's job (`test`), not the workflow's display name.
Where branch protection is enabled, `main` requires this check, so a red or absent result blocks the merge button too.
