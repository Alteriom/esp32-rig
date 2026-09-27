# AGENTS.md — rig/ (the rig half and its dashboard)

Loaded when you work under `rig/`. The repo root AGENTS.md still applies. (This file lives here and
not in `rig/web/` because `rig/web/` is packed whole into the dashboard bundle every rig serves.)

## The rig half

- `rig/alteriom_hil/rig_manager.py` is the rig's half of the manager (`RigMixin`): the pipeline,
  health check and quarantine, the GitHub fetch of a project's bundle, metering (`_run_metrics`).
- Host scripts (`setup-runner.sh`, `install-health-service.sh`, `update-runner.sh`, `join-rig.sh`,
  `node-update.sh`, `verify-rig.sh`) run under bash on Raspberry Pi OS; keep them idempotent and
  `set -euo pipefail`, and never put a token on a command line.
- A public checkout has no `portal/`; a script that installs one checks it exists first.

## The dashboard (`rig/web/`)

One dashboard, plain JavaScript with no build step, served by a rig (`rig/web/index.html`) and by
the portal (the portal image copies its own `portal/web/` over this directory, adding
`portal-shell.js`, `portal.css` and its own `index.html`). `app.js` must work with and without the
portal's shell.

| File | Role |
|---|---|
| `app.js` | Everything shared: router, pages, runs, rigs, boards, library, settings, statistics |
| `index.html` | A rig's document (the portal has its own, in the farm repo) |
| `chips.js` | `escapeHtml` and the capability chips; also loaded by the public site |
| `app.css`, `pipeline.css`, `brand/` | Styles and brand assets (portal-only styles live in the portal) |

## Rules

- **The shell seam.** `shell()` returns `RIG_SHELL` (in `app.js`) or the portal's `PORTAL_SHELL`.
  Both must declare the **same members**; a portal difference is a shell member, never
  `if (isPortal())` scattered through `app.js`.
- **One way in.** Every navigation — menu, link, tab, reload, Back — goes through
  `navigateTo(href)` → `openRoute(route)`. Showing a page with `showPanel` alone skips its loader
  and leaves it on "Loading…".
- **Escape everything you interpolate** with `escapeHtml` — text and attribute values alike (it
  escapes quotes). Values a rig reports reach an admin's page on the portal.
- **CSP is `default-src 'self'; script-src 'self'; style-src 'self'`**: no inline scripts, no
  inline `style=`; set widths and the like from script.
- **Guard async renders**: a slower answer for something the reader has left must not overwrite
  the page (see `showJob`, `showRig`).
- **Accounts vs keys.** `workspaceOnly()` is true for every signed-in account, an admin's included;
  farm-wide elements carry `farm-wide`, admin pages `admin-only`. A pasted key sees the whole farm.
- **Every `$("id")` the script reaches must exist in the rig's document** unless guarded; ids that
  only the portal's document has are listed in `PORTAL_ONLY_IDS` in `tests/test_web_dashboard.py`.

## Tests

`tests/test_web_dashboard.py` pins much of this file by string; when you change a pinned line,
change its test in the same commit and keep the behaviour the test describes.
