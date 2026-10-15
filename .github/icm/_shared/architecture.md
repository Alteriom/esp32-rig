---
verified_at: 2026-10-15
verified_commit: 1b1a24a0e0767ef4b1b505a83d3713e27672e214
---

# Architecture — where to look

A map for finding the file that owns a fact, not a description of how a rig works. How it works is the guide (`docs/index.md` and the pages `mkdocs.yml` lists); what the package is, `README.md` "What it is".

## Four repositories, kept consistent

| Repository | Role |
|---|---|
| `Alteriom/esp32-rig` (this) | the source of truth for `core/` and `rig/`; each release is a tag here ([release.md](release.md)) |
| `Alteriom/esp32-hil-firmware` | the Rig Health Check firmware; a release of it is pinned in `canary/firmware.json` and fetched by `.github/workflows/release.yml` |
| `Alteriom/esp32-rig-example` | the built-in "Rig example" project (`profiles/rig-example.yaml`); its own suite runs against a rig release it pins |
| the farm portal (a private repository) | pins a release of this repository and mirrors `core/` and `rig/` byte for byte, so a change to them is made here first, never there |

## Two halves, one import name

`alteriom_hil` is one import name split over two distributions with no `__init__.py`: `core/pyproject.toml` (`alteriom-hil-core`, what a portal holds with no hardware) and `rig/pyproject.toml` (`alteriom-hil`). `tests/test_package_boundaries.py` holds the line: core never imports rig, and neither half imports a portal.

## Where to look

| Question | Where |
|---|---|
| HTTP API, auth gate, runs, artifact store and library, retention, backups, statistics | `core/alteriom_hil/service.py` (`BaseManager`, `make_handler`) |
| keys, roles, which routes an account may use | `core/alteriom_hil/api_keys.py` (`allowed`, `ACCOUNT_ROUTES`) |
| the job store (SQLite) | `core/alteriom_hil/jobstore.py` |
| backup and restore (streaming, checksummed, allow-listed) | `core/alteriom_hil/backup.py` |
| which boards a run gets | `core/alteriom_hil/allocation.py` |
| board families — data, not code | `core/alteriom_hil/devices/families/` |
| profiles and project documents (`.alteriom-hil.yaml`) | `core/alteriom_hil/profiles.py`, `core/alteriom_hil/projects.py`, `profiles/` |
| the bundle manifest a producer writes and a rig verifies | `core/alteriom_hil/artifacts.py` |
| the rig half: pipeline, health check, quarantine, GitHub fetch, metering | `rig/alteriom_hil/rig_manager.py` (`RigMixin`) |
| node agent, admin CLI, pytest plugin, simulator | `rig/alteriom_hil/farm_node.py`, `rig/alteriom_hil/admin_cli.py`, `rig/alteriom_hil/pytest_plugin.py`, `rig/alteriom_hil/sim.py` |
| a rig taking a newer release | `rig/alteriom_hil/updates.py` |
| the dashboard (plain JavaScript, no build step) | `rig/web/app.js` and its folder; its rules are `rig/AGENTS.md` |
| host scripts, systemd, udev, nginx | `rig/setup-runner.sh`, `rig/install-health-service.sh`, `rig/verify-rig.sh`, `rig/udev/`, `rig/nginx/` |
| suites | `suites/canary/` (the health check), `suites/painlessmesh/` (the reference) |
| the release build | `runner/ci/build-release.sh`, `.github/workflows/release.yml` |
| the guide's screenshots (retaken after a visible `rig/web/` change) | `runner/screenshots/README.md` (a demo rig and a Playwright capture), into `docs/images/` |
