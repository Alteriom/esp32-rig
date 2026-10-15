# 03_verify — the checks, the guide, the pull request

One job: the pull request a reviewer can merge, with the guide and screenshots brought along and the checks CI will run already green.

## Inputs
- Working (this run): `.github/icm/02-feature/stages/02_implement/output/change.md`
- Reference (every run): `.github/icm/_shared/build-and-test.md`
- Reference (every run): `.github/icm/_shared/documents.md`

Do NOT load: the other pipelines, other runs' output.

## Process
1. Run what CI runs ([build-and-test.md](../../../_shared/build-and-test.md)): the suite on Linux, both simulated suites, `runner/ci/build-release.sh --out dist`.
2. Correct the guide pages the change made wrong ([documents.md](../../../_shared/documents.md)), retake the screenshots of a visible dashboard change with `runner/screenshots/README.md`, and run `mkdocs build --strict`. Update `README.md`, `rig/AGENTS.md` or `.github/icm/_shared/` if a line there is now wrong.
3. Write the pull request: what a rig owner will notice (the subject, in plain words), the test that failed first, the checks and results, the hardware evidence or why none, and what a portal must follow with when it takes the release.
4. After the human check, open it against `main` (`gh pr create --base main --body-file <this stage's output/pull-request.md>`). The agent never merges.

## Outputs
- pull-request.md → output/ — the title and body; the checks run with results; the guide pages and pictures changed; what a portal must follow with.

## Human check
A person reads `pull-request.md`, the guide diff and the evidence, and says whether to open it; the reviewer, not the author, merges.
