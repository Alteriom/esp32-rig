# 01_spec — what changes, for whom

One job: a short spec a person can approve: the behaviour, who sees it, where it lives, and the test that will prove it.

## Inputs
- Working (this run): the request — an issue (`gh issue view <n> --comments`) or the person's words.
- Reference (every run): `.github/icm/_shared/architecture.md`
- Reference (every run): `.github/icm/_shared/rules.md`
- Reference (only when the change is under `rig/`): `rig/AGENTS.md`

Do NOT load: the other pipelines, other runs' output, `.github/icm/_shared/release.md`.

## Process
1. Restate the request as behaviour: who (a rig owner, an account, a key of which role, a portal), what they do, what they see. A rig runs standalone or as a node of a portal; say which modes the change touches.
2. Place it with [architecture.md](../../../_shared/architecture.md): core or rig half (`tests/test_package_boundaries.py` decides), the files, the routes or pages. A dashboard change names the shell members it needs (`rig/AGENTS.md` "The shell seam").
3. Check it against [rules.md](../../../_shared/rules.md): scope and 404, totals over the same scope, nothing unbounded or untried on a timer, secrets, families the user chose; a board family is a description, not code.
4. Name the test that will fail before the change, as a sentence, and the guide pages or screenshots the change makes wrong.
5. Say what a portal must do when it takes the release (a shell member, a route it adds, a document id), so its mirror can follow.

## Outputs
- spec.md → output/ — the behaviour and who sees it; the half and files; the rules it meets; the failing test's name; the guide pages it changes; what a portal must follow with.

## Human check
A person approves the spec, or corrects it, before any code is written.
