#!/usr/bin/env python3
"""Render the compact rig: boards upright in a card rack behind the deck.

Same renderer and conventions as render_chassis.py (which it imports for
the shared parts and the Pi/hub models). Not part of CI:

    python3 render_compact.py        # writes compact-*.png

Assembly frame (mm, Z up, base top = 0): the control deck is 330 x 190
at the front (posts in the corners), the two 4-slot rack modules sit
behind its rear wall at y 192, boards stand at a 40 mm pitch with the
antenna up and the USB socket down over the junction pocket. Footprint
330 x 272 mm.
"""

import numpy as np

import render_chassis as rc
import render_rig as rr
from render_rig import View, bezier_tube, box, load_stl, render, save, translate

for name in ("deck-wall-long", "deck-wall-rear", "deck-wall-end", "deck-lid",
             "rack-4slot", "rack-pocket-lid", "psu-cradle-side"):
    rc.P[name] = load_stl(rc.STL / f"{name}.stl")

place, P, PLACED = rc.place, rc.P, rc.PLACED
CHASSIS, LID, PIGTAIL, NUT, RED = rc.CHASSIS, rc.LID, rc.PIGTAIL, rc.NUT, rc.RED

DECK_W, DECK_D, DECK_H = 330, 190, 50
RACK_Y = 192
SLOTS = [6 + 20 + 40 * i for i in range(4)] + [165 + 20 + 40 * i for i in range(4)]
BOARD_LIFT, T = 55.0, 3.2
FIN_Y = RACK_Y + 66            # fin front face
BASE = (0, 0, -12, 330, 272, 0)


def deck(scene, lids=True):
    place(scene, "bay-corner-post", CHASSIS, 0, 0, 0, 0)
    place(scene, "bay-corner-post", CHASSIS, DECK_W, 0, 0, 1)
    place(scene, "bay-corner-post", CHASSIS, DECK_W, DECK_D, 0, 2)
    place(scene, "bay-corner-post", CHASSIS, 0, DECK_D, 0, 3)
    place(scene, "bay-splice-post", CHASSIS, 159, 0, 0, 0)
    place(scene, "bay-splice-post", CHASSIS, 171, DECK_D, 0, 2)
    for x0 in (6, 165):
        place(scene, "deck-wall-long", CHASSIS, x0, 2.4, 0, 0)
        place(scene, "deck-wall-rear", CHASSIS, x0 + 159, DECK_D - 2.4, 0, 2)
    place(scene, "deck-wall-end", CHASSIS, 2.4, DECK_D - 6, 0, 3)
    place(scene, "deck-wall-end", CHASSIS, DECK_W - 2.4, 6, 0, 1)
    if lids:
        place(scene, "deck-lid", LID, 0, 0, DECK_H, 0)
        place(scene, "deck-lid", LID, DECK_W, DECK_D, DECK_H, 2)


def deck_contents(scene):
    # relay module on its tray, terminals toward the front wall
    place(scene, "relay-tray", CHASSIS, 16, 16)
    t = lambda g: translate(g, 16, 16, 0)
    scene.append((t(box(10, 9.5, 9.2, 148, 59.5, 10.8)), rc.RELAY_PCB))
    for i in range(8):
        x = 14 + i * 16.5
        scene.append((t(box(x, 34, 10.8, x + 15, 53, 26.3)), rc.RELAY_BLUE))
        scene.append((t(box(x, 11, 10.8, x + 15, 19, 19)), rc.RELAY_BLUE))
    scene.append((t(box(48, 2, 10.8, 112, 9.5, 19)), rc.RELAY_BLUE))
    # hub, rotated so its ports face the rack
    tmp = []
    ports, uplink = rr.hub_node(tmp, 0, 0)
    for tris, color in tmp:
        scene.append((translate(rc.rotz(tris, 2), 300, 86, 0), color))
    ports = [np.array([300 - p[0], 86 - p[1], p[2]]) for p in ports]
    uplink = np.array([300 - uplink[0], 86 - uplink[1], uplink[2]])
    # Pi, USB stack toward the right
    pi_usb = rr.pi_node(scene, 16, 90)
    scene.append((bezier_tube(pi_usb, pi_usb + np.array([40, 0, 10]), uplink + np.array([0, -30, 10]), uplink, r=2.2), rr.UPLINK))
    # brick on its edge
    place(scene, "psu-cradle-side", CHASSIS, 190, 100)
    scene.append((translate(box(8, 6, 3.2, 108, 36, 48), 190, 100, 0), rc.BRICK))
    for cx in (34.8, 81.2):
        scene.append((translate(box(cx - 2.5, 2, 3.2, cx + 2.5, 40, 49.4), 190, 100, 0), rr.TIE))
    # GPIO ribbon: Pi header -> relay IN block (around the tray's left end)
    p0, p3 = np.array([40, 155, 19.0]), np.array([90, 20, 19.0])
    scene.append((bezier_tube(p0, p0 + np.array([-45, -20, 12]), p3 + np.array([-70, 20, 12]), p3, r=2.6), (0.35, 0.35, 0.38)))
    return ports


def rack(scene, ports, boards=True, pocket_lids=True):
    place(scene, "rack-4slot", CHASSIS, 6, RACK_Y)
    place(scene, "rack-4slot", CHASSIS, 165, RACK_Y)
    for i, cx in enumerate(SLOTS):
        y0 = RACK_Y
        # female pigtail in its clamp, mouth toward the deck wall
        scene.append((box(cx - 9.5, y0 + 4, T, cx + 9.5, y0 + 20, T + 8), PIGTAIL))
        scene.append((box(cx - 6, y0 + 1, T + 2, cx + 6, y0 + 4, T + 6), (0.03,) * 3))
        if pocket_lids:
            place(scene, "rack-pocket-lid", LID, cx - 17, y0 + 24, T + 16)
        else:
            for j, (dx, dy) in enumerate(((-9, 36), (0, 36), (9, 36), (-5, 50), (5, 50))):
                scene.append((box(cx + dx - 3, y0 + dy - 4, 5, cx + dx + 3, y0 + dy + 4, 12), NUT if j < 3 else RED))
        # male pigtail: out of the pocket's rear notch, up into the board
        p0 = np.array([cx, y0 + 63, 12.0])
        p3 = np.array([cx, FIN_Y - 7, T + BOARD_LIFT - 2])
        scene.append((bezier_tube(p0, p0 + np.array([0, 2, 20]), p3 + np.array([0, -6, -22]), p3, r=1.9), PIGTAIL))
        if boards:
            zb = T + BOARD_LIFT
            scene.append((box(cx - 13, FIN_Y - 4.6, zb, cx + 13, FIN_Y - 3, zb + 55), rr.PCB_DARK))     # PCB
            scene.append((box(cx - 9, FIN_Y - 7.6, zb + 30, cx + 9, FIN_Y - 4.6, zb + 45), rr.SHIELD))  # module can
            scene.append((box(cx - 9, FIN_Y - 6, zb + 45, cx + 9, FIN_Y - 4.6, zb + 55), rr.PCB_DARK))  # antenna
            scene.append((box(cx - 3.5, FIN_Y - 7.4, zb, cx + 3.5, FIN_Y - 4.6, zb + 7), rr.SHIELD))    # USB socket
            for zt in (zb + 12, zb + 42):                                                                # straps
                scene.append((box(cx - 13.5, FIN_Y - 9, zt, cx + 13.5, FIN_Y + 5.5, zt + 5), rr.TIE))
        # hub cable: through the rear-wall notch into the socket
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


def main():
    scene = build()
    view = View((-260, -620, 520), (165, 140, 10), fovy=31)
    save("compact-overview.png", render(scene, view), view,
         notes=[
             ("Control deck 330 × 190: Pi 5, hub, relay tray,\n12 V brick on its edge, two vented lids",
              np.array([120, 90, 52]), 60, 120),
             ("Card rack: 8 boards upright at 40 mm pitch,\nantenna up, USB down onto the junction pocket",
              np.array([SLOTS[5], FIN_Y - 6, 95]), 900, 120),
             ("one notch per slot in the rear wall:\nhub cable + 2 red wires, nothing exposed",
              np.array([SLOTS[2], DECK_D, 12]), 1050, 640),
         ],
         title="Alteriom ESP32 HIL rig — compact chassis (330 × 272 mm, 8 ports)",
         footer="hardware/mounts/generate_chassis.py (compact set) · wiring: docs/relay-power-wiring.md\n"
                "RF: 40 mm pitch is far below blueprint §6; reduce TX power in the agent firmware (§7)")

    scene = build(lids=False, pocket_lids=False)
    view = View((60, -520, 470), (165, 120, 0), fovy=30)
    save("compact-deck.png", render(scene, view), view,
         notes=[
             ("relay-tray, terminals toward the front wall", np.array([95, 20, 20]), 60, 700),
             ("hub ports face the rack", np.array([245, 70, 22]), 1000, 720),
             ("psu-cradle-side: brick standing on\nits edge, 48 mm tall under a 50 mm wall", np.array([250, 120, 48]), 1000, 120),
             ("pi5-tile", np.array([60, 130, 20]), 100, 160),
         ],
         title="Deck lids off, pockets open")

    scene = build(lids=True, pocket_lids=True)
    view = View((-120, 40, 250), (95, 240, 50), fovy=33)
    save("compact-rack.png", render(scene, view), view,
         notes=[
             ("fin with a 12 mm rib between the header rows;\ntwo ties around board + fin", np.array([SLOTS[1], FIN_Y - 6, 100]), 60, 120),
             ("USB-C female socket faces the deck wall", np.array([SLOTS[0], RACK_Y + 3, 8]), 60, 720),
             ("male pigtail up into the board", np.array([SLOTS[2], FIN_Y - 10, 45]), 900, 720),
             ("pocket lid with the pigtail notch", np.array([SLOTS[3], RACK_Y + 44, 20]), 1000, 200),
         ],
         title="Card rack — rack-4slot.stl")

    scene = print_set()
    view = View((250, -440, 400), (255, 110, -5), fovy=34)
    save("compact-printset.png", render(scene, view), view,
         notes=[
             ("deck-wall-long / -rear / -end", np.array([80, 20, 20]), 60, 800),
             ("deck-lid", np.array([80, 160, 8]), 60, 200),
             ("rack-4slot (prints upright)", np.array([280, 210, 100]), 900, 130),
             ("psu-cradle-side", np.array([255, 60, 10]), 600, 800),
             ("relay-tray", np.array([420, 75, 8]), 1050, 800),
             ("pocket lid, posts", np.array([440, 170, 20]), 1350, 420),
         ],
         title="Compact print set (hardware/mounts/stl)")


if __name__ == "__main__":
    main()
