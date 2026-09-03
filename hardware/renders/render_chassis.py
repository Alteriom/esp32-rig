#!/usr/bin/env python3
"""Render the rig chassis: control deck + upright board rack.

Reuses render_rig.py's rasterizer and its Pi/hub models; every printed
part is the real STL from ../mounts/stl/ placed at its assembly position,
so the pictures show exactly what generate_chassis.py + generate_mounts.py
produce. Not part of CI. Needs numpy + matplotlib:

    python3 render_chassis.py        # writes chassis-*.png

Assembly frame (mm, Z up, base top = 0): the control deck is 330 x 190 at
the front (posts in the corners, walls 2.4 mm in from the edges), the two
4-slot rack modules sit behind its rear wall, boards stand at a 40 mm
pitch with the antenna up and the USB socket down over the junction
pocket. Footprint 330 x 272 mm. build_viewer.py reads PLACED after
build() to emit the interactive assembly.
"""

import sys
from pathlib import Path

import numpy as np

import render_rig as rr
from render_rig import View, bezier_tube, box, load_stl, render, save, translate

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mounts"))
import generate_chassis as gc  # noqa: E402  (dimensions come from the generator)

STL = rr.STL


def rotz(tris, k):
    """Rotate about Z by k * 90 degrees (winding preserved)."""
    out = tris.copy()
    for _ in range(k % 4):
        x, y = out[..., 0].copy(), out[..., 1].copy()
        out[..., 0], out[..., 1] = -y, x
    return out


# ---------------------------------------------------------------- palette

CHASSIS = (0.20, 0.21, 0.24)     # charcoal PETG: posts, walls, trays, rack
LID = (0.88, 0.47, 0.13)         # orange PETG accents: lids + pocket lids
RELAY_PCB = (0.62, 0.13, 0.17)
RELAY_BLUE = (0.14, 0.33, 0.72)
BRICK = (0.09, 0.09, 0.10)
NUT = (0.90, 0.45, 0.12)
RED = (0.80, 0.12, 0.10)
PIGTAIL = (0.10, 0.10, 0.11)

P = {name: load_stl(STL / f"{name}.stl") for name in (
    "bay-corner-post", "bay-splice-post", "deck-wall-long", "deck-wall-rear",
    "deck-wall-end", "deck-lid", "relay-tray", "psu-cradle-side",
    "rack-4slot", "rack-pocket-lid",
)}

PLACED = []  # (name, dx, dy, dz, k) of every STL placed by build(): the viewer reads this


def place(scene, name, color, dx, dy, dz=0, k=0):
    PLACED.append((name, dx, dy, dz, k))
    scene.append((translate(rotz(P[name], k), dx, dy, dz), color))


DECK_W, DECK_D, DECK_H = gc.DECK_W, gc.DECK_D, gc.DECK_H
RACK_Y = DECK_D + gc.RACK_GAP
SLOTS = gc.slot_centres()
T, BOARD_LIFT = gc.T, gc.BOARD_LIFT
FIN_Y = RACK_Y + gc.RACK_D - 14            # fin front face
BASE = (0, 0, -12, gc.BASE_W, gc.BASE_D, 0)
SEG = (DECK_W - 3 * gc.POST + 4 * gc.SLOT_DEPTH) / 2     # 159


# --------------------------------------------------------------- assembly


def deck(scene, lids=True):
    place(scene, "bay-corner-post", CHASSIS, 0, 0, 0, 0)
    place(scene, "bay-corner-post", CHASSIS, DECK_W, 0, 0, 1)
    place(scene, "bay-corner-post", CHASSIS, DECK_W, DECK_D, 0, 2)
    place(scene, "bay-corner-post", CHASSIS, 0, DECK_D, 0, 3)
    place(scene, "bay-splice-post", CHASSIS, SEG, 0, 0, 0)
    place(scene, "bay-splice-post", CHASSIS, SEG + gc.POST, DECK_D, 0, 2)
    for x0 in (gc.SLOT_DEPTH, gc.SLOT_DEPTH + SEG):
        place(scene, "deck-wall-long", CHASSIS, x0, gc.INSET, 0, 0)
        place(scene, "deck-wall-rear", CHASSIS, x0 + SEG, DECK_D - gc.INSET, 0, 2)
    place(scene, "deck-wall-end", CHASSIS, gc.INSET, DECK_D - gc.SLOT_DEPTH, 0, 3)
    place(scene, "deck-wall-end", CHASSIS, DECK_W - gc.INSET, gc.SLOT_DEPTH, 0, 1)
    if lids:
        place(scene, "deck-lid", LID, 0, 0, DECK_H, 0)
        place(scene, "deck-lid", LID, DECK_W, DECK_D, DECK_H, 2)


# deck interior placements (base coordinates of each tile's origin)
RELAY_AT = (16, 16)
HUB_AT = (190, 16)        # tile origin; the hub is rotated so its ports face the rack
PI_AT = (16, 90)
PSU_AT = (190, 100)


def deck_contents(scene):
    place(scene, "relay-tray", CHASSIS, *RELAY_AT)
    t = lambda g: translate(g, RELAY_AT[0], RELAY_AT[1], 0)
    scene.append((t(box(10, 9.5, 9.2, 148, 59.5, 10.8)), RELAY_PCB))
    for i in range(8):
        x = 14 + i * 16.5
        scene.append((t(box(x, 34, 10.8, x + 15, 53, 26.3)), RELAY_BLUE))   # relay
        scene.append((t(box(x, 11, 10.8, x + 15, 19, 19)), RELAY_BLUE))     # NC/COM/NO
    scene.append((t(box(48, 2, 10.8, 112, 9.5, 19)), RELAY_BLUE))          # DC/IN block
    tmp = []
    ports, uplink = rr.hub_node(tmp, 0, 0)
    hx, hy = HUB_AT[0] + 110, HUB_AT[1] + 70
    for tris, color in tmp:
        scene.append((translate(rotz(tris, 2), hx, hy, 0), color))
    ports = [np.array([hx - p[0], hy - p[1], p[2]]) for p in ports]
    uplink = np.array([hx - uplink[0], hy - uplink[1], uplink[2]])
    pi_usb = rr.pi_node(scene, *PI_AT)
    scene.append((bezier_tube(pi_usb, pi_usb + np.array([40, 0, 10]), uplink + np.array([0, -30, 10]), uplink, r=2.2), rr.UPLINK))
    place(scene, "psu-cradle-side", CHASSIS, *PSU_AT)
    bl, bw, bt = gc.BRICK
    scene.append((translate(box(8, 6, T, 8 + bl, 6 + bt, T + bw), PSU_AT[0], PSU_AT[1], 0), BRICK))
    for cx in ((bl + 16) * 0.3, (bl + 16) * 0.7):
        scene.append((translate(box(cx - 2.5, 2, T, cx + 2.5, bt + 10, T + bw + 1.4), PSU_AT[0], PSU_AT[1], 0), rr.TIE))
    p0, p3 = np.array([40, 155, 19.0]), np.array([90, 20, 19.0])
    scene.append((bezier_tube(p0, p0 + np.array([-45, -20, 12]), p3 + np.array([-70, 20, 12]), p3, r=2.6), (0.35, 0.35, 0.38)))
    return ports


def rack(scene, ports, boards=True, pocket_lids=True):
    for x0 in gc.RACK_X:
        place(scene, "rack-4slot", CHASSIS, x0, RACK_Y)
    pw, pd = gc.POCKET
    for i, cx in enumerate(SLOTS):
        y0 = RACK_Y
        scene.append((box(cx - 9.5, y0 + 4, T, cx + 9.5, y0 + 20, T + 8), PIGTAIL))          # female pigtail
        scene.append((box(cx - 6, y0 + 1, T + 2, cx + 6, y0 + 4, T + 6), (0.03,) * 3))        # socket mouth
        if pocket_lids:
            place(scene, "rack-pocket-lid", LID, cx - pw / 2, y0 + 24, T + gc.POCKET_H)
        else:
            for j, (dx, dy) in enumerate(((-9, 36), (0, 36), (9, 36), (-5, 50), (5, 50))):
                scene.append((box(cx + dx - 3, y0 + dy - 4, 5, cx + dx + 3, y0 + dy + 4, 12), NUT if j < 3 else RED))
        p0 = np.array([cx, y0 + 24 + pd - 1, 12.0])
        p3 = np.array([cx, FIN_Y - 7, T + BOARD_LIFT - 2])
        scene.append((bezier_tube(p0, p0 + np.array([0, 2, 20]), p3 + np.array([0, -6, -22]), p3, r=1.9), PIGTAIL))
        if boards:
            zb = T + BOARD_LIFT
            scene.append((box(cx - 13, FIN_Y - 4.6, zb, cx + 13, FIN_Y - 3, zb + 55), rr.PCB_DARK))
            scene.append((box(cx - 9, FIN_Y - 7.6, zb + 30, cx + 9, FIN_Y - 4.6, zb + 45), rr.SHIELD))
            scene.append((box(cx - 9, FIN_Y - 6, zb + 45, cx + 9, FIN_Y - 4.6, zb + 55), rr.PCB_DARK))
            scene.append((box(cx - 3.5, FIN_Y - 7.4, zb, cx + 3.5, FIN_Y - 4.6, zb + 7), rr.SHIELD))
            for zt in (zb + 12, zb + 42):
                scene.append((box(cx - 13.5, FIN_Y - 9, zt, cx + 13.5, FIN_Y + gc.FIN_T + 1.5, zt + 5), rr.TIE))
        p = ports[i % len(ports)]
        q3 = np.array([cx, y0 + 0.5, T + 4])
        scene.append((bezier_tube(p, p + np.array([0, 40, 4]), q3 + np.array([0, -40, 8]), q3), rr.CABLE))


def build(lids=True, pocket_lids=True):
    PLACED.clear()
    scene = [(box(*BASE), rr.WOOD)]
    deck(scene, lids=lids)
    ports = deck_contents(scene)
    rack(scene, ports, pocket_lids=pocket_lids)
    return scene


def print_set():
    parts = [("deck-wall-long", 0, 0), ("deck-wall-rear", 0, 22), ("deck-wall-end", 0, 44),
             ("deck-lid", 0, 70), ("rack-4slot", 200, 150), ("rack-pocket-lid", 400, 150),
             ("psu-cradle-side", 200, 40), ("relay-tray", 340, 40),
             ("bay-corner-post", 450, 150), ("bay-splice-post", 480, 150)]
    scene = [(box(-30, -30, -10, 540, 270, 0), (0.88, 0.88, 0.90))]
    for name, x, y in parts:
        place(scene, name, LID if "lid" in name else CHASSIS, x, y, 5 if "lid" in name else 0)
    return scene


VIEWS = {
    "overview": {"eye": [-260, -620, 520], "target": [165, 140, 10]},
    "deck": {"eye": [60, -520, 470], "target": [165, 120, 0]},
    "rack": {"eye": [-120, 40, 250], "target": [95, 240, 50]},
}


def main():
    scene = build()
    v = VIEWS["overview"]
    view = View(v["eye"], v["target"], fovy=31)
    save("chassis-overview.png", render(scene, view), view,
         notes=[
             ("Control deck 330 × 190: Pi 5, hub, relay tray,\n12 V brick on its edge, two vented lids",
              np.array([120, 90, 52]), 60, 120),
             ("Card rack: 8 boards upright at 40 mm pitch,\nantenna up, USB down onto the junction pocket",
              np.array([SLOTS[5], FIN_Y - 6, 95]), 900, 120),
             ("one notch per slot in the rear wall:\nhub cable + 2 red wires, nothing exposed",
              np.array([SLOTS[2], DECK_D, 12]), 1050, 640),
         ],
         title="Alteriom ESP32 HIL rig — chassis (330 × 272 mm, 8 ports)",
         footer="hardware/mounts/generate_chassis.py · wiring: docs/relay-power-wiring.md\n"
                "RF: 40 mm pitch is far below blueprint §6; reduce TX power in the agent firmware (§7)")

    scene = build(lids=False, pocket_lids=False)
    v = VIEWS["deck"]
    view = View(v["eye"], v["target"], fovy=30)
    save("chassis-deck.png", render(scene, view), view,
         notes=[
             ("relay-tray, terminals toward the front wall", np.array([95, 20, 20]), 60, 700),
             ("hub ports face the rack", np.array([245, 70, 22]), 1000, 720),
             ("psu-cradle-side: brick standing on\nits edge, 48 mm tall under a 50 mm wall", np.array([250, 120, 48]), 1000, 120),
             ("pi5-tile", np.array([60, 130, 20]), 100, 160),
         ],
         title="Deck lids off, pockets open")

    scene = build(lids=True, pocket_lids=True)
    v = VIEWS["rack"]
    view = View(v["eye"], v["target"], fovy=33)
    save("chassis-rack.png", render(scene, view), view,
         notes=[
             ("fin with a 12 mm rib between the header rows;\ntwo ties around board + fin", np.array([SLOTS[1], FIN_Y - 6, 100]), 60, 120),
             ("USB-C female socket faces the deck wall", np.array([SLOTS[0], RACK_Y + 3, 8]), 60, 720),
             ("male pigtail up into the board", np.array([SLOTS[2], FIN_Y - 10, 45]), 900, 720),
             ("pocket lid with the pigtail notch", np.array([SLOTS[3], RACK_Y + 44, 20]), 1000, 200),
         ],
         title="Card rack — rack-4slot.stl")

    scene = print_set()
    view = View((250, -440, 400), (255, 110, -5), fovy=34)
    save("chassis-printset.png", render(scene, view), view,
         notes=[
             ("deck-wall-long / -rear / -end", np.array([80, 20, 20]), 60, 800),
             ("deck-lid", np.array([80, 160, 8]), 60, 200),
             ("rack-4slot (prints upright)", np.array([280, 210, 100]), 900, 130),
             ("psu-cradle-side", np.array([255, 60, 10]), 600, 800),
             ("relay-tray", np.array([420, 75, 8]), 1050, 800),
             ("pocket lid, posts", np.array([440, 170, 20]), 1350, 420),
         ],
         title="Chassis print set (hardware/mounts/stl)")


if __name__ == "__main__":
    main()
