# 01_reproduce — reproduce the reported defect

One job: turn the report into something that fails on demand, for the reason reported.

## Inputs
- Working (this run): the report — read whole with what it links: an issue (`gh issue view <n> --comments`), a red run (`gh run view <run-id> --log-failed`), a run's evidence directory and serial capture, a rig giving the wrong answer.
- Reference (every run): `.github/icm/_shared/architecture.md`
- Reference (every run): `.github/icm/_shared/build-and-test.md`
- Reference (only when the defect is under `rig/`): `rig/AGENTS.md`

Do NOT load: the other pipelines, other runs' output, `.github/icm/_shared/release.md`, the guide beyond the page the report names.

## Process
1. Restate the report as observed versus expected, with where it was seen: a rig (standalone or as a node), the dashboard (as which account or key), a workflow run, a test.
2. Find the code path with [architecture.md](../../../_shared/architecture.md) and name the suspected cause as `path:line`.
3. Separate hardware from software: a red health check on one board, a board that never appears, or a token GitHub refuses has a page in `docs/troubleshooting.md`; read it before calling it a defect.
4. Write the failing test (a sentence-named test in `tests/`), or for a suite or pipeline behaviour the simulated run (`ALTERIOM_HIL_MODE=sim`). It must call this repository's code, never a copy of it.
5. Run it and keep the failing output. A Windows-only failure is not a reproduction ([build-and-test.md](../../../_shared/build-and-test.md) "Linux is authoritative"); if it passes on Linux, the reproduction or the report is wrong: say which, and stop.

## Outputs
- reproduction.md → output/ — the restated report; suspected cause (`path:line`); the test or steps and the command; the failing output and where it ran; what it does not cover (hardware, a portal).

## Human check
A person runs the command, or reads the evidence, and confirms it fails for the reported reason and not a neighbouring one.
