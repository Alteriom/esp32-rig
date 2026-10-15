# 03_follow — bring the consumers along

One job: for each consumer of the release, what it must do and the pull request or step that does it, prepared for a person.

## Inputs
- Working (this run): `.github/icm/03-release/stages/02_publish/output/publish.md`
- Reference (every run): `.github/icm/_shared/release.md`
- Reference (only when the release changed what a page of the guide installs or shows): `.github/icm/_shared/documents.md`

Do NOT load: the other pipelines, other runs' output.

## Process
1. The farm portal (a private repository): its mirror of `core/` and `rig/` and its pin follow this tag in a pull request there, opened by a person or by an agent working in that repository; name what `plan.md` says it must follow with. Nothing here edits it.
2. Rigs: say whether this release needs anything of a rig owner beyond Settings, Rig, Software (an install script re-run, a configuration key, a firmware move), from the changes in `plan.md`.
3. `Alteriom/esp32-rig-example`: say whether its pinned `RIG_RELEASE` should move to this release, and why; moving it is a pull request there.
4. The guide: if this release changes what `docs/getting-started.md` or `docs/bringup.md` installs, prepare the guide pull request here ([documents.md](../../../_shared/documents.md) "Known drift").

## Outputs
- follow.md → output/ — per consumer: what it must do, the pull request or step that does it, and who opens it.

## Human check
A person reads `follow.md` and opens (or asks for) each consumer's pull request; a change to `core/` or `rig/` is done when the farm portal's mirror has landed.
