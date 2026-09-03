#!/usr/bin/env python3
"""Render the enclosed, relay-switched rig built from the chassis STLs.

Reuses render_rig.py's rasterizer and its Pi/hub models; every printed
part is the real STL from ../mounts/stl/ placed at its assembly position,
so the pictures show exactly what generate_chassis.py + generate_mounts.py
produce. Not part of CI. Needs numpy + matplotlib:

    python3 render_chassis.py        # writes chassis-*.png

Assembly frame (mm, Z up, plank top = 0): control bay occupies plank
x 0..300 (posts in the corners, walls 2.4 mm in from the edges), the
cable channel runs along the front edge from inside the bay to the far
end, and board stations sit behind it at a 145 mm pitch.
"""

import numpy as np

import render_rig as rr
from render_rig import View, bezier_tube, box, load_stl, render, save, translate

STL = rr.STL

# ------------------------------------------------------------- transforms


def rotz(tris, k):
    """Rotate about Z by k * 90 degrees (winding preserved)."""
    out = tris.copy()
    for _ in range(k % 4):
        x, y = out[..., 0].copy(), out[..., 1].copy()
        out[..., 0], out[..., 1] = -y, x
    return out


P = {name: load_stl(STL / f"{name}.stl") for name in (
    "bay-corner-post", "bay-splice-post", "bay-wall-long",
    "bay-wall-short-outer", "bay-wall-short-inner", "bay-lid",
    "relay-tray", "psu-cradle", "cable-channel-200", "cable-channel-100",
    "cable-channel-lid-200", "cable-channel-lid-100", "board-station",
    "station-pocket-lid",
)}

PLACED = []  # (name, dx, dy, dz, k) of every STL placed by build(): the viewer reads this


def place(scene, name, color, dx, dy, dz=0, k=0):
    PLACED.append((name, dx, dy, dz, k))
    scene.append((translate(rotz(P[name], k), dx, dy, dz), color))


# ---------------------------------------------------------------- palette

CHASSIS = (0.20, 0.21, 0.24)     # charcoal PETG: posts, walls, channel, trays
LID = (0.88, 0.47, 0.13)         # orange PETG accents: lids + pocket lids
RELAY_PCB = (0.62, 0.13, 0.17)
RELAY_BLUE = (0.14, 0.33, 0.72)
BRICK = (0.09, 0.09, 0.10)
NUT = (0.90, 0.45, 0.12)
RED = (0.80, 0.12, 0.10)
PIGTAIL = (0.10, 0.10, 0.11)



BAY_W, BAY_D, BAY_H = 300, 200, 45
CH_Y = 16
STATION_X = [320 + 145 * i for i in range(6)]
STATION_Y = 60
CHANNEL = [(280, 200), (480, 200), (680, 200), (880, 200), (1080, 100)]


# --------------------------------------------------------------- assembly


def control_bay(scene, lids=True):
    # posts: front-left as generated, then rotated into the other corners
    place(scene, "bay-corner-post", CHASSIS, 0, 0, 0, 0)
    place(scene, "bay-corner-post", CHASSIS, BAY_W, 0, 0, 1)
    place(scene, "bay-corner-post", CHASSIS, BAY_W, BAY_D, 0, 2)
    place(scene, "bay-corner-post", CHASSIS, 0, BAY_D, 0, 3)
    place(scene, "bay-splice-post", CHASSIS, 144, 0, 0, 0)
    place(scene, "bay-splice-post", CHASSIS, 156, BAY_D, 0, 2)
    # long walls: two 144 mm segments per side, ends inside the posts
    for x0 in (6, 150):
        place(scene, "bay-wall-long", CHASSIS, x0, 2.4, 0, 0)          # front
        place(scene, "bay-wall-long", CHASSIS, x0 + 144, BAY_D - 2.4, 0, 2)  # rear
    # end walls: flange must point inward (-x on the right, +x on the left)
    place(scene, "bay-wall-short-outer", CHASSIS, 2.4, 194, 0, 3)
    place(scene, "bay-wall-short-inner", CHASSIS, BAY_W - 2.4, 6, 0, 1)
    if lids:
        place(scene, "bay-lid", LID, 0, 0, BAY_H, 0)
        place(scene, "bay-lid", LID, BAY_W, BAY_D, BAY_H, 2)


def bay_contents(scene):
    pi_usb = rr.pi_node(scene, 16, 100)
    ports, uplink = rr.hub_node(scene, 16, 22)
    # relay tray + module (138 x 50 PCB, 8 relays along the far edge)
    place(scene, "relay-tray", CHASSIS, 126, 116)
    t = lambda g: translate(g, 126, 116, 0)
    scene.append((t(box(10, 9.5, 9.2, 148, 59.5, 10.8)), RELAY_PCB))
    for i in range(8):
        x = 14 + i * 16.5
        scene.append((t(box(x, 34, 10.8, x + 15, 53, 26.3)), RELAY_BLUE))   # relay
        scene.append((t(box(x, 11, 10.8, x + 15, 19, 19)), RELAY_BLUE))     # NC/COM/NO
    scene.append((t(box(48, 2, 10.8, 112, 9.5, 19)), RELAY_BLUE))          # DC/IN block
    # 12 V brick in its cradle
    place(scene, "psu-cradle", CHASSIS, 150, 30)
    scene.append((translate(box(10, 10, 3.2, 110, 55, 33), 150, 30, 0), BRICK))
    for cx in (33.6, 86.4):
        scene.append((translate(box(cx - 2.5, 4, 3.2, cx + 2.5, 61, 34.4), 150, 30, 0), rr.TIE))
    # GPIO ribbon Pi header -> relay IN block
    p0 = np.array([60, 165, 19.0])
    p3 = np.array([200, 120, 19.0])
    scene.append((bezier_tube(p0, p0 + np.array([0, 40, 10]), p3 + np.array([-40, 30, 8]), p3, r=2.6), (0.35, 0.35, 0.38)))
    # hub uplink to the Pi, hub downstream cables toward the channel mouth
    scene.append((bezier_tube(pi_usb, pi_usb + np.array([20, -30, 6]), uplink + np.array([-20, 30, 6]), uplink, r=2.2), rr.UPLINK))
    mouth = np.array([282, CH_Y + 17, 8.0])
    for i, p in enumerate(ports[:6]):
        p1 = p + np.array([0, -30, -2])
        p2 = mouth + np.array([-60, -5, 4])
        scene.append((bezier_tube(p, p1, p2, mouth + np.array([0, (i - 2.5) * 2.5, 0])), rr.CABLE))


def cable_channel(scene, lids=True):
    for x0, ln in CHANNEL:
        place(scene, f"cable-channel-{ln}", CHASSIS, x0, CH_Y)
        if lids:
            place(scene, f"cable-channel-lid-{ln}", CHASSIS, x0, CH_Y, 24)


def station(scene, sx, sy, board=True, pocket_lid=True):
    place(scene, "board-station", CHASSIS, sx, sy)
    t = lambda g: translate(g, sx, sy, 0)
    # USB-C female pigtail: overmold in the clamp, wires into the pocket
    scene.append((t(box(11.5, 5, 3.2, 30.5, 23, 11)), PIGTAIL))
    scene.append((t(box(14, 1.5, 5.5, 28, 5, 9.5)), (0.03,) * 3))            # socket mouth
    if pocket_lid:
        place(scene, "station-pocket-lid", LID, sx + 4, sy + 26, 3.2 + 16)
    else:
        for i, (x, y) in enumerate(((12, 40), (22, 40), (32, 40), (14, 54), (28, 54))):
            scene.append((t(box(x - 3, y - 4, 5, x + 3, y + 4, 12)), NUT if i < 3 else RED))
    # male pigtail out of the right notch into the devkit's USB socket
    p0 = np.array([sx + 40, sy + 50, 10.0])
    p3 = np.array([sx + 55, sy + 35, 8.0])
    scene.append((bezier_tube(p0, p0 + np.array([8, 0, 2]), p3 + np.array([-8, 0, 1]), p3, r=1.9), PIGTAIL))
    if board:
        # devkit on the rails (same shapes as render_rig.esp32_node, shifted)
        d = lambda g: translate(g, sx + 26, sy + 5, 0)
        scene.append((d(box(30, 16, 3.2, 83, 44, 4.8)), rr.PCB_DARK))
        scene.append((d(box(62, 21, 4.8, 77, 39, 7.9)), rr.SHIELD))
        scene.append((d(box(77, 21, 4.8, 83, 39, 6.2)), rr.PCB_DARK))
        scene.append((d(box(31, 26.5, 4.8, 38, 33.5, 7.6)), rr.SHIELD))
        for cx in (58, 102):
            scene.append((t(box(cx - 2.5, 13, 3.2, cx + 2.5, 17, 9.2)), rr.TIE))
            scene.append((t(box(cx - 2.5, 53, 3.2, cx + 2.5, 57, 9.2)), rr.TIE))
            scene.append((t(box(cx - 2.5, 13, 9.2, cx + 2.5, 57, 10.4)), rr.TIE))
    # hub cable out of the nearest channel notch into the socket
    notch_x = min((x0 + k for x0, ln in CHANNEL for k in range(50, ln, 50)),
                  key=lambda nx: abs(nx - (sx + 21)))
    n0 = np.array([notch_x, CH_Y + 34, 20.0])
    n3 = np.array([sx + 21, sy + 1, 7.5])
    scene.append((bezier_tube(n0, n0 + np.array([0, 6, 8]), n3 + np.array([0, -8, 4]), n3), rr.CABLE))


def build(lids=True, pocket_lids=True):
    PLACED.clear()
    scene = []
    scene.append((box(0, 0, -18, 1200, 200, 0), rr.WOOD))
    control_bay(scene, lids=lids)
    bay_contents(scene)
    cable_channel(scene, lids=lids)
    for sx in STATION_X:
        station(scene, sx, STATION_Y, pocket_lid=pocket_lids)
    return scene


def print_set():
    parts = [
        ("bay-wall-long", 0, 0, 0), ("bay-wall-short-outer", 0, 25, 0),
        ("bay-lid", 0, 50, 0), ("relay-tray", 170, 60, 0),
        ("psu-cradle", 170, 140, 0), ("cable-channel-200", 350, 40, 0),
        ("cable-channel-lid-200", 350, 90, 0), ("board-station", 350, 140, 0),
        ("station-pocket-lid", 500, 150, 0), ("bay-corner-post", 500, 40, 0),
        ("bay-splice-post", 530, 40, 0),
    ]
    scene = [(box(-30, -30, -10, 560, 260, 0), (0.88, 0.88, 0.90))]
    for name, x, y, k in parts:
        place(scene, P[name], LID if "lid" in name else CHASSIS, x, y, 5 if "lid" in name else 0, k)
    return scene


def main():
    scene = build()
    view = View((150, -1250, 780), (600, 90, -60), fovy=33)
    save("chassis-overview.png", render(scene, view), view,
         notes=[
             ("Control bay: Pi 5, hub, relay tray, 12 V brick\nunder two vented lid panels",
              np.array([150, 100, 47]), 60, 120),
             ("Cable channel along the front edge:\n8 USB cables + 16 red VBUS wires, lidded",
              np.array([600, 33, 26]), 560, 700),
             ("Board station: USB-C socket faces the channel,\nwire nuts under the pocket lid, devkit strapped",
              np.array([STATION_X[1] + 21, STATION_Y + 40, 20]), 760, 130),
             ("stations at 145 mm pitch\n(RF spacing: blueprint §6)",
              np.array([STATION_X[4] + 60, STATION_Y + 35, 8]), 1150, 300),
         ],
         title="Alteriom ESP32 HIL rig — enclosed chassis (relay-switched, 8 ports)",
         footer="All printed parts: hardware/mounts/generate_chassis.py + generate_mounts.py → stl/   ·   "
                "wiring: docs/relay-power-wiring.md")

    scene = build(lids=False)
    view = View((0, -560, 440), (178, 105, 0), fovy=30)
    save("chassis-control-bay.png", render(scene, view), view,
         notes=[
             ("bay-wall-long ×4, slotted corner posts,\nscrew flanges inside", np.array([80, 4, 30]), 60, 700),
             ("relay-tray: slotted M3 bosses,\nterminal row toward the brick", np.array([200, 130, 27]), 980, 120),
             ("psu-cradle: 12 V brick strapped", np.array([210, 62, 34]), 1020, 470),
             ("channel enters through the\ninner wall's port", np.array([290, 33, 26]), 1050, 700),
             ("hub → 6 cables into the channel", np.array([70, 40, 30]), 120, 150),
         ],
         title="Control bay, lids off")

    scene = build(lids=True, pocket_lids=False)
    sx = STATION_X[0]
    view = View((sx - 60, -200, 190), (sx + 62, 95, 4), fovy=31)
    save("chassis-station.png", render(scene, view), view,
         notes=[
             ("USB-C female pigtail in the clamp,\nsocket faces the channel", np.array([sx + 21, STATION_Y + 8, 12]), 60, 700),
             ("junction pocket: 3 wire nuts (D+ D− GND)\n+ 2 red joins to the relay", np.array([sx + 22, STATION_Y + 46, 12]), 120, 130),
             ("male pigtail into the devkit", np.array([sx + 52, STATION_Y + 36, 9]), 900, 150),
             ("channel lid; cable exits\nthrough a notch", np.array([sx + 30, CH_Y + 30, 26]), 950, 720),
         ],
         title="Board station (pocket lid off) — board-station.stl")

    scene = print_set()
    view = View((250, -420, 380), (265, 110, -5), fovy=34)
    save("chassis-printset.png", render(scene, view), view,
         notes=[
             ("bay-wall-long / -short-outer", np.array([70, 12, 20]), 60, 800),
             ("bay-lid (vented)", np.array([75, 150, 8]), 60, 250),
             ("relay-tray", np.array([250, 95, 8]), 520, 800),
             ("psu-cradle", np.array([230, 170, 8]), 400, 150),
             ("cable-channel-200 + lid", np.array([450, 57, 24]), 1000, 800),
             ("board-station + pocket lid", np.array([415, 175, 8]), 1000, 200),
             ("corner / splice posts", np.array([515, 46, 45]), 1300, 480),
         ],
         title="Chassis print set (hardware/mounts/stl)")


if __name__ == "__main__":
    main()
