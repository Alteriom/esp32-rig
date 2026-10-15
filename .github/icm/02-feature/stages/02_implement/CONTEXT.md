# 02_implement — the failing test, then the change

One job: the test from the spec failing on the tree as it is, then the smallest change that makes it pass without breaking a rule.

## Inputs
- Working (this run): `.github/icm/02-feature/stages/01_spec/output/spec.md`
- Reference (every run): `.github/icm/_shared/rules.md`
- Reference (every run): `.github/icm/_shared/build-and-test.md`
- Reference (only when the change is under `rig/`): `rig/AGENTS.md`

Do NOT load: the other pipelines, other runs' output, `.github/icm/_shared/release.md`.

## Process
1. Write the spec's test and run it: it must fail for the reason the spec gives. Keep the output.
2. Make the change. Comments say *why*, with what was observed and when; nothing that ships names a host, address, person or private repository of ours.
3. Under `rig/web/`: both shells declare the same members, every navigation goes through `navigateTo`, everything interpolated is escaped, no inline script or style, async renders guarded, every `$("id")` exists in the rig's document or is listed in `PORTAL_ONLY_IDS` (`rig/AGENTS.md` "Rules").
4. Change every test that pins an edited string in the same commit ([build-and-test.md](../../../_shared/build-and-test.md) "Tests that pin files").
5. Run the test (green now), the suite on Linux, and the simulated suites if a suite, profile or the pipeline changed.

## Outputs
- change.md → output/ — the failing then passing output; each file changed and why; the suite results and where they ran; what a portal must follow with.

## Human check
A person reads the diff beside the spec: it does what the spec says and no more, the test failed first, and no rule was loosened.
