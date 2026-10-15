---
verified_at: 2026-10-15
verified_commit: 1b1a24a0e0767ef4b1b505a83d3713e27672e214
---

# Rules that are easy to break, and the conventions

Each rule names the file or test that holds it. The dashboard's own rules (the shell seam, one way in, escaping, the CSP, guarded renders, accounts versus keys) are `rig/AGENTS.md` "Rules"; read them for any change under `rig/web/`.

## A change

- **Tests first.** A change needs a test that fails without it; test names are sentences stating the behaviour; comments say *why*, with what was observed and when (`CONTRIBUTING.md` "What a change needs").
- **Boundaries are tests.** `tests/test_package_boundaries.py`: core never imports rig, neither half imports a portal. `tests/test_public_scrub.py`: no host, person, portal or private-repository name of ours, and no routable address, ships in `core/`, `rig/` or `canary/`; both are ratchets whose known counts may shrink and never grow. If one fails it is usually right; if it is wrong, change its list and say why in the same commit (`CONTRIBUTING.md` "The boundaries, and why they are tests").
- **A board family is a description** under `core/alteriom_hil/devices/families/`; if it needs code, the description or the abstraction is wrong (`CONTRIBUTING.md` "Adding a board family").
- **Host scripts** run under bash on Raspberry Pi OS: idempotent, `set -euo pipefail`, never a token on a command line (`rig/AGENTS.md` "The rig half").

## Access, data and secrets

- **Scope reads to the caller, and answer 404 — never 403 — outside it**, so ids and rig names cannot be probed; every total beside a scoped list is counted over the same scope. The gate is `allowed` in `core/alteriom_hil/api_keys.py`; the scoped reads are in `core/alteriom_hil/service.py`, held by `tests/test_api_keys.py` and `tests/test_farm_service.py`.
- **Nothing unbounded, nothing unproven on a timer.** Backups stream and nothing holds an archive in memory (`core/alteriom_hil/backup.py`); anything scheduled inside the service is tried once against production-sized data first (a portal pod has 1 GiB).
- **Secrets never reach logs, URLs, backups or the page** (`SECURITY.md` "What this software assumes": provider credentials sealed to the rig's key, the job log scrubbed as it is written).
- **A run is scoped to the families the user chose**; never assume a family is connected (`core/alteriom_hil/allocation.py`, `tests/test_allocation.py`).

## Firmware a rig runs

- **ESP32 core 3.x callbacks run under the event lock** (esp32-c5, esp32-c6): never reconfigure the radio inside one — the node hangs silently. This file is the rule's home; it applies to the reference suite's firmware (`suites/painlessmesh/firmware/`) and to any firmware written for a rig.

## Conventions

- One change per pull request, squash-merged, the subject in plain words: what a rig owner would notice (the subjects on `main` show the shape).
- A change to `core/` or `rig/` is not done until it is released and the farm portal's mirror pull request has landed ([release.md](release.md) "Who follows a release").
- A visible `rig/web/` change retakes the guide's screenshots (`runner/screenshots/README.md`).
- Public repository: what ships, and these files, name no host, address, person or private repository of ours (`CONTRIBUTING.md` "What a change needs", "Nothing about your rig in particular").
