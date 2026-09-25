# esp32-rig

Hardware-in-the-loop testing for ESP32 firmware: a bank of real boards, a
Raspberry Pi beside them, and a service that flashes, runs a suite, captures
what the boards said and answers with a verdict.

Compile-only CI cannot catch what a radio does, what an OTA does, what a board
does after four hours, or what two boards do to each other. A rig can. This is
the software for one.

```
        your CI                 your rig
   ┌──────────────┐        ┌────────────────────┐
   │ build the    │ bundle │  flash  ▸  run  ▸  │   ESP32  ESP32-C3
   │ firmware     ├───────▸│  capture ▸ verdict │   ESP32-C5  ESP32-C6
   └──────────────┘        └────────────────────┘   ESP32-S3  ESP8266
```

## What it is

- **A service on the rig.** It queues runs, holds the boards, flashes an
  artifact your CI built, runs your suite against real hardware, captures
  every serial line and keeps the evidence. It has a dashboard.
- **A hardware abstraction layer.** Flashing, the serial console, power,
  discovery, and the pytest fixtures a suite is written against. Adding a
  board family is a description, not a rewrite.
- **A health check of its own.** Firmware the rig flashes to every board to
  answer one question before yours: is this rig fit to test on? Boots,
  serial, flash, reset, radio, wiring — per board, every release.
- **Optional: a portal.** Several rigs, one place to submit to. A rig runs
  perfectly well without one, and joining is the rig owner's choice.

## What it is not

It does not build your firmware. Your CI does that and hands the rig an
artifact with a manifest; the rig flashes exactly that and can say afterwards
precisely what it flashed. A rig with a toolchain on it is a rig that can
disagree with your CI about what "the same commit" means.

## Getting started

You need a Linux host (a Raspberry Pi 4 or 5 is what this was built on), at
least two dev boards, and a powered USB hub. **The guide** is at
[alteriom.github.io/esp32-rig](https://alteriom.github.io/esp32-rig/): [getting started](https://alteriom.github.io/esp32-rig/getting-started/)
in six steps, [what to buy and why](https://alteriom.github.io/esp32-rig/hardware/), [bring-up](https://alteriom.github.io/esp32-rig/bringup/)
in order, [the dashboard](https://alteriom.github.io/esp32-rig/dashboard/), [projects and your CI](https://alteriom.github.io/esp32-rig/projects/)
with [an example project](https://github.com/Alteriom/esp32-rig-example) to copy,
and [the API](https://alteriom.github.io/esp32-rig/api/). Its source is `docs/` here; a change to it is
published by `docs.yml`.

```bash
git clone https://github.com/Alteriom/esp32-rig
cd esp32-rig
rig/setup-runner.sh           # a virtualenv, the packages, the tools
rig/install-health-service.sh # the service, its units, udev rules
rig/verify-rig.sh             # what is missing, and what to do about it
```

Then open the dashboard, register your boards, and run the health check.

## Installing it as packages

A release is two wheels, the dashboard bundle, the Rig Health Check firmware
and a `release.json` naming each with its digest, attached to a
[GitHub release][releases]. Not PyPI yet.

```bash
base=https://github.com/Alteriom/esp32-rig/releases/download/v1.0.0
pip install   $base/alteriom_hil_core-1.0.0-py3-none-any.whl   $base/alteriom_hil-1.0.0-py3-none-any.whl
```

Both on one command line: that is what satisfies `alteriom-hil`'s dependency on
`alteriom-hil-core` without a package index.

The firmware comes with it, one bundle for every board family the rig supports,
so bringing a rig up needs no compiler and no toolchain. The firmware itself has
a repository of its own, [esp32-hil-firmware][firmware], with its own releases;
a rig release pins one (`canary/firmware.json`) and carries it. `alteriom-hil-admin
upgrade` installs and pins it, and the health check flashes what is pinned: the
same build for everyone who installs that release, which is what makes one
rig's health check comparable to another's. `--no-firmware` leaves a canary you
pinned yourself alone.

The commands it brings: `alteriom-hil-service` (the farm), `alteriom-hil-admin`
(configuration, keys, boards, providers), `alteriom-hil-health` (is this host
fit), `alteriom-hil-agent` (a rig that works for a portal), `alteriom-hil-flash`
(flash an artifact by its manifest).

A rig that already has them upgrades with the release rather than by hand:

```bash
alteriom-hil-admin upgrade --from ./dist          # a release you downloaded
alteriom-hil-admin upgrade                        # or the one your portal names
```

which checks every file against `release.json` and installs nothing if one
disagrees.

[releases]: https://github.com/Alteriom/esp32-rig/releases
[firmware]: https://github.com/Alteriom/esp32-hil-firmware
[example]: https://github.com/Alteriom/esp32-rig-example

## Writing a suite

A suite is pytest. The rig gives it a bank of boards as fixtures; what you do
with them is yours:

```python
def test_the_node_rejoins_after_a_reset(bank):
    node = bank.take("esp32-c6")
    node.reset()
    assert node.wait_for("mesh: connected", timeout=30)
```

A **profile** is the document that says where your firmware comes from, which
boards a run needs, and what to run — one file, checked in, so a run is
reproducible and a second project does not mean a second rig.

## Your project on the rig

A *project* is a GitHub repository whose firmware the rig flashes and whose
test suite it runs. A rig runs the projects it lists and no other. Two come
with the rig: the **Rig example** ([Alteriom/esp32-rig-example][example], a
small firmware and its suite -- the project to try a rig with, and to copy)
and the painlessMesh reference; both are yours to change or to remove from
the rig (and restore later). The Rig Health Check is not a project but the
rig's own firmware, installed with each release and run from Boards. Your
project needs GitHub first: the rig checks a project's repository out for
every run and fetches the firmware its CI built, and it can do neither
without a token. Give it one from the dashboard (**Settings → Rig → GitHub**,
or the Projects page while it is missing), or on the rig:

```bash
sudo alteriom-hil-admin github set     # a fine-grained token: Contents read, Actions read
sudo alteriom-hil-admin github check   # who the token is, and that GitHub accepts it
```

Until then Settings → Projects says so and offers no Add project. With it,
**Settings → Projects → Add project** asks for the repository (checked with
GitHub before anything is written), the branch a run is for by default
(empty takes the repository's own), where the pytest suite lives in it,
which chip families a run takes, and the CI workflow that builds the firmware
bundle. The rig writes the profile document under its state directory
(`/var/lib/alteriom-hil/profiles/` on a standard install), reads it back at
once, and an upgrade leaves it alone. Each project has a page there: its
configuration, the bundles the rig holds for it, its recent runs, and
**Get firmware from GitHub**, which takes the newest artifact the supply
workflow uploaded -- how a rig on a LAN, which no CI can reach, gets the
firmware its CI built. A finished run can be deleted from its page, and a
project's runs from the project's; what a run left is yours to keep or not. The same is done over the API (`GET`/`POST /api/v1/projects`,
`POST /api/v1/projects/<name>/fetch`) with an admin key. The rig does not
build firmware: your CI builds a bundle, the rig fetches it or takes it when
the CI hands it over (`POST /api/v1/artifacts`), and a run flashes it.

The rig's overview also shows the public page of the farm its software comes
from -- how many rigs and boards, how they are doing -- with a way to connect
this rig to it. Nothing there is yours; connecting is a choice, made in
Settings. `farm.public_url` in the host configuration points it at another
farm, or `off` shows none.

## Licence

Apache-2.0. The patent grant matters for a tool people run beside their own
hardware and build on.
