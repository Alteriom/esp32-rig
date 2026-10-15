# 02_publish — a person tags, the release is checked

One job: after a person pushes the tag, check that `Release` built, installed and attached exactly what a release carries.

## Inputs
- Working (this run): `.github/icm/03-release/stages/01_plan/output/plan.md`
- Reference (every run): `.github/icm/_shared/release.md`

Do NOT load: the other pipelines, other runs' output.

## Process
1. The person pushes the tag with the commands in `plan.md`. The agent never runs `git tag`, `git push` of a tag, `gh release create` or a dispatch of `release.yml`.
2. Watch the tag's `Release` run (`gh run list --workflow release.yml --limit 3`, then `gh run watch <id>`). A red "The tag is the version" step means the number was wrong: say so; the fix is a new commit and a new tag, decided by the person.
3. Check the release (`gh release view v1.0.N --repo Alteriom/esp32-rig`): the two wheels, the dashboard bundle, the `alteriom-hil-canary-*.tar.gz` of the pinned firmware, `SHA256SUMS`, `release.json`; the install notes name this version.
4. Record what was attached, with sizes and the run link.

## Outputs
- publish.md → output/ — the tag, the release run and its result, each asset attached, anything missing or wrong.

## Human check
A person pushes the tag, then reads `publish.md` and confirms the release page shows every asset before anyone is told to take it.
