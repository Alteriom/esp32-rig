# AGENTS.md — esp32-rig

The software for an ESP32 hardware-in-the-loop **rig**: a Raspberry Pi with boards on USB that flashes a project's firmware bundle, runs its pytest suite against the boards and keeps the evidence, standalone (its own dashboard and projects) or as a node of a farm portal. Public, Apache-2.0, released by tag; the guide at https://alteriom.github.io/esp32-rig is built from `docs/`. Humans: `README.md` and `CONTRIBUTING.md`. For Claude Code, Copilot, Codex and Cursor alike (`CLAUDE.md` and `.github/copilot-instructions.md` point here). Keep this file short and true: a line the code contradicts is worse than none, so update it in the pull request that makes it wrong.

## Where things live

| Path | What it holds |
|---|---|
| `core/`, `rig/` | `alteriom_hil`, one import name over two distributions: `core/` (`alteriom-hil-core`, what a portal holds with no hardware) and `rig/` (`alteriom-hil`: the rig half, host scripts, the dashboard — read [`rig/AGENTS.md`](rig/AGENTS.md) first) |
| `profiles/`, `suites/` | the built-in projects' profiles; the health check (`canary`) and painlessMesh suites |
| `canary/firmware.json` | the release of `Alteriom/esp32-hil-firmware` every rig release carries |
| `devices/`, `instruments/`, `hardware/` | example board and instrument descriptions; printable mounts and renders |
| `runner/` | the release build (`runner/ci/`) and the guide's screenshot recipe (`runner/screenshots/`) |
| `tests/`, `docs/` | the suite CI runs; the guide, published by `docs.yml` |
| `.github/workflows/` | `ci.yml` (tests, simulated suites, the release build), `release.yml` (`v*` tags), `docs.yml`, `icm-check.yml` |
| [`.github/icm/_shared/`](.github/icm/_shared/CONTEXT.md) | what every job below draws on, one file per question |
| [`.github/icm/01-fix/`](.github/icm/01-fix/CLAUDE.md) | fix a defect |
| [`.github/icm/02-feature/`](.github/icm/02-feature/CLAUDE.md) | change what a rig does: a route, a page, a setting, a script, a board family |
| [`.github/icm/03-release/`](.github/icm/03-release/CLAUDE.md) | cut a rig release (a person tags it) and bring its consumers along |

## Route by what just happened

| Route | If | Go to | Then stop at |
|---|---|---|---|
| triage | an issue or report arrives and its kind is unclear | read it whole, then take the row below that fits | the row chosen, said to the person |
| fix | a defect is reported (an issue, a failing test, a run's evidence, a rig giving the wrong answer) | [01_reproduce](.github/icm/01-fix/stages/01_reproduce/CONTEXT.md) | a person has seen the reproduction fail |
| ci-red | `CI`, `Docs` or `Release` is red on `main` or on a tag | [01_reproduce](.github/icm/01-fix/stages/01_reproduce/CONTEXT.md), with the run log as the report | a person has seen the reproduction fail |
| feature | a new route, page, setting, script, suite fixture or board family is asked for | [01_spec](.github/icm/02-feature/stages/01_spec/CONTEXT.md) | a person approves the spec |
| dashboard | the change is under `rig/` (the dashboard, the rig half, the host scripts) | [rig/AGENTS.md](rig/AGENTS.md), then the row above that fits | its rules read before the change is designed |
| guide | a page of the guide is wrong or missing and no code has to change | [documents.md](.github/icm/_shared/documents.md) "The guide" | a person reads the edit and its `mkdocs build --strict` |
| firmware | the health check firmware is to move to a newer release | [release.md](.github/icm/_shared/release.md) "Moving the firmware forward" | a person reviews the one-line pin pull request |
| release | a rig release is to be cut, or one was tagged and its consumers must follow | [01_plan](.github/icm/03-release/stages/01_plan/CONTEXT.md) | a person approves the tag number |
| next | a stage's output was approved | the next numbered stage of the same job | a person reads that stage's output |
| status | asked for status | scan `.github/icm/*/stages/*/output/` | report what exists |

## Agents connecting remotely

This repository becomes the ICM workspace `esp32-rig` on MemoryRelay, created by a person from this repository's `main`; the Alteriom hub `portfolio` then delegates to it. Until the workspace exists, an agent on a clone reads these files directly: this file is the entry and the table above is the route. Once it exists, an agent that is not on a clone reaches the same files over MCP at `https://api.memoryrelay.net/mcp` with a key in `MEMORYRELAY_API_KEY`; none of it holds a secret.

- Start with `icm_context_build` for the route that fits (`{kind: "route", id: "fix"}`), not with the whole repository.
- Report what you read with `icm_report_reads` when a stage ends, and what the task came to with `icm_report_outcome`.
- Never write through MCP: a change is a branch and a pull request here, read and merged by a person.

## Rules

- Nothing moves to the next stage until a person has read the output of the last one.
- A stage writes to its own `output/`, which is not committed: one run per worktree or branch; clear that job's `stages/*/output/` when its pull request merges or is abandoned.
- `.github/icm/` points at the files that own each fact. When they disagree, the owning file is right: correct `.github/icm/` in the same pull request and bump its `verified_commit`.
- No stage pushes a tag or publishes a release: a person does ([release.md](.github/icm/_shared/release.md)).
- Public repository: no host name, address, person or private repository of ours, in code or in these files ([rules.md](.github/icm/_shared/rules.md)).
