# AGENTS.md — esp32-rig

Instructions for AI coding agents (Claude Code, Copilot, Codex, Cursor). Humans: README.md and
CONTRIBUTING.md. Keep this file short and true — a fact here that the code contradicts is worse
than none. Update it in the same PR as the change that makes it wrong.

## What this repo is

The software for an ESP32 hardware-in-the-loop **rig**: a Raspberry Pi with boards on USB that
flashes a project's firmware bundle, runs its pytest suite against the boards and keeps the
evidence. It runs standalone (its own dashboard and projects) or as a node of a farm portal.
Public, Apache-2.0. Guide: https://alteriom.github.io/esp32-rig (built from `docs/`).

One of four repositories that must stay consistent:

| Repo | Role |
|---|---|
| `Alteriom/esp32-rig` (this) | Source of truth for `core/` and `rig/`; each release is consumed by the portal |
| `Alteriom/esp32-hil-firmware` | Rig Health Check firmware, pinned here in `canary/firmware.json` |
| `Alteriom/esp32-rig-example` | The built-in "Rig example" project (`profiles/rig-example.yaml`) |
| The Alteriom farm portal (a private repository) | Pins a release of this repo and mirrors `core/` + `rig/` byte for byte |

## Layout — where to look

`alteriom_hil` is one import name split over two distributions with no `__init__.py`:
`core/` (`alteriom-hil-core`, what a portal holds with no hardware) and `rig/` (`alteriom-hil`).

| Question | Where |
|---|---|
| HTTP API, auth gate, runs, artifact store and library, retention, backups, statistics | `core/alteriom_hil/service.py` (`BaseManager`, `make_handler`) |
| Keys, roles, which routes an account may use | `core/alteriom_hil/api_keys.py` (`allowed`, `ACCOUNT_ROUTES`) |
| Job store (SQLite) | `core/alteriom_hil/jobstore.py` |
| Backup and restore (streaming, checksummed, allow-listed) | `core/alteriom_hil/backup.py` |
| Board families — data, not code | `core/alteriom_hil/devices/families/` |
| Profiles and project documents (`.alteriom-hil.yaml`) | `core/alteriom_hil/profiles.py`, `projects.py`, `profiles/*.yaml` |
| The rig half: pipeline, health check, quarantine, GitHub fetch, metering | `rig/alteriom_hil/rig_manager.py` |
| Node agent, admin CLI, pytest plugin and simulator | `rig/alteriom_hil/farm_node.py`, `admin_cli.py`, `pytest_plugin.py`, `sim.py` |
| Dashboard (plain JS, no build step) | `rig/web/` — read `rig/AGENTS.md` first |
| Host scripts, systemd, udev, nginx | `rig/*.sh`, `rig/udev/`, `rig/nginx/` |
| Suites | `suites/canary` (health check), `suites/painlessmesh` (reference) |
| Release build | `runner/ci/build-release.sh`, `.github/workflows/release.yml` |

## Commands

```bash
python -m pip install -e ./core[dev] -e './rig[dev]'
python -m pytest tests -q                                        # Linux is authoritative
ALTERIOM_HIL_MODE=sim python -m pytest suites/canary/tests suites/painlessmesh/tests -q
runner/ci/build-release.sh --version-only                        # the version the next tag must be
mkdocs build --strict                                            # the guide; every relative link must resolve
```

On Windows or macOS dozens of tests fail or skip for platform reasons; a green run there proves
little. Run on Linux, e.g. a throwaway container:

```bash
docker run --rm -v "$PWD:/src:ro" python:3.12 bash -lc 'cp -r /src /work && cd /work && rm -rf .git && git init -q && git add -A && git -c user.email=t@t -c user.name=t commit -qm t && pip install -q -e ./core[dev] -e ./rig[dev] && python -m pytest tests -q'
```

## Releasing (by hand, on purpose)

1. Merge to `main` (squash) with CI green on Python 3.9 and 3.12.
2. `git tag v1.0.$(git rev-list --count origin/main) origin/main && git push origin <tag>` —
   `release.yml` runs on `v*` tags only and refuses a tag that disagrees with the tree.
3. The farm portal pins the release (its `release.json`) and copies `core/` and `rig/` from the tag;
   a test there compares them file by file, so nothing is changed there first.
4. Rigs update from Settings → Rig → Software, or `POST /api/v1/update/check` then `/update/install`.

## Rules that are easy to break

- **Tests first** (CONTRIBUTING.md): a change needs a test that fails without it. Test names are
  sentences stating the behaviour. Comments say *why*, with what was observed and when.
- **Boundaries** are tests: `tests/test_package_boundaries.py` (core never imports rig; neither
  half imports a portal) and `tests/test_public_scrub.py` (no internal host, person, portal or
  private-repo name ships).
- **Scope reads to the caller, and answer 404 — never 403 — outside it**, so ids and rig names
  cannot be probed. Every total beside a scoped list is counted over the same scope.
- **Nothing unbounded, nothing unproven on a timer.** Backups stream; anything scheduled inside the
  service is tried once against production-sized data first (a portal pod has 1 GiB).
- **Secrets never reach logs, URLs, backups or the page.**
- **A run is scoped to the families the user chose**; never assume a family is connected.
- **ESP32 core 3.x callbacks run under the event lock** (esp32-c5/c6): never reconfigure the radio
  inside one — the node hangs silently.

## Conventions

One change per PR, squash-merged, subject in plain words (what a rig owner would notice). A change
to `core/` or `rig/` is not done until it is released and the farm's mirror PR has landed.
