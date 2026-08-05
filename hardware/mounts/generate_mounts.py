#!/usr/bin/env python3
"""Generate the rig's printable mounting-plate STLs.

Dependency-free (stdlib only). All geometry is a union of axis-aligned
boxes; holes/slots are made by covering a footprint with boxes *around*
the cutout rectangles (guillotine subtraction), which slicers union into
one solid. Rerun after editing dimensions:

    python3 generate_mounts.py        # writes stl/*.stl

Units: millimetres. Z=0 is the face that sits on the backing plank.

Plates produced (see README.md for print settings and assembly):
  esp32-board-tile.stl  one ESP32 devkit: 29 mm locating channel between
                        rails + 3 zip-tie stations with under-tile grooves
  pi5-tile.stl          Raspberry Pi 4/5: 58x49 hole pattern on 6 mm bosses
  hub-strap-tile.stl    universal zip-strap plate (USB hub, relay boards)
"""

import struct
from pathlib import Path

OUT = Path(__file__).parent / "stl"

# ---------------------------------------------------------------- mesh core


def box(tris, x0, y0, z0, x1, y1, z1):
    """Append the 12 triangles of an axis-aligned box (outward normals)."""
    v = [
        (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
        (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
    ]
    faces = [
        ((0, 0, -1), 0, 2, 1), ((0, 0, -1), 0, 3, 2),
        ((0, 0, 1), 4, 5, 6), ((0, 0, 1), 4, 6, 7),
        ((0, -1, 0), 0, 1, 5), ((0, -1, 0), 0, 5, 4),
        ((1, 0, 0), 1, 2, 6), ((1, 0, 0), 1, 6, 5),
        ((0, 1, 0), 2, 3, 7), ((0, 1, 0), 2, 7, 6),
        ((-1, 0, 0), 3, 0, 4), ((-1, 0, 0), 3, 4, 7),
    ]
    for n, a, b, c in faces:
        tris.append((n, v[a], v[b], v[c]))


def subtract(rects, cut):
    """Subtract one rectangle from a list of rectangles (guillotine split)."""
    cx0, cy0, cx1, cy1 = cut
    out = []
    for x0, y0, x1, y1 in rects:
        if cx0 >= x1 or cx1 <= x0 or cy0 >= y1 or cy1 <= y0:
            out.append((x0, y0, x1, y1))
            continue
        ix0, ix1 = max(x0, cx0), min(x1, cx1)
        if x0 < ix0:
            out.append((x0, y0, ix0, y1))
        if ix1 < x1:
            out.append((ix1, y0, x1, y1))
        if y0 < cy0:
            out.append((ix0, y0, ix1, cy0))
        if cy1 < y1:
            out.append((ix0, cy1, ix1, y1))
    return out


def layer(tris, footprint, cutouts, z0, z1):
    """Emit footprint minus cutouts, extruded from z0 to z1."""
    rects = [footprint]
    for c in cutouts:
        rects = subtract(rects, c)
    for x0, y0, x1, y1 in rects:
        box(tris, x0, y0, z0, x1, y1, z1)


def write_stl(path, tris):
    with open(path, "wb") as f:
        f.write(b"alteriom-esp32-farm mount".ljust(80, b"\0"))
        f.write(struct.pack("<I", len(tris)))
        for n, a, b, c in tris:
            f.write(struct.pack("<12fH", *n, *a, *b, *c, 0))
    print(f"{path.name}: {len(tris)} triangles")


def sq(cx, cy, s):
    """Square cutout centred on (cx, cy)."""
    return (cx - s / 2, cy - s / 2, cx + s / 2, cy + s / 2)


# ------------------------------------------------------------- shared specs

T = 3.2            # base thickness; groove depth is T/2 so a 1.4 mm-thick
GROOVE = T / 2     # zip tie lies flush between tile and plank
TIE_W = 5.5        # slot/groove width — fits standard 4.8 mm cable ties
SLOT_H = 4.0       # slot opening along the tie's run direction
PLANK_HOLE = 4.0   # square hole for a #6 / M3.5 wood screw into the plank


def zip_station(bottom_cuts, top_cuts, cx, y_near, y_far):
    """One tie station: two through-slots joined by an under-tile groove."""
    x0, x1 = cx - TIE_W / 2, cx + TIE_W / 2
    bottom_cuts.append((x0, y_near, x1, y_far))                  # groove
    top_cuts.append((x0, y_near, x1, y_near + SLOT_H))           # slot A
    top_cuts.append((x0, y_far - SLOT_H, x1, y_far))             # slot B


# ---------------------------------------------------------- esp32 board tile


def esp32_board_tile():
    W, D = 80.0, 60.0
    tris = []
    corners = [sq(6, 6, PLANK_HOLE), sq(W - 6, 6, PLANK_HOLE),
               sq(6, D - 6, PLANK_HOLE), sq(W - 6, D - 6, PLANK_HOLE)]
    bottom, top = list(corners), list(corners)
    for cx in (18, 40, 62):
        zip_station(bottom, top, cx, 7, D - 7)
    layer(tris, (0, 0, W, D), bottom, 0, GROOVE)
    layer(tris, (0, 0, W, D), top, GROOVE, T)
    # locating rails: 29 mm channel cradles 25.4–28.5 mm devkits; mount the
    # board with its antenna overhanging a tile edge (RF, blueprint §6)
    box(tris, 8, 12.5, T, W - 8, 15.5, T + 3)
    box(tris, 8, D - 15.5, T, W - 8, D - 12.5, T + 3)
    write_stl(OUT / "esp32-board-tile.stl", tris)


# ----------------------------------------------------------------- pi5 tile


def pi5_tile():
    W, D = 104.0, 82.0
    BOSS, BOSS_H, HOLE = 9.0, 6.0, 2.7   # M2.5 screws self-tap into HOLE
    tris = []
    # Pi 4/5 pattern: 58 x 49, first hole 3.5/3.5 from board corner
    ox, oy = (W - 85) / 2 + 3.5, (D - 56) / 2 + 3.5
    pi_holes = [(ox, oy), (ox + 58, oy), (ox, oy + 49), (ox + 58, oy + 49)]
    cuts = [sq(5, 5, PLANK_HOLE), sq(W - 5, 5, PLANK_HOLE),
            sq(5, D - 5, PLANK_HOLE), sq(W - 5, D - 5, PLANK_HOLE)]
    cuts += [sq(x, y, HOLE) for x, y in pi_holes]
    layer(tris, (0, 0, W, D), cuts, 0, T)
    for x, y in pi_holes:
        bx = sq(x, y, BOSS)
        layer(tris, bx, [sq(x, y, HOLE)], T, T + BOSS_H)
    write_stl(OUT / "pi5-tile.stl", tris)


# ----------------------------------------------------------- hub strap tile


def hub_strap_tile():
    W, D = 110.0, 70.0
    tris = []
    corners = [sq(6, 6, PLANK_HOLE), sq(W - 6, 6, PLANK_HOLE),
               sq(6, D - 6, PLANK_HOLE), sq(W - 6, D - 6, PLANK_HOLE)]
    bottom, top = list(corners), list(corners)
    for cx in (20, 55, 90):
        zip_station(bottom, top, cx, 6, D - 6)
    layer(tris, (0, 0, W, D), bottom, 0, GROOVE)
    layer(tris, (0, 0, W, D), top, GROOVE, T)
    write_stl(OUT / "hub-strap-tile.stl", tris)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    esp32_board_tile()
    pi5_tile()
    hub_strap_tile()
