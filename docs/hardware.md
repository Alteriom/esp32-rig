# Hardware

What to buy, what matters about each part, and the few rules the software
assumes you kept. A working rig is a Linux host, a powered hub, two or more
dev boards and cables that carry data. Everything else is comfort.

## The parts

| Part | What matters | Why |
|---|---|---|
| **Host**: Raspberry Pi 4 or 5 with 2 GB or more, or any small Debian or Ubuntu box | wired Ethernet; USB ports; runs Python 3.9 or newer | the rig's own Wi-Fi radio is left free for the boards to see; it reaches your LAN over the wire |
| **Host power**: the official Pi supply (27 W for a Pi 5) | never power boards from the Pi's own ports | a starved Pi throttles, and the health check will tell you so |
| **Storage**: 32 GB microSD (A2) or a small SSD | endurance is fine at this write volume | runs keep logs and serial captures; retention trims them |
| **Powered USB hub**, 7 or 8 ports, external supply of **5 V at 4 A or more** | its own supply, not the host's | boards draw more than a Pi's ports give; a starved hub is a board that "flakes" |
| **Dev boards**: esp32, esp32-c3, esp32-c5, esp32-c6, esp32-s3, esp8266, in any mix | a data-capable USB connector; see the note on two sockets below | the rig identifies each by its silicon MAC, so any family can sit on any port |
| **USB cables**, short, **data-capable**, one per board | unmodified | a charge-only cable is a board that enumerates never, or only sometimes |
| **Adapters** for micro-B boards (ESP32 DevKitC, C3-DevKitM-1, ESP8266 NodeMCU) | USB-C female to micro-B male | so every rig port can be the same |

Optional, for tests that need them:

- **Per-board power switching.** A relay module on the host's GPIO in each
  board's 5 V line, or a hub that `uhubctl` can switch. Only the power-recovery
  checks need it; reset over RTS/DTR is always available and covers most of
  what a power cut would.
- **An I/O instrument** (an ESP32 board flashed as one) wired to boards with
  jumpers, for the wiring check and for suites that drive a pin and read it
  back. See the health check page.

## Two sockets on one board

The C3, C5, C6 and S3 devkits carry two USB-C sockets: one is the chip's own
**USB-Serial/JTAG**, the other a **UART bridge**. Cable them through the one
labelled **USB**, not **UART**. A board on the UART socket still answers the
flasher during discovery, but the firmware then talks on the port the rig is
not watching, and it reads as a dead board. Discovery notes when a
native-USB family is reached through a bridge, and the health check speaks
on both, so a miscabled board is found rather than mourned.

## Rules the software assumes

- **Board identity is the silicon MAC. Port identity is the hub-port path.**
  Any family on any port; nothing is hand-labelled except, if you like, the
  port numbers on the hub. The ESP32-C5 and C6 report an 8-byte EUI-64 to the
  flasher; the rig's identity is the 6-byte base MAC.
- **Boards are USB-powered only.** Flash, serial and (with a relay) the power
  cut all ride the one USB path from hub port to board.
- **The host's Wi-Fi radio is the rig's test network.** The boards associate
  with an access point the host brings up on its own radio, so the radio
  checks prove the boards see *this* rig. Keep the host on Ethernet.
- **Antennas up, boards apart.** Boards standing upright with a few
  centimetres between them, not lying in a heap: a heap is a radio test of
  the heap.

## How many boards

Two is the minimum for anything that talks board to board. The health check
is per board and is happy with one. Six, one per family, is what the rig
software is developed against; eight is a comfortable hub.

## A chassis, if you want one

The rig this software was built on sits on a 330 × 272 mm base: a lidded
control deck for the Pi, the hub and the relay module, and a card rack that
stands eight boards upright with their antennas clear. It is about a
weekend of printing and an afternoon of assembly. Nothing in the software
depends on it; a board on a desk is a board.
