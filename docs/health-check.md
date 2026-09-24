# The health check firmware

Every other run on a rig answers "is this project's firmware good on this
hardware". The **Rig Health Check** answers **"is this hardware good"**, and
that is the rig's own question. So the firmware it flashes is the rig's own,
carrying nothing of yours: a red health check is never your regression. It
is an ESP, a cable, a hub port, or the rig's own access point, broker or
uplink.

## Where it lives

The firmware has a repository of its own,
[Alteriom/esp32-hil-firmware](https://github.com/Alteriom/esp32-hil-firmware),
with its own build, its own tests and its own releases. It validates any
hardware the rig supports and changes when a family or a check changes, not
when the rig software does.

The rig **pins** one release of it. `canary/firmware.json` in the rig
repository names the release by version, source digest, tarball digest,
families, the serial commands the firmware answers and the pins it lets an
instrument be wired to. A rig release fetches that tarball, checks it against
the pin, and carries it; `alteriom-hil-admin upgrade` installs and pins it on
the rig. Every rig on a given rig release runs the same health check
firmware, byte for byte, which is what makes one rig's verdict comparable to
another's. Moving the firmware forward is a one-line change to the pin,
reviewed like any other.

## What it checks

Each check runs **once per board**, so a report is a board × check matrix
that separates two kinds of red: a check failing on **every** board is the
rig; a check failing on **one** board is that board.

| Check | Asks |
|---|---|
| **boot** | it started, it is talking, and the part answering (chip, revision, flash size) is the part the registry says is there |
| **serial** | a kilobyte comes back byte for byte: the cable, the hub port and the console driver together |
| **flash** | a value written is read back and erased: NVS on the ESP32 families, a LittleFS file on the ESP8266 |
| **reset** | the board restarts when the rig says so, and comes back as a new boot |
| **radio** | the board sees the rig's access point, joins it, gets an address |
| **uplink** | an HTTP GET from the board reaches the rig's probe and is answered |
| **queue** | a message from the board reaches the rig's MQTT broker |
| **wiring** | every jumper to an instrument carries a level both ways, named per wire when it does not |

Checks that need something the rig does not have (no instrument, no broker)
skip and say so; they do not fail.

## What a red does

- **Red on every board is the rig.** The service pauses its queue itself,
  with the reason, as soon as it records the verdicts: everything queued
  behind that check would otherwise flash boards whose rig cannot join a
  network, and report that as the project's fault. The queue resumes when a
  health check passes, or when you resume it.
- **Red on one board is that board.** It is marked on the Boards page with
  the check that failed. With quarantine turned on in the host configuration,
  a board red on its own checks twice in a row is left out of runs until a
  clean health check, or you, release it.
- **A wiring failure never pauses the rig.** An unplugged instrument fails
  every wired board; that is the instrument, not the network, and runs that
  never touch a wire are not stopped for it.

## The protocol

Newline-delimited JSON both ways, the same framing the rig's board client
already reads. Commands are `{"cmd": "..."}` frames; the firmware answers each
with one event, and announces a start with `{"evt": "boot", ...}`.

| Command | Answer |
|---|---|
| `info` | version, family, source digest, boot id, reset reason, uptime, heap, MAC, and the silicon |
| `echo` `{"text": "..."}` | the text and its length |
| `store_write`, `store_read`, `store_erase` | the operation, whether it succeeded and what was found |
| `wifi_scan` `{"ssid": "..."}` | how many networks, and the named one with RSSI and channel |
| `wifi_join` `{"ssid", "password"}` / `wifi_leave` | joined, address, gateway, RSSI, milliseconds |
| `http_get` `{"url": "http://..."}` | status, bytes, milliseconds |
| `mqtt_publish` `{"host", "port", "topic", "payload"}` | ok, bytes, milliseconds |
| `gpio_mode`, `gpio_write`, `gpio_read`, `gpio_release` `{"pin", ...}` | the operation, the pin, the level; only a wireable pin of the family, never an input-only one driven |
| `reset` | `resetting`, then a fresh `boot` |

A frame the firmware cannot parse is answered `{"evt": "error", "error": "bad
json"}`, a statement that the command did **not** run, which is what makes
the rig's resend safe. The station is never an access point on any family:
a board that brought up an AP would change what every other board on the
rig can see.

The rig's simulator answers the same commands, so the health check suite
runs in the rig's own CI without hardware. A simulated pass is evidence about
the rig's software, never about an ESP: every check carries the `hil_only`
marker for that reason.

## Running it

From the **Boards** page, **Run the Rig Health Check** checks every board;
a board's own page checks that one. A release install runs it once at the
end and is not green until the rig says its own hardware is. On the host:

```bash
sudo alteriom-hil-admin health refresh
```

writes a fresh host health snapshot (disk, throttling, temperature, the
boards seen). The check's report, per board and per check, is on the run's
page like any other run, under the health check's name.

## Its version

Three identities travel in the bundle: the firmware repository's commit it
was built from (the revision of record, what a bundle is reused by), the
digest of its source (what a board reports back over serial, so a report can
say which health check answered), and the version a person reads. The
version's `MAJOR.MINOR` is set by hand in the firmware repository; `PATCH` is
the number of commits that changed the firmware, so it rises by itself when
the firmware does and never when the rig does. The board reports it in its
`boot` and `info` frames, and the suite holds each board to the version the
run flashed.
