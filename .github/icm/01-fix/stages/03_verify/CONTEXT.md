# 03_verify — checks, records, pull request

One job: the pull request a reviewer can merge, with the evidence CI and a rig will look for and what the merge leads to.

## Inputs
- Working (this run): `.github/icm/01-fix/stages/02_fix/output/change.md`
- Reference (every run): `.github/icm/_shared/build-and-test.md`
- Reference (only when the change touches what a release carries or the firmware pin): `.github/icm/_shared/release.md`
- Reference (only when the fix changes what a page of the guide says): `.github/icm/_shared/documents.md`

Do NOT load: the other pipelines, other runs' output.

## Process
1. Run what CI runs that the change can affect ([build-and-test.md](../../../_shared/build-and-test.md)): the suite on Linux; the simulated suites if a suite, profile or the pipeline changed; `runner/ci/build-release.sh --out dist` if packaging changed; `mkdocs build --strict` if `docs/` changed.
2. Decide the hardware evidence: a change to the health check, flashing, serial capture or allocation needs a run on a rig or says why none is needed.
3. Record: correct a page of the guide, `README.md` or `rig/AGENTS.md` if the fix made a line there wrong; retake screenshots for a visible dashboard change (`runner/screenshots/README.md`); correct `.github/icm/_shared/` if a pointer moved.
4. Write the pull request: what was wrong, how you know (the reproduction), what the change does; the checks and results; the hardware evidence or its absence; and that a rig gets it with the next release. Subject in plain words, what a rig owner would notice, with the issue number.
5. After the human check, open it against `main` (`gh pr create --base main --body-file <this stage's output/pull-request.md>`). The agent never merges.

## Outputs
- pull-request.md → output/ — the title and body; the checks run with results; the records updated; the hardware evidence or why none.

## Human check
A person reads `pull-request.md` and its evidence and says whether to open it; the reviewer, not the author, merges.
