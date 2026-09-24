# Troubleshooting

In the order things usually go wrong. Each entry says what you see, what it
is, and what to do. Start with `rig/verify-rig.sh`: it checks every
precondition the rig depends on and names the one that is broken.

## A board never appears on the Boards page

- **The cable.** Charge-only cables look identical and carry no data. Swap it
  for one you know carries data.
- **The wrong socket.** On a C3, C5, C6 or S3 devkit, cable the socket
  labelled **USB**, not **UART**. On the UART socket the board flashes but is
  silent on the port the rig watches. `alteriom-hil-admin boards discover`
  says when a native-USB family is reached through a bridge.
- **Permissions.** The rig's user needs the `dialout` group for serial ports
  and `video` for the Pi's throttling and temperature readings. The installer
  adds both; log out and in, or reboot, for a new membership to apply.
- **Discovery has not run.** The service discovers when it starts and when
  you press **Rediscover** on the Boards page. A board plugged in later shows
  up on the next discovery. Registration is automatic unless
  `inventory.auto_register` was turned off in the host configuration.

## A board appears but every check on it is red

- **Power.** The hub must have its own supply of 4 A or more. A board on a
  starved hub boots, browns out under the radio, and fails in ways no
  firmware explains. The host's own health snapshot reports the Pi's
  throttling too.
- **The cable, again.** The serial check sends a kilobyte and expects it back
  byte for byte; a marginal cable drops bytes only on long lines, which is why
  the check uses one.
- **The board.** A part whose flash has worn out passes boot and serial and
  fails flash. Replace it; that is what the check is for.

## Every board is red on the same check

That is the rig, not the boards, and the queue has paused itself with the
reason. Radio: the rig's access point is down (`sudo systemctl status
hostapd`) or its password file changed. Uplink: the probe service on the host
is not running. Queue: the broker is not running or not enabled in the host
configuration. Fix it and run the health check again; a pass resumes the
queue, or resume it yourself from the Runs page.

## "board map not found" from verify-rig.sh

A legacy check. Boards are registered by the service now; the warning is
about a YAML file older rigs were configured with. If the Boards page lists
your boards, ignore it.

## Settings → Projects offers no Add project

The rig has no GitHub token. Add one under **Settings → Rig → GitHub**, or
`sudo alteriom-hil-admin github set` on the host, then
`alteriom-hil-admin github check` to see who the token is and that GitHub
accepts it. A fine-grained token needs **Contents: read** and **Actions:
read** on the repositories the rig will test.

## Adding a project is refused

The message is GitHub's answer, and for a fine-grained token it names the
repositories the token reaches: the new one is not among them (not selected
when the token was made, or an organisation that has not approved it).
Add it to the token's *Repository access* on GitHub, press **Check again**
on Settings → Rig → GitHub, and add the project again. Or the URL is not a
GitHub repository.

## Get firmware from GitHub is refused with a 403

The token sees the repository but may not list its Actions artifacts: it
lacks **Actions: read** there. The GitHub card's per-project table says so
("fetches its bundles: no").

## The Overview says the token expires soon

Fine-grained tokens expire. Make a new one on GitHub with the same
repositories and permissions and paste it under Settings → Rig → GitHub
(**Replace**); nothing else changes.

## Get firmware from GitHub finds nothing

The rig looks for the newest artifact named `hil-artifacts` (or what the
project's supply block names) uploaded by a run of the project's supply
workflow (`.github/workflows/hil.yml` by default). Check the workflow ran on
the branch you expect, that it uploaded the artifact under that name, that
the artifact has not expired, and that the token has **Actions: read**. A
run of a different workflow is not taken, on purpose.

## A run is refused at submit for want of a bundle

The rig does not build firmware. A run flashes a bundle the rig holds for
the commit, or one you name. Fetch one first from the project's page, or
have your CI hand one over (`POST /api/v1/artifacts`). The message names
the workflow the rig expects the bundle from.

## The bundle is refused

The rig re-hashes every image and every component and checks each
component sits at its stated offset inside the merged image. A refusal
names what disagreed. Most often: the manifest's revision key is missing
or not the 40-character commit; a family in the manifest has no image; a
file was rebuilt after the manifest was written.

## The dashboard says the key is wrong

The rig's own key is `sudo cat /etc/alteriom-hil/api-token` on the host. Keys
you made with `alteriom-hil-admin keys create` are shown once, at creation.
A key is kept in that browser tab only.

## The host is "unhealthy"

The host health snapshot (every five minutes, and `alteriom-hil-admin health
refresh` on demand) fails the host on: disk above the critical percentage,
the Pi reporting under-voltage or throttling, fewer boards than
`health.minimum_boards`, or the service down. The dashboard's Host page shows
which. Under-voltage on a Pi 5 is almost always a supply that is not the
official 27 W one.

## A board stops answering mid-suite

A board that hard-hangs stays hung: port open, silent, no watchdog. The bank
resets a silent board once, over RTS/DTR, before anything is measured, and
names one that stays dead rather than letting a later test blame the
project. If the same board does this often, it is that board or its port.

## Where the logs are

- **The service:** `journalctl -u alteriom-hil-farm -e`
- **A run:** its page on the dashboard keeps the pipeline log, per-board
  serial captures, the JUnit results and the report; the same files are under
  the run's directory in `/var/lib/alteriom-hil/`.
- **The host health snapshot:** `alteriom-hil-admin status`.

## Reinstalling from scratch

`alteriom-hil-admin backup create` writes the job database, the registries,
the configuration and the pinned bundles (never a secret) to the backup
directory; nightly by default. `backup restore --apply` puts one back. The
GitHub token and the API key are not in a backup: set them again.
