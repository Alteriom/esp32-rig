#!/usr/bin/env python3
"""Draw the per-board power-relay wiring diagram (relay-wiring.svg).

Companion to ``docs/relay-power-wiring.md``. Pure Python, no dependencies:

    python3 render_relay_wiring.py            # writes ./relay-wiring.svg

The PNG next to it is a screenshot of the SVG (see the doc for the
chromium one-liner); regenerate both when CHANNELS below changes.

What the drawing encodes, so the code and the doc cannot drift:

* one relay channel per hub port per board (channel n = hub port n =
  ``/dev/esp32-farm-0n``), boards powered through the **NC** contact so a
  de-energised relay (Pi off, 12 V brick off, GPIO unconfigured) keeps
  every board running;
* only the USB cable's 5 V core is diverted through the relay; D+, D- and
  GND stay on the breakout's pass-through;
* relay inputs in **high-level trigger** mode, driven straight from Pi 5
  GPIOs that default to pull-down at boot, with DC- tied to Pi GND;
* a dedicated 12 V brick on DC+/DC- — nothing from the Pi's 5 V/3V3 pins.
"""

from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "relay-wiring.svg"

# ----------------------------------------------------------------- channel map
# (relay channel, Pi header pin, BCM GPIO). Every GPIO is in the 9-27 range
# whose RP1/BCM power-on default is pull-DOWN -> relay stays de-energised
# while the Pi boots. All on the even (outer) header row so the wires run
# straight up out of the header without crossing the odd row.
CHANNELS = [
    (1, 12, 18),
    (2, 16, 23),
    (3, 18, 24),
    (4, 22, 25),
    (5, 32, 12),
    (6, 36, 16),
    (7, 38, 20),
    (8, 40, 21),
]
GND_PIN = 6  # Pi GND -> relay DC- (the signal return for IN1..IN8)
HUB_PORTS = 7  # channel 8 has no hub port on a 7-port hub: spare

# 40-pin header, physical pin -> name (BCM numbers as "GPIOn")
HEADER = {
    1: "3V3", 2: "5V", 3: "GPIO2", 4: "5V", 5: "GPIO3", 6: "GND", 7: "GPIO4",
    8: "GPIO14", 9: "GND", 10: "GPIO15", 11: "GPIO17", 12: "GPIO18",
    13: "GPIO27", 14: "GND", 15: "GPIO22", 16: "GPIO23", 17: "3V3",
    18: "GPIO24", 19: "GPIO10", 20: "GND", 21: "GPIO9", 22: "GPIO25",
    23: "GPIO11", 24: "GPIO8", 25: "GND", 26: "GPIO7", 27: "ID_SD",
    28: "ID_SC", 29: "GPIO5", 30: "GND", 31: "GPIO6", 32: "GPIO12",
    33: "GPIO13", 34: "GND", 35: "GPIO19", 36: "GPIO16", 37: "GPIO26",
    38: "GPIO20", 39: "GND", 40: "GPIO21",
}
for _ch, _pin, _gpio in CHANNELS:
    assert HEADER[_pin] == f"GPIO{_gpio}", (_ch, _pin, _gpio)
    assert _pin % 2 == 0, "wires are routed from the even (outer) header row"
    assert 9 <= _gpio <= 27, "only pull-down-at-boot GPIOs keep relays off"
assert HEADER[GND_PIN] == "GND"

# ----------------------------------------------------------------- geometry
W, H = 1600, 1140
PITCH = 140
X0 = 370  # centre of channel 1


def xc(ch):
    return X0 + PITCH * (ch - 1)


# colours
RED = "#d62828"  # 5 V VBUS (switched)
DKRED = "#8b1a1a"  # 12 V
BLK = "#111111"  # GND / DC-
GRN = "#1b8a3c"  # IN signals
GREY = "#8a8f98"  # USB data pass-through
BLUE = "#2b6cb0"  # USB 3.0 uplink
INK = "#1f2933"
FILL_RELAY = "#2f6fd1"
FILL_TERM = "#2c5aa0"
FILL_PCB = "#c62839"
FILL_PCB_EDGE = "#8e1c29"

out = []


def w(s):
    out.append(s)


def text(x, y, s, size=13, anchor="middle", weight="normal", color=INK, family="sans-serif", rot=None):
    t = f' transform="rotate({rot} {x} {y})"' if rot is not None else ""
    w(
        f'<text x="{x}" y="{y}" font-size="{size}" text-anchor="{anchor}" '
        f'font-weight="{weight}" fill="{color}" font-family="{family}"{t}>{s}</text>'
    )


def rect(x, y, wd, ht, fill="none", stroke=INK, sw=1.5, rx=3, dash=None, opacity=None):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    o = f' fill-opacity="{opacity}"' if opacity is not None else ""
    w(
        f'<rect x="{x}" y="{y}" width="{wd}" height="{ht}" rx="{rx}" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{d}{o}/>'
    )


def wire(points, color, sw=2.4, dash=None):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    pts = " ".join(f"{x},{y}" for x, y in points)
    w(
        f'<polyline points="{pts}" fill="none" stroke="{color}" '
        f'stroke-width="{sw}" stroke-linejoin="round" stroke-linecap="round"{d}/>'
    )


def dot(x, y, color, r=3.5):
    w(f'<circle cx="{x}" cy="{y}" r="{r}" fill="{color}"/>')


def terminal(x, y, label=None, size=(26, 36)):
    """One screw terminal: blue block with a screw head; centre-top at (x, y)."""
    wd, ht = size
    rect(x - wd / 2, y, wd, ht, fill=FILL_TERM, stroke="#1b3a6b", rx=2)
    w(f'<circle cx="{x}" cy="{y + ht / 2}" r="7" fill="#dfe7f5" stroke="#1b3a6b" stroke-width="1"/>')
    w(f'<line x1="{x - 4}" y1="{y + ht / 2}" x2="{x + 4}" y2="{y + ht / 2}" stroke="#1b3a6b" stroke-width="1.5"/>')
    if label:
        text(x, y + ht + 13, label, size=11)


# ================================================================== canvas
w(
    f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
    'font-family="sans-serif">'
)
w(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')
text(40, 34, "Per-board USB power switching — 8-channel 12 V relay module wired to the HIL rig",
     size=22, anchor="start", weight="bold")
text(40, 56, "Channel n switches ONLY the 5 V core of board n's USB cable. Boards run through NC: "
     "relay de-energised = board powered. Trigger jumpers on HIGH. DC− is common with Pi GND.",
     size=13, anchor="start", color="#3d4852")

# ------------------------------------------------------------------ USB hub
HUB_Y0, HUB_Y1 = 78, 128
hub_x0, hub_x1 = xc(1) - 60, xc(HUB_PORTS) + 60
rect(hub_x0, HUB_Y0, hub_x1 - hub_x0, HUB_Y1 - HUB_Y0, fill="#eef1f5", stroke=INK, rx=6)
text((hub_x0 + hub_x1) / 2, HUB_Y0 + 20, "Powered USB hub (existing) — 5 V ≥4 A external PSU, uplink to Pi 5 USB 3.0",
     size=13, weight="bold")
for ch in range(1, HUB_PORTS + 1):
    x = xc(ch)
    rect(x - 12, HUB_Y1 - 14, 24, 14, fill="#ffffff", stroke=INK, rx=1)
    text(x, HUB_Y1 - 3, f"p{ch}", size=10)
# spare column (channel 8)
x8 = xc(8)
rect(x8 - 60, HUB_Y0, 120, HUB_Y1 - HUB_Y0, fill="none", stroke=GREY, dash="6 4", rx=6)
text(x8, HUB_Y0 + 20, "channel 8: spare", size=11, color="#4a5568")
text(x8, HUB_Y0 + 36, "(2nd hub p1 / bench)", size=11, color="#4a5568")
# uplink (unchanged from the blueprint)
wire([(hub_x1, 103), (1560, 103), (1560, 1090), (776, 1090)], BLUE, sw=3)
text(1572, 560, "USB 3.0 A–A uplink (existing, unchanged)", size=11, color=BLUE, rot=90)

# --------------------------------------------------------- per-channel columns
SPL_Y0, SPL_Y1 = 176, 226  # VBUS splice / breakout
BRD_Y0, BRD_Y1 = 262, 340  # ESP32 board
MOD_Y0 = 392  # relay module top edge (terminals straddle it)
T_Y = 386  # top of the NC/COM/NO terminal blocks

for ch, pin, gpio in CHANNELS:
    x = xc(ch)
    # hub cable (grey: whole cable) down to the splice
    src_y = HUB_Y1 if ch <= HUB_PORTS else HUB_Y1
    wire([(x, src_y), (x, SPL_Y0)], GREY, sw=5, dash=None if ch <= HUB_PORTS else "8 5")
    # splice box
    rect(x - 40, SPL_Y0, 80, SPL_Y1 - SPL_Y0, fill="#fff7e6", stroke="#b7791f", rx=4)
    text(x, SPL_Y0 + 15, "VBUS splice", size=10, weight="bold")
    text(x, SPL_Y0 + 28, "cut red core / A-breakout", size=9)
    text(x, SPL_Y0 + 42, "D+ D− GND untouched", size=9, color="#4a5568")
    # splice pins
    for px in (x - 24, x, x + 24):
        w(f'<rect x="{px - 4}" y="{SPL_Y1 - 2}" width="8" height="6" fill="#b7791f"/>')
    # data pass-through to the board
    wire([(x, SPL_Y1 + 4), (x, BRD_Y0)], GREY, sw=5)
    # board
    rect(x - 40, BRD_Y0, 80, BRD_Y1 - BRD_Y0, fill="#e6f4ea", stroke="#276749", rx=5)
    text(x, BRD_Y0 + 18, f"ESP32 board {ch:02d}", size=11, weight="bold")
    text(x, BRD_Y0 + 34, f"/dev/esp32-farm-{ch:02d}", size=9, family="monospace")
    text(x, BRD_Y0 + 50, "USB-powered", size=9, color="#4a5568")
    text(x, BRD_Y0 + 64, f"hub port {ch}" if ch <= HUB_PORTS else "spare port", size=9, color="#4a5568")
    # NC -> board 5 V (left side of the column)
    wire([(x - 32, T_Y), (x - 32, 362), (x - 56, 362), (x - 56, 240), (x - 24, 240), (x - 24, SPL_Y1 + 4)], RED)
    # hub 5 V -> COM (right side of the column)
    wire([(x + 24, SPL_Y1 + 4), (x + 24, 240), (x + 56, 240), (x + 56, 374), (x, 374), (x, T_Y)], RED)

# label the VBUS wires once (channel 1) and the NO once
text(xc(1) - 62, 300, "5 V → board (from NC)", size=10, color=RED, rot=-90)
text(xc(1) + 70, 300, "5 V from hub (to COM)", size=10, color=RED, rot=90)

# ------------------------------------------------------------ relay module
MOD_X0, MOD_X1 = xc(1) - 80, xc(8) + 80
MOD_Y1 = 736
rect(MOD_X0, MOD_Y0, MOD_X1 - MOD_X0, MOD_Y1 - MOD_Y0, fill=FILL_PCB, stroke=FILL_PCB_EDGE, sw=2, rx=8)
for cx, cy in ((MOD_X0 + 14, MOD_Y0 + 14), (MOD_X1 - 14, MOD_Y0 + 14), (MOD_X0 + 14, MOD_Y1 - 14), (MOD_X1 - 14, MOD_Y1 - 14)):
    w(f'<circle cx="{cx}" cy="{cy}" r="5" fill="#ffffff" stroke="{FILL_PCB_EDGE}"/>')
text(MOD_X0 + 30, 628, "8-ch relay module, 12 V coils, opto-isolated inputs, high/low trigger jumpers (as received)",
     size=11, anchor="start", color="#ffffff")

for ch, pin, gpio in CHANNELS:
    x = xc(ch)
    # NC / COM / NO terminals
    terminal(x - 32, T_Y)
    terminal(x, T_Y)
    terminal(x + 32, T_Y)
    text(x - 32, T_Y + 50, f"NC{ch}", size=10, color="#ffffff", weight="bold")
    text(x, T_Y + 50, f"COM{ch}", size=10, color="#ffffff", weight="bold")
    text(x + 32, T_Y + 50, f"NO{ch}", size=10, color="#ffffff")
    # relay body
    rect(x - 48, 452, 96, 100, fill=FILL_RELAY, stroke="#1a3d7a", rx=3)
    text(x, 476, "SONGLE", size=10, color="#ffffff", weight="bold")
    text(x, 494, "SRD-12VDC-SL-C", size=10, color="#ffffff")
    text(x, 512, "10 A 250 VAC / 30 VDC", size=9, color="#dbe7ff")
    text(x, 530, "coil 12 V, ≈30 mA", size=9, color="#dbe7ff")
    text(x, 546, f"relay {ch}", size=9, color="#dbe7ff")
    # opto + LED row
    rect(x - 46, 570, 30, 22, fill="#111111", stroke="#000", rx=2)
    text(x - 31, 585, "817", size=8, color="#ffffff")
    w(f'<circle cx="{x + 8}" cy="581" r="5" fill="#ff5a5a" stroke="#7a0000"/>')
    text(x + 8, 604, f"LED{ch}", size=8, color="#ffffff")
    rect(x + 22, 574, 22, 14, fill="#333", stroke="#000", rx=1)
    text(x + 33, 584, "Q", size=8, color="#ffffff")
text(MOD_X0 - 8, T_Y + 24, "NO terminals:", size=10, anchor="end", color=INK)
text(MOD_X0 - 8, T_Y + 38, "leave empty", size=10, anchor="end", color=INK)

# trigger-select jumper blocks
def jumper_block(x0, label, chans):
    rect(x0, 636, 150, 80, fill="#f6d5d9", stroke=FILL_PCB_EDGE, rx=3)
    text(x0 + 75, 650, label, size=10, weight="bold")
    for r, name in enumerate(("Low", "Com", "High")):
        y = 664 + r * 16
        text(x0 + 22, y + 4, name, size=9, anchor="end")
        for i, c in enumerate(chans):
            px = x0 + 40 + i * 30
            w(f'<circle cx="{px}" cy="{y}" r="3" fill="#333"/>')
    for i, c in enumerate(chans):
        px = x0 + 40 + i * 30
        # shunt across Com + High
        rect(px - 6, 674, 12, 28, fill="#111111", stroke="#000", rx=2)
        text(px, 712, f"S{c}", size=8)


jumper_block(MOD_X0 + 30, "trigger select S1–S4: shunt on Com–High", (1, 2, 3, 4))
jumper_block(MOD_X1 - 180, "trigger select S5–S8: shunt on Com–High", (5, 6, 7, 8))

# bottom terminal block: DC+ DC- IN1..IN8
BT_Y = 694  # top of bottom terminal blocks (they straddle the module edge)
BT_X0 = 640
BT_PITCH = 44
bt_labels = ["DC+", "DC−"] + [f"IN{c}" for c, _, _ in CHANNELS]
bt_x = {}
for i, lab in enumerate(bt_labels):
    x = BT_X0 + i * BT_PITCH
    bt_x[lab] = x
    terminal(x, BT_Y, size=(30, 44))
    text(x, BT_Y - 6, lab, size=11, color="#ffffff", weight="bold")
text((bt_x["DC+"] + bt_x["IN8"]) / 2, BT_Y - 22, "power supply + signal trigger terminal", size=10, color="#ffffff")

# ------------------------------------------------------------------ 12 V PSU
PSU_X0, PSU_Y0 = 20, 728
rect(PSU_X0, PSU_Y0, 150, 56, fill="#fff", stroke=DKRED, sw=2, rx=5)
text(PSU_X0 + 75, PSU_Y0 + 18, "12 V DC brick", size=12, weight="bold", color=DKRED)
text(PSU_X0 + 75, PSU_Y0 + 33, "≥1 A, 5.5×2.1 barrel", size=10)
text(PSU_X0 + 75, PSU_Y0 + 47, "+ barrel→screw adapter", size=10)
text(PSU_X0 + 150 + 8, PSU_Y0 + 28, "+", size=13, anchor="start", weight="bold", color=DKRED)
text(PSU_X0 + 150 + 8, PSU_Y0 + 44, "−", size=13, anchor="start", weight="bold", color=BLK)
BT_BOTTOM = BT_Y + 44
DCP_Y, DCM_Y = PSU_Y0 + 24, PSU_Y0 + 40
wire([(PSU_X0 + 150, DCP_Y), (bt_x["DC+"], DCP_Y), (bt_x["DC+"], BT_BOTTOM)], DKRED)
wire([(PSU_X0 + 150, DCM_Y), (bt_x["DC−"], DCM_Y), (bt_x["DC−"], BT_BOTTOM)], BLK)
text(400, DCP_Y - 5, "12 V +  (relay coils only — never from the Pi)", size=10, anchor="start", color=DKRED)

# ------------------------------------------------------------------ Pi 5
PI_X0, PI_Y0, PI_X1, PI_Y1 = 100, 862, 770, 1108
rect(PI_X0, PI_Y0, PI_X1 - PI_X0, PI_Y1 - PI_Y0, fill="#e8f0e3", stroke="#2f855a", sw=2, rx=8)
text(PI_X0 + 16, PI_Y1 - 60, "Raspberry Pi 5 (runner host) — 40-pin header, board seen from above,", size=13, anchor="start", weight="bold")
text(PI_X0 + 16, PI_Y1 - 42, "pin 1 at the microSD end; outer row = even pins. Own 27 W USB-C PSU.", size=12, anchor="start")
text(PI_X0 + 16, PI_Y1 - 22, "Access: /dev/gpiochip* (label pinctrl-rp1), group gpio. GPIOs 12/16/18/20/21/23/24/25 are pull-down at boot.",
     size=11, anchor="start", color="#3d4852")
rect(PI_X1 - 6, 1078, 14, 24, fill="#dfe7f5", stroke=BLUE, rx=1)
text(PI_X1 - 12, 1094, "USB 3.0 →", size=10, anchor="end", color=BLUE)
# header
HX0 = 130
HY_EVEN, HY_ODD = 882, 910
used = {pin: (ch, gpio) for ch, pin, gpio in CHANNELS}
rect(HX0 - 15, HY_EVEN - 14, 30 * 20, 56, fill="#2d2d2d", stroke="#000", rx=3)
for col in range(20):
    x = HX0 + 30 * col
    for pin, y in ((2 * col + 2, HY_EVEN), (2 * col + 1, HY_ODD)):
        name = HEADER[pin]
        if pin in used:
            fill = GRN
        elif pin == GND_PIN:
            fill = "#ffffff"
        elif name == "GND":
            fill = "#9aa0a6"
        elif name in ("5V",):
            fill = "#f56565"
        elif name == "3V3":
            fill = "#f6ad55"
        else:
            fill = "#4a5568"
        w(f'<circle cx="{x}" cy="{y}" r="8" fill="{fill}" stroke="#000" stroke-width="0.8"/>')
        text(x, y + 3.5, str(pin), size=7.5, color="#000" if fill in ("#ffffff", "#f6ad55", "#f56565", GRN) else "#fff")
# names under the header for the pins we use
for pin, (ch, gpio) in used.items():
    x = HX0 + 30 * (pin // 2 - 1)
    text(x, HY_ODD + 26, f"GPIO{gpio}", size=8.5, rot=-90, anchor="end")
xg = HX0 + 30 * (GND_PIN // 2 - 1)
text(xg, HY_ODD + 26, "GND", size=8.5, rot=-90, anchor="end", weight="bold")
text(HX0 - 15, HY_ODD + 30, "pin 1 = 3V3 · pin 2 = 5V — neither is used", size=9, anchor="start", color="#4a5568")

# signal wires: even pins -> lanes -> terminals. Lane order = source x order,
# so the wires never cross (leftmost source takes the highest lane).
LANE_TOP, LANE_STEP = 796, 8
sources = [(GND_PIN, "DC−", BLK)] + [(pin, f"IN{ch}", GRN) for ch, pin, gpio in CHANNELS]
sources.sort(key=lambda s: s[0])
for lane, (pin, dest, color) in enumerate(sources):
    sx = HX0 + 30 * (pin // 2 - 1)
    dx = bt_x[dest]
    ly = LANE_TOP + lane * LANE_STEP
    wire([(sx, HY_EVEN - 8), (sx, ly), (dx, ly), (dx, BT_BOTTOM)], color, sw=2.2)
    if dest != "DC−":
        ch, gpio = used[pin]
        text(dx + 12, BT_BOTTOM + 56, f"GPIO{gpio} · p{pin}", size=9, anchor="start", rot=-90)
dot(bt_x["DC−"], DCM_Y, BLK)
text(bt_x["DC−"] - 8, DCM_Y + 14, "Pi GND joins DC− here (signal return)", size=9, anchor="end")

# ------------------------------------------------------------------ legend
LX, LY = 1100, 872
rect(LX, LY, 440, 128, fill="#fafafa", stroke="#cbd5e0", rx=6)
text(LX + 12, LY + 20, "Legend", size=12, anchor="start", weight="bold")
legend = [
    (RED, "5 V USB VBUS, switched through NC/COM (2 short wires per board)"),
    (GREY, "USB cable: D+, D−, GND untouched (breakout pass-through)"),
    (GRN, "Relay input IN1–IN8 ← Pi GPIO (3.3 V high = relay ON = board OFF)"),
    (BLK, "Pi GND → DC− (must be wired; USB shield is not the return)"),
    (DKRED, "12 V coil supply → DC+ (dedicated brick)"),
    (BLUE, "USB 3.0 uplink hub → Pi (unchanged)"),
]
for i, (c, s) in enumerate(legend):
    y = LY + 38 + i * 15
    wire([(LX + 14, y), (LX + 44, y)], c, sw=3)
    text(LX + 52, y + 4, s, size=10, anchor="start")

# channel table (top-right free space is used by the uplink; put it bottom-centre)
TX, TY = 800, 1008
text(TX, TY, "Channel map (channel = hub port = board = udev name):", size=11, anchor="start", weight="bold")
row = "  ".join(f"ch{ch}→IN{ch}=GPIO{gpio}(pin {pin})" for ch, pin, gpio in CHANNELS[:4])
text(TX, TY + 16, row, size=10, anchor="start", family="monospace")
row = "  ".join(f"ch{ch}→IN{ch}=GPIO{gpio}(pin {pin})" for ch, pin, gpio in CHANNELS[4:])
text(TX, TY + 31, row, size=10, anchor="start", family="monospace")
text(TX, TY + 48, "Drive test:  pinctrl set 18 op dl   (board 01 on)   ·   pinctrl set 18 op dh   (board 01 cut)   ·   pinctrl get 18",
     size=10, anchor="start", family="monospace")
text(TX, TY + 64, "Never fit the Low jumper: with 12 V on DC+ a 3.3 V GPIO cannot pull IN high enough to switch the relay OFF.",
     size=10, anchor="start", color=DKRED)

w("</svg>")
OUT.write_text("\n".join(out) + "\n", encoding="utf-8")
print(f"wrote {OUT} ({len(CHANNELS)} channels)")
