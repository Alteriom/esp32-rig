# 01_plan — which commit, which number

One job: a release plan that names the commit, the tag the tree will build, and what the release carries and changes.

## Inputs
- Working (this run): the changes since the last release — `gh release list --repo Alteriom/esp32-rig --limit 3`, then `git log --oneline <last tag>..origin/main`.
- Reference (every run): `.github/icm/_shared/release.md`

Do NOT load: the other pipelines, other runs' output, `.github/icm/_shared/architecture.md`.

## Process
1. List the merged changes since the last tag and what each means for a consumer: a behaviour a rig owner will notice, a dashboard change a portal's shell must match, a protocol or schema change a node and a portal must agree on, a firmware pin move (`canary/firmware.json`).
2. Compute the number the tree builds: `runner/ci/build-release.sh --version-only` on an up-to-date `origin/main` (MAJOR.MINOR from `VERSION`, PATCH the commit count). Confirm the tag `v<that>` does not exist (`git ls-remote --tags origin`). A wrong number costs a new commit, never a retag ([release.md](../../../_shared/release.md) "The tag is the version").
3. Confirm `CI` is green on that commit, both Python versions and the `release` job; record the run link.
4. Write the exact commands the person will run: `git tag v1.0.N <commit> && git push origin v1.0.N`.

## Outputs
- plan.md → output/ — the commit and the tag `v1.0.N`; each merged change and what a consumer must do about it; the green CI run; the commands for the person.

## Human check
A person approves the tag number and the commit; nothing is tagged until they do.
