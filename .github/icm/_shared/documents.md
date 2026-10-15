---
verified_at: 2026-10-15
verified_commit: 1b1a24a0e0767ef4b1b505a83d3713e27672e214
---

# Documents — what a person reads here, and who owns its facts

The agent-facing files are `AGENTS.md` (the entry), `rig/AGENTS.md` and this folder. Everything below is a document a person reads: each restates facts that code, a workflow or a test owns, and the owner is right when they disagree. Each linked document is pinned in the workspace, so an agent can be given one by path.

## The guide

`docs/`, ordered by the `nav` of `mkdocs.yml` and published at https://alteriom.github.io/esp32-rig by `.github/workflows/docs.yml` on a merge to `main`; a pull request builds it with `--strict` and publishes nothing. To correct a page: edit it, run `mkdocs build --strict` (needs `mkdocs-material`), and open a pull request saying which owning file proves the new text. Pictures under `docs/images/` are retaken by the recipe in `runner/screenshots/README.md`, never edited by hand.

| Document | What it answers | Owner of the facts |
|---|---|---|
| [docs/index.md](../../../docs/index.md) | what a rig is, where things are | `README.md` |
| [docs/getting-started.md](../../../docs/getting-started.md) | from a host to a first run, in six steps | `rig/setup-runner.sh`, `rig/install-health-service.sh`, `rig/verify-rig.sh` |
| [docs/hardware.md](../../../docs/hardware.md) | what to buy, the rules the software assumes | `core/alteriom_hil/devices/families/` |
| [docs/bringup.md](../../../docs/bringup.md) | bring-up in order: hardening, the rig's network, the proxy, the first health check | `rig/harden-pi.sh`, `rig/setup-gateway-network.sh`, `rig/install-dashboard-proxy.sh` |
| [docs/dashboard.md](../../../docs/dashboard.md) | every page and setting, keys and roles | `rig/web/app.js`, `core/alteriom_hil/api_keys.py` |
| [docs/tour.md](../../../docs/tour.md) | the dashboard in pictures | `rig/web/app.js`, `runner/screenshots/capture.py` |
| [docs/projects.md](../../../docs/projects.md) | a project, the bundle a CI builds, its workflow and suite | `core/alteriom_hil/projects.py`, `core/alteriom_hil/artifacts.py`, `profiles/rig-example.yaml` |
| [docs/health-check.md](../../../docs/health-check.md) | the health check firmware: where it lives, what it checks, the protocol, its version | `canary/firmware.json`, `suites/canary/`, `rig/alteriom_hil/health_check.py` |
| [docs/farm.md](../../../docs/farm.md) | connecting a rig to a farm, what the farm sees | `rig/alteriom_hil/farm_node.py`, `rig/alteriom_hil/admin_cli.py` |
| [docs/api.md](../../../docs/api.md) | the HTTP API, the command line, the MCP server | `core/alteriom_hil/service.py`, `rig/alteriom_hil/admin_cli.py` |
| [docs/troubleshooting.md](../../../docs/troubleshooting.md) | symptom to cause | `rig/alteriom_hil/health_check.py`, `rig/verify-rig.sh` |
| [docs/mcp.md](../../../docs/mcp.md) | the read-only MCP server | `core/alteriom_hil/mcp_server.py` |

## The rest

| Document | What it answers | Owner of the facts |
|---|---|---|
| [README.md](../../../README.md) | what it is, installing it as packages, writing a suite, a project on the rig | `.github/workflows/release.yml`, `core/alteriom_hil/projects.py` |
| [CONTRIBUTING.md](../../../CONTRIBUTING.md) | what a change needs, running the tests, the boundaries, board families, hardware reports | `tests/test_package_boundaries.py`, `tests/test_public_scrub.py` |
| [SECURITY.md](../../../SECURITY.md) | reporting a vulnerability, what is trusted, what is out of scope | `core/alteriom_hil/api_keys.py` |
| [runner/screenshots/README.md](../../../runner/screenshots/README.md) | how the guide's pictures are made | `runner/screenshots/capture.py`, `runner/screenshots/demo_rig.py` |

## Known drift

- **Release numbers in the guide** are those of the release each page was written for: `docs/getting-started.md` installs 1.0.179 and `docs/bringup.md` 1.0.169, while the latest release on 2026-10-15 is 1.0.197. The commands work for the release they name; move them in a guide pull request when a newer release changes what they install.
- **A plan the comments cite is not here.** `.github/workflows/release.yml`, `runner/ci/build-release.sh` and `tests/test_public_scrub.py` cite steps of a "public release plan" document that stayed with the farm portal's private repository when this one was lifted. Their reasoning stands on its own; the citation cannot be followed from here.
