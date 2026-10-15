---
verified_at: 2026-10-15
verified_commit: 1b1a24a0e0767ef4b1b505a83d3713e27672e214
---

# Build, test, and what counts as evidence

What CI runs is `.github/workflows/ci.yml`; read its steps rather than copying them anywhere. The contributor's version is `CONTRIBUTING.md` "Running the tests".

## Install and run, from the repository root

```bash
python -m pip install -e ./core[dev] -e './rig[dev]'
python -m pytest tests -q                                        # Linux is authoritative
ALTERIOM_HIL_MODE=sim python -m pytest suites/canary/tests suites/painlessmesh/tests -q
runner/ci/build-release.sh --version-only                        # the version the next tag must be
mkdocs build --strict                                            # the guide (pip install mkdocs-material); every relative link must resolve
```

The extras are in `core/pyproject.toml` and `rig/pyproject.toml`. CI's `Install the rig` step installs `-e ./core -e './rig[hardware]'` with pytest, pyyaml, jsonschema and build into a venv first on `PATH` (the service finds `profiles/` by walking up from its own file), and runs each simulated suite with `ALTERIOM_HIL_SIM_BOARDS: "3"`.

## Linux is authoritative

On Windows or macOS dozens of tests fail or skip for platform reasons; a green run there proves little (`CONTRIBUTING.md` "Running the tests"). The throwaway container below runs the suite on Linux (this file is the recipe's home):

```bash
docker run --rm -v "$PWD:/src:ro" python:3.12 bash -lc 'cp -r /src /work && cd /work && rm -rf .git && git init -q && git add -A && git -c user.email=t@t -c user.name=t commit -qm t && pip install -q -e ./core[dev] -e ./rig[dev] && python -m pytest tests -q'
```

The copy and the fresh `git init` are there because the release build counts commits with git (`runner/ci/build-release.sh`, read by `tests/test_release_build.py`), and a worktree's `.git` is a file naming a path the container cannot see.

## What CI runs, and when

| Workflow | Runs | On |
|---|---|---|
| `ci.yml` `tests` | the suite on Python 3.9 and 3.12, then both suites against the simulator | every pull request and push to `main` |
| `ci.yml` `release` | `runner/ci/build-release.sh --out dist` without firmware: a release can still be built | every pull request and push to `main` |
| `docs.yml` | `mkdocs build --strict`; on `main` it also publishes the guide | changes to `docs/**`, `mkdocs.yml` |
| `release.yml` | the release ([release.md](release.md)) | `v*` tags; a dispatch against a ref builds and attaches nothing |
| `icm-check.yml` | the workspace files compile to the ICM standard | changes to the entry files or `.github/icm/`; weekly with `--strict` |

## Tests that pin files

- `tests/test_public_scrub.py` and `tests/test_package_boundaries.py` are ratchets: what may ship in `core/`, `rig/` and `canary/`, and what may import what ([rules.md](rules.md)).
- `tests/test_release_build.py` and `tests/test_canary.py` read `.github/workflows/release.yml` and `canary/firmware.json`; `tests/test_wiring_check.py` reads the pin too. A workflow or pin edit is a test change.
- `tests/test_web_dashboard.py` pins strings of `rig/web/app.js` and ids of the rig's document; ids only a portal's document has are listed in `PORTAL_ONLY_IDS` there (`rig/AGENTS.md` "Tests").

## Evidence

- A test that fails on the tree before the change and passes after; if one cannot be written, the pull request says so and what was done instead (`CONTRIBUTING.md` "What a change needs").
- The simulator (`ALTERIOM_HIL_MODE=sim`, `rig/alteriom_hil/sim.py`) proves that fixtures, protocol and pipeline agree, not hardware (the comment above the suites in `ci.yml`). Hardware evidence is a run on a rig with its evidence directory; the serial capture is usually the whole answer (`CONTRIBUTING.md` "Reporting something that breaks on hardware").
