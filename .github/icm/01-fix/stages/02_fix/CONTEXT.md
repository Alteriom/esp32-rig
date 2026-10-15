# 02_fix — change the cause, not the symptom

One job: the smallest change at the defect's cause that turns the reproduction green without weakening it or loosening a rule.

## Inputs
- Working (this run): `.github/icm/01-fix/stages/01_reproduce/output/reproduction.md`
- Reference (every run): `.github/icm/_shared/rules.md`
- Reference (only when the change is under `rig/`): `rig/AGENTS.md`
- Reference (only when the change moves or renames a file, a workflow or a pinned string): `.github/icm/_shared/build-and-test.md`

Do NOT load: the other pipelines, other runs' output, `.github/icm/_shared/release.md`.

## Process
1. Read the reproduction and confirm its cause in the code before changing anything; if it moved, record where it really is.
2. Change the cause. Do not edit the reproduction to make it pass; if it was wrong, go back to stage 01.
3. Check the change against [rules.md](../../../_shared/rules.md): out-of-scope reads answer 404, totals counted over the same scope, nothing unbounded, no secret in a log, URL or page, no family assumed; under `rig/web/`, the rules of `rig/AGENTS.md`.
4. Comment the *why* with what was observed and when (`CONTRIBUTING.md` "What a change needs"). Nothing that ships names a host, address, person or private repository of ours: `tests/test_public_scrub.py` fails the build.
5. After moving or renaming anything, grep the whole repository for the old name and change every test that pins it by string in the same commit.
6. Run the reproduction (green now) and the suite on Linux ([build-and-test.md](../../../_shared/build-and-test.md)).

## Outputs
- change.md → output/ — the cause in one paragraph; each file changed and why; the reproduction's passing output; the suite result and where it ran; anything the fix deliberately leaves alone.

## Human check
A person reads the diff beside the reproduction: it removes the cause, not only the symptom the test sees, and no scope, gate or rule was loosened. Edit `change.md` if the explanation is wrong; stage 03 reads it.
