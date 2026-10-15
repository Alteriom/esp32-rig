---
verified_at: 2026-10-15
verified_commit: 1b1a24a0e0767ef4b1b505a83d3713e27672e214
---

# Releasing — by hand, on purpose

## The tag is the version

The version is `VERSION`'s MAJOR.MINOR plus the commit count of the tree (`runner/ci/build-release.sh`; `--version-only` prints it). A person cuts a release; no agent pushes a tag:

1. Merge to `main` (squash) with `CI` green on Python 3.9 and 3.12.
2. `git tag v1.0.$(git rev-list --count origin/main) origin/main && git push origin <tag>`. `.github/workflows/release.yml` runs on `v*` tags only, and its step "The tag is the version" refuses a tag that disagrees with the tree: a wrong number costs a new commit, never a retag. The releases page (`gh release list --repo Alteriom/esp32-rig`) shows the last one.

## What a release carries

`.github/workflows/release.yml`, header and steps: the two wheels (`alteriom_hil_core`, `alteriom_hil`), the dashboard bundle, the pinned health check firmware for every board family, `SHA256SUMS` and the `release.json` that names them, attached to the GitHub release with install notes. The release is installed from the files alone in a clean venv before it is attached. The channel is the GitHub release only, not PyPI yet (`README.md` "Installing it as packages"). The `release` job of `.github/workflows/ci.yml` builds the same without firmware on every pull request.

## Moving the firmware forward

The Rig Health Check firmware is released by `Alteriom/esp32-hil-firmware`, never built here, and pinned in `canary/firmware.json`: that release's own `firmware.json`, copied verbatim. Moving it is a one-line change reviewed like any other. The step "Fetch the pinned firmware" of `release.yml` checks the tarball against the pin's digest, version, revision and families; `tests/test_canary.py`, `tests/test_wiring_check.py` and `tests/test_release_build.py` read the pin, so they run on the pull request. For the reader: `docs/health-check.md` "Where it lives" and "Its version".

## Who follows a release

- **The farm portal** (a private repository) pins the release and copies `core/` and `rig/` from the tag; a test there compares them file by file, so nothing is changed there first and a change is only done when that mirror lands.
- **Rigs** update from Settings, Rig, Software, or `POST /api/v1/update/check` then `POST /api/v1/update/install` (the routes in `rig/alteriom_hil/rig_manager.py`, the work in `rig/alteriom_hil/updates.py`); `alteriom-hil-admin upgrade` checks every file against `release.json` first (`README.md` "Installing it as packages").
- **`Alteriom/esp32-rig-example`** runs its suite against a rig release it pins (`RIG_RELEASE` in its suite workflow) and moves that pin deliberately, in a pull request of its own.
- **The guide** names release numbers in its install commands (`docs/getting-started.md`, `docs/bringup.md`); they move in a guide pull request when a release changes what they install ([documents.md](documents.md) "Known drift").
