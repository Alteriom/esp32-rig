# Alteriom HIL Rig

A hardware-in-the-loop rig for ESP32 firmware: a Raspberry Pi, a powered USB
hub and a handful of dev boards that flash the firmware your CI built, run
your test suite against real silicon, and keep the evidence. It works on its
own, on your bench, with its own dashboard; it can also connect to a farm and
take work from there.

<div class="grid cards" markdown>

- **[Getting started](getting-started.md)** — from a box of parts to the first
  green health check, in about an hour.
- **[Projects and your CI](projects.md)** — how the rig gets *your* firmware and
  runs *your* suite: the project, the bundle, the workflow, the example.
- **[The dashboard](dashboard.md)** — what every page shows and what it is for.
- **[API reference](api.md)** — every route the rig answers.

</div>

## What it is

- **A rig runs the projects you give it and no other.** A project is a GitHub
  repository: the rig checks it out for each run, flashes the bundle its CI
  built, runs its pytest suite over the boards, and keeps the run as
  evidence — logs, serial captures, JUnit, a report.
- **The rig does not build firmware.** Your CI does, the way it already does,
  and hands the bundle over — or the rig fetches it from GitHub, which is how
  a rig on a home LAN, reachable by no CI, still gets its firmware.
- **It proves its own hardware first.** The Rig Health Check flashes every
  board with a small firmware of its own and asks each one: do you boot, is
  your serial path clean, does your flash keep a value, can I reset you, do
  you see my network. A red health check is never your regression.
- **GitHub is where projects come from.** A rig without a GitHub token can add
  no project; with one — a fine-grained token that reads your repositories
  and their Actions artifacts — adding a project is pasting its URL.
- **Standalone first, connected when you want.** The rig's dashboard is the
  rig's. Connecting it to a farm gives it a page there too, the same page,
  and lets the farm hand it runs.

## Where things are

| | |
|---|---|
| The rig software | [Alteriom/esp32-rig](https://github.com/Alteriom/esp32-rig) — releases are two wheels, the dashboard bundle and the pinned health check firmware |
| The health check firmware | [Alteriom/esp32-hil-firmware](https://github.com/Alteriom/esp32-hil-firmware) — its own repository, its own releases; the rig pins one |
| An example project | [Alteriom/esp32-rig-example](https://github.com/Alteriom/esp32-rig-example) — the smallest project a rig can run, with the workflow that builds its bundle |
| The reference project | [painlessMesh](https://github.com/Alteriom/painlessMesh) — the mesh library this rig was built to validate; ships with the rig, removable |

Apache-2.0. Issues and pull requests on the repository.
