# Bring-up

The order to do things in, on a fresh host, so nothing has to be undone. About
an hour once the parts are on hand. [Getting started](getting-started.md) is
the short form of this page; here is the why, and the steps a fussier host
needs.

## 1. The host, and your key

Flash Raspberry Pi OS Lite (64-bit) or a minimal Debian or Ubuntu; enable
SSH; give the host a DHCP reservation or a static address. Create the user
the rig will run as, sudo-capable, not root. Install your SSH key from the
workstation you will administer the rig from, **before anything else**:

```bash
ssh-copy-id <user>@<rig-address>
ssh -o PasswordAuthentication=no <user>@<rig-address> true
```

From Windows PowerShell, which has no `ssh-copy-id`:

```powershell
type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh <user>@<rig-address> "mkdir -p ~/.ssh && chmod 700 ~/.ssh && tr -d '\r' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
```

The hardening step turns password logins off, and a rig is usually the
machine nobody has a screen and keyboard for. `harden-pi.sh` refuses to run
until a key is installed, for that reason.

## 2. Plug the boards in

Into the powered hub, in port order if you like labels. Nothing below needs
them unplugged again. Native-USB families through the socket labelled
**USB**, not **UART** ([Hardware](hardware.md)).

## 3. Install the rig

```bash
git clone https://github.com/Alteriom/esp32-rig ~/esp32-rig
cd ~/esp32-rig
git checkout v1.0.169            # the release you are installing; see the releases page
rig/setup-runner.sh              # a virtualenv, the packages, esptool, udev rules, group memberships
rig/install-health-service.sh    # the service, its units, the host configuration, the API key
rig/verify-rig.sh                # what is missing, and what to do about it
```

`setup-runner.sh` makes an isolated virtual environment at
`~/.local/share/alteriom-hil/venv` (Debian's system Python is not touched),
installs the rig's two packages from the checkout, and adds the rig's user
to `dialout` (serial ports) and `video` (the Pi's throttling and temperature
readings). Re-running it is safe. Log out and in once for the new group
memberships.

`install-health-service.sh` writes the systemd units (the service, the health
timer, the nightly backup), the host configuration at
`/etc/alteriom-hil/config.yaml`, the API key at `/etc/alteriom-hil/api-token`,
and starts the service. Nothing of the service is copied out of the venv:
the launcher, the agent, the health check and the admin CLI run by name from
it.

Then the release's firmware and dashboard bundle, checked against the
release's own checksums and pinned:

```bash
mkdir -p ~/esp32-rig-release/1.0.169 && cd ~/esp32-rig-release/1.0.169
base=https://github.com/Alteriom/esp32-rig/releases/download/v1.0.169
for f in release.json SHA256SUMS alteriom_hil_core-1.0.169-py3-none-any.whl alteriom_hil-1.0.169-py3-none-any.whl alteriom-hil-dashboard-1.0.169.tar.gz alteriom-hil-canary-1.0.7.tar.gz; do curl -fsSLO "$base/$f"; done
sha256sum -c SHA256SUMS
~/.local/share/alteriom-hil/venv/bin/alteriom-hil-admin upgrade --from .
```

`upgrade` checks every file against `release.json` and installs nothing if
one disagrees; it installs the wheels, unpacks the dashboard, imports the
health check firmware into the rig's artifact store and pins it. The
firmware's file name is in `release.json` under `firmware.name`.

!!! note "Why both a checkout and a release"
    The checkout supplies what a package cannot: the installer, the units,
    the udev rules, the profiles. The release supplies what must be the
    same on every rig: the wheels, the dashboard bundle and the pinned
    health check firmware. A later release is installed with `upgrade`
    alone, from a directory you downloaded or from the farm the rig is
    connected to.

## 4. Harden the host

Once the key-only login from step 1 works:

```bash
cd ~/esp32-rig/rig
HIL_LAN_CIDR=192.168.1.0/24 HIL_SSH_USER="$USER" ./harden-pi.sh
```

Limits SSH to your LAN, disables password and root SSH logins, enables
unattended security updates, selects headless boot, and disables desktop,
discovery, printing, Bluetooth and RPC services. Keep the original session
open until a second key-only login succeeds.

## 5. Let the boards be found

The service discovers boards when it starts and when you press
**Rediscover** on the Boards page, and registers what it finds
(`inventory.auto_register`, on by default). A board is named by its family
and the last four characters of its MAC. Nothing to type.

`udevadm info -a -n /dev/ttyACM0 | grep KERNELS` gives each port's USB path
if you want stable `/dev/esp32-farm-*` names in the udev rules as well; the
rig does not need them, since it finds a board by its MAC on whichever port
it is.

## 6. The rig's own network

The radio checks need an access point the boards can see, and the uplink and
queue checks need a probe and a broker on the host. Both are one script each,
run once:

```bash
sudo ~/esp32-rig/rig/setup-gateway-network.sh    # hostapd on the host's radio; the probe service
sudo ~/esp32-rig/rig/setup-mqtt-broker.sh        # mosquitto, local only
```

They fill in the `gateway` and `mqtt` sections of the host configuration.
Without them, the radio, uplink and queue checks skip and say so; the rest
of the health check runs.

## 7. Reach the dashboard

From the host, `http://127.0.0.1:8090`. From your LAN, the rig's own reverse
proxy, allowing your LAN and nothing else:

```bash
sudo ~/esp32-rig/rig/install-dashboard-proxy.sh --lan --network 192.168.1.0/24
```

For a rig with a name on the Internet, `--host rig.example.org`, then once
DNS and ports 80/443 reach it, `--host rig.example.org --issue --email you@example.org`
for a Let's Encrypt certificate. Sign in with the rig's key:

```bash
sudo cat /etc/alteriom-hil/api-token
```

## 8. First health check

**Boards → Run the Rig Health Check.** Every board healthy, or the check that
was not and why. Then [connect GitHub and add a project](projects.md).

## Later: upgrading

From the dashboard: **Settings → Rig → Software** says what is installed and
what is newer; **Check for updates** asks now; **Install** fetches the
release, checks every file against the release's own document, and hands it
to the rig's update unit, which installs the wheels, the dashboard and the
pinned health check firmware, moves the checkout to the release's tag,
re-runs the installer and restarts the service. **Install updates
automatically** does the same on its own when the rig is idle, and is off
until you turn it on. What happened is in
`/var/lib/alteriom-hil/update/update.log`.

By hand, the same thing:

```bash
alteriom-hil-admin upgrade --from <directory with the release's files>    # a release you downloaded
alteriom-hil-admin upgrade                                                # the one your farm names, if connected
```

`--no-firmware` leaves a health check firmware you pinned yourself alone.
