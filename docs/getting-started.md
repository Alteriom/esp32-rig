# Getting started

From a box of parts to the first green health check, in about an hour once
the parts are on hand. Everything here is the standalone rig on your bench;
connecting it to a farm is [a separate step](farm.md) you take later, or
never.

## 1. What you need

- A Linux host. A Raspberry Pi 4 or 5 with 2 GB or more is what this was
  built on; any small Debian or Ubuntu box with USB works. Wired Ethernet.
- A **powered** USB hub with its own supply of 4 A or more. Boards draw more
  than a Pi's ports give, and the first symptom of a starved hub is a board
  that "flakes" in ways no firmware explains.
- Two or more ESP32-family dev boards: esp32, esp32-c3, esp32-c5, esp32-c6,
  esp32-s3 or esp8266, in any mix. [Hardware](hardware.md) says which boards
  and cables, and why.

## 2. Install the rig software

On the host, as a user with `sudo`. Pick the release you are installing on
the [releases page](https://github.com/Alteriom/esp32-rig/releases); the
commands below say `1.0.170`.

```bash
sudo apt-get install -y python3-venv git curl
git clone https://github.com/Alteriom/esp32-rig ~/esp32-rig
cd ~/esp32-rig && git checkout v1.0.170
rig/setup-runner.sh              # a virtualenv, the packages, esptool, udev rules, group memberships
rig/install-health-service.sh    # the service, its units, the host configuration, the API key
rig/verify-rig.sh                # what is missing, and what to do about it
```

`verify-rig.sh` says what is missing and what to do about it. The last line
you want ends in `rig is ready`, or, before any board is plugged in, `rig is
set up, with no boards registered yet`.

Then the release's health check firmware and dashboard, checked against the
release's checksums and pinned:

```bash
mkdir -p ~/esp32-rig-release/1.0.170 && cd ~/esp32-rig-release/1.0.170
base=https://github.com/Alteriom/esp32-rig/releases/download/v1.0.170
for f in release.json SHA256SUMS alteriom_hil_core-1.0.170-py3-none-any.whl alteriom_hil-1.0.170-py3-none-any.whl alteriom-hil-dashboard-1.0.170.tar.gz alteriom-hil-canary-1.0.7.tar.gz; do curl -fsSLO "$base/$f"; done
sha256sum -c SHA256SUMS
~/.local/share/alteriom-hil/venv/bin/alteriom-hil-admin upgrade --from .
```

!!! note "A checkout and a release"
    The checkout supplies what a package cannot: the installer, the units,
    the udev rules, the profiles. The release supplies what must be the
    same on every rig: the wheels, the dashboard bundle and the pinned
    health check firmware. Later releases are installed with `upgrade`
    alone. [Bring-up](bringup.md) has the long form, including hardening
    the host and the rig's own test network.

## 3. Open the dashboard

The dashboard listens on the host, port 8090. From the host itself:

```bash
xdg-open http://127.0.0.1:8090 2>/dev/null || echo "open http://127.0.0.1:8090"
```

From another machine on your LAN, put it behind the rig's own reverse proxy
once (it allows your LAN and nothing else):

```bash
sudo ~/esp32-rig/rig/install-dashboard-proxy.sh --lan --network 192.168.1.0/24
```

and open `http://<the rig's address>/`. Sign in with the rig's key:

```bash
sudo cat /etc/alteriom-hil/api-token
```

The key stays in that browser tab. The dashboard opens on the rig's own
page: its boards, its health, its runs. [The dashboard](dashboard.md) walks
through every page.

## 4. Plug the boards in and run the health check

Plug the boards into the hub. On the dashboard's **Boards** page they appear
within a minute, named by family and the last four characters of their MAC
(`esp32-c3-1b30`). The service registers them itself; nothing to type.

Press **Run the Rig Health Check**. The rig flashes every board with its own
small firmware and asks each one whether it boots, whether its serial path
is clean, whether its flash keeps a value, whether it can be reset, whether
it sees the rig's network. A few minutes later every board reads **healthy**
on the Boards page, or says exactly what failed.

!!! tip "A red health check is a hardware or a cable problem"
    The health check firmware depends on nothing of yours. If a board fails
    it, look at the board, the cable and the hub's supply, in that order —
    [Troubleshooting](troubleshooting.md) has the usual suspects.

## 5. Connect GitHub and add your project

A project is a GitHub repository the rig checks out and whose CI builds the
firmware the rig flashes; the rig needs a token to do either. Make a
fine-grained token with **Contents: read** and **Actions: read** on the
repositories you will test, and give it to the rig — on the dashboard under
**Settings → Rig → GitHub**, or on the host:

```bash
sudo alteriom-hil-admin github set
```

Then **Settings → Projects → Add project**, paste the repository's URL and
press **Look it up**. The rig reads the repository and fills the form in;
check it and add. [Projects and your CI](projects.md) explains what the rig
found, what it guessed, and what your CI has to produce — and points at the
example project you can copy.

## 6. Run

**Runs → New run**: choose the project, the bundle to flash (fetched from
GitHub with **Get firmware from GitHub** on the project's page, or handed over
by your CI), and press run. The run's page follows the stages live and keeps
what it left: logs, serial captures, results, a report.

That is the rig. What follows in this guide is detail: the
[hardware](hardware.md) and [bring-up](bringup.md) in depth, the
[health check firmware](health-check.md), [connecting to a farm](farm.md),
the [API](api.md).
