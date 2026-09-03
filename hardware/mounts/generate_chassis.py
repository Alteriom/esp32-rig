#!/usr/bin/env python3
"""Generate the rig chassis STLs: control deck + upright board rack.

Same rules as generate_mounts.py (stdlib only, axis-aligned boxes, Z=0 on
the base, millimetres) — this file reuses its primitives and adds the
parts that turn the relay-switched build described in
docs/relay-power-wiring.md into an enclosed, wire-managed rig of
330 x 272 mm:

    python3 generate_chassis.py       # writes stl/*.stl (chassis set)

Layout (see README.md "Chassis" and renders/chassis-dimensions.png):

  control deck, 330 x 190 footprint, 50 mm walls, at the front
    bay-corner-post.stl        x4  slotted post, walls slide in
    bay-splice-post.stl        x2  in-line post joining two long segments
    deck-wall-long.stl         x2  159 mm front segment, screw flange
    deck-wall-rear.stl         x2  159 mm rear segment, one cable notch
                                   per board slot
    deck-wall-end.stl          x2  178 mm end wall with two cable entries
    deck-lid.stl               x2  165 x 190 vented half, drop-in rails
    relay-tray.stl             x1  8-ch relay module: slotted M3 bosses
    psu-cradle-side.stl        x1  12 V brick standing on its long edge
    (pi5-tile.stl and hub-strap-tile.stl come from generate_mounts.py)
  card rack, 2 x 160 x 80 behind the deck's rear wall
    rack-4slot.stl             x2  four upright board slots at 40 mm
                                   pitch: fin + strap notches + junction
                                   pocket + USB-C socket clamp
    rack-pocket-lid.stl        x8  pocket lid with the male-pigtail notch

Dimensions marked MEASURE are defaults for the parts pictured in the
wiring doc; check them against what you actually received before
printing (slots and clearances absorb a few millimetres, not more).
"""

from pathlib import Path

from generate_mounts import (
    GROOVE,
    PLANK_HOLE,
    SLOT_H,
    T,
    TIE_W,
    box,
    layer,
    sq,
    write_stl,
    zip_station,
)

OUT = Path(__file__).parent / "stl"

# ----------------------------------------------------------- shared specs

WALL = 2.4          # wall / lid thickness (6 perimeters at 0.4 mm)
CLR = 0.3           # sliding clearance per side (post slots, lid rails)
INSET = WALL        # walls sit one wall-thickness in from the base edge
FLANGE = 10.0       # screw flange along the inside foot of every wall
POST = 12.0         # corner / splice post side
SLOT_DEPTH = POST / 2   # how far a wall end slides into a post
SOCKET_W = 20.0     # MEASURE: USB-C female pigtail overmold width + 2
SOCKET_H = 9.0      # MEASURE: overmold height; clamp ribs stand this tall
RELAY_HOLES = (130.0, 43.0)   # MEASURE: 8-ch module hole pattern (x, y)
BRICK = (100.0, 45.0, 30.0)   # MEASURE: 12 V brick length, width, thickness

# control deck
DECK_W, DECK_D = 330.0, 190.0
DECK_H = 50.0       # clears Pi USB stack (27), hub (30), brick on its edge (48)
# card rack
RACK_W, RACK_D = 160.0, 80.0
SLOT_PITCH = 40.0   # board pitch; 8 boards = 2 rack modules of 4
SLOT_X0 = 20.0      # first slot centre from the module's origin
FIN_T = 4.0
FIN_W = 30.0
BOARD_LIFT = 55.0   # board's bottom edge above the base: room for the male
                    # pigtail's plug (~30 mm) above the pocket lid
BOARD_H = 55.0      # longest common devkit; the fin clears it by 5
POCKET = (34.0, 40.0)   # junction pocket footprint (x, y), 2 mm walls
POCKET_H = 16.0
RACK_GAP = 2.0      # rack modules sit this far behind the deck's rear edge
RACK_X = (6.0, 6.0 + RACK_W)   # rack module origins along X: 6..166, 166..326
BASE_W = DECK_W
BASE_D = DECK_D + RACK_GAP + RACK_D          # 272
FIN_TOP = T + BOARD_LIFT + BOARD_H + 5       # 118.2 above the base


def slot_centres():
    """Board slot centres in base coordinates."""
    return [x0 + SLOT_X0 + SLOT_PITCH * i for x0 in RACK_X for i in range(4)]


def zip_station_x(bottom_cuts, top_cuts, cy, x_near, x_far):
    """Tie station running along X (generate_mounts' runs along Y)."""
    y0, y1 = cy - TIE_W / 2, cy + TIE_W / 2
    bottom_cuts.append((x_near, y0, x_far, y1))
    top_cuts.append((x_near, y0, x_near + SLOT_H, y1))
    top_cuts.append((x_far - SLOT_H, y0, x_far, y1))


def plank_corners(W, D, inset=6.0):
    return [sq(inset, inset, PLANK_HOLE), sq(W - inset, inset, PLANK_HOLE),
            sq(inset, D - inset, PLANK_HOLE), sq(W - inset, D - inset, PLANK_HOLE)]


# --------------------------------------------------------------- posts


def bay_corner_post():
    """12 mm post at a deck corner; the two walls slide into 6 mm-deep
    slots. Oriented for the front-left corner: wall along +X leaves
    through the +X face, wall along +Y through the +Y face. Rotate for the
    others. A 10 mm foot inside the corner takes one base screw."""
    tris = []
    slot_w = WALL + 2 * CLR
    slot_x = (POST / 2, INSET, POST, INSET + slot_w)
    slot_y = (INSET, POST / 2, INSET + slot_w, POST)
    layer(tris, (0, 0, POST, POST), [slot_x, slot_y], 0, DECK_H)
    foot = (POST, POST, POST + 10, POST + 10)
    layer(tris, foot, [sq(POST + 5, POST + 5, PLANK_HOLE)], 0, T)
    write_stl(OUT / "bay-corner-post.stl", tris)


def bay_splice_post():
    """In-line post: a through slot along X joins two wall segments."""
    tris = []
    slot_w = WALL + 2 * CLR
    slot = (0, INSET, POST, INSET + slot_w)
    layer(tris, (0, 0, POST, POST), [slot], 0, DECK_H)
    foot = (0, POST, POST, POST + 10)
    layer(tris, foot, [sq(POST / 2, POST + 5, PLANK_HOLE)], 0, T)
    write_stl(OUT / "bay-splice-post.stl", tris)


# --------------------------------------------------------------- walls


def wall(name, length, notches=()):
    """Wall segment along X: plate at y 0..WALL, flange inward to y FLANGE.

    `notches` are (x0, x1, height) openings cut up from the base surface
    through both the wall and its flange (cable entries, slot notches).
    Print lying on the outer face; the flange points up, no supports.
    """
    tris = []
    # flange stops short of both ends: the wall's last SLOT_DEPTH sits in a
    # post and the post's foot occupies the next 10 mm of the inside corner
    fx0, fx1 = SLOT_DEPTH + 11, length - SLOT_DEPTH - 11
    holes = [sq(x, WALL + FLANGE / 2, PLANK_HOLE)
             for x in range(int(fx0) + 12, int(fx1) - 8, 50)]
    notch_cuts = [(x0, 0, x1, FLANGE + WALL) for x0, x1, _h in notches]
    layer(tris, (0, 0, length, WALL), notch_cuts, 0, T)
    layer(tris, (fx0, WALL, fx1, FLANGE + WALL), holes + notch_cuts, 0, T)
    z = T
    for h in sorted({n[2] for n in notches}):
        cuts = [(x0, 0, x1, WALL) for x0, x1, nh in notches if nh > z]
        layer(tris, (0, 0, length, WALL), cuts, z, min(h, DECK_H))
        z = min(h, DECK_H)
    if z < DECK_H:
        layer(tris, (0, 0, length, WALL), [], z, DECK_H)
    write_stl(OUT / name, tris)


def deck_walls():
    seg = (DECK_W - 3 * POST + 4 * SLOT_DEPTH) / 2                 # 159
    end = DECK_D - 2 * POST + 2 * SLOT_DEPTH                        # 178
    wall("deck-wall-long.stl", seg)
    # rear: one notch per board slot. The two rear segments are one print,
    # placed rotated 180 deg with their origins at base x = SLOT_DEPTH + seg
    # and SLOT_DEPTH + 2 * seg (see render_chassis.deck), so wall coordinate
    # w maps to base x = origin - w. Each notch spans both segments' slot
    # positions plus 10 mm either side.
    slots = slot_centres()
    rear = []
    for i in range(4):
        ws = [SLOT_DEPTH + seg - slots[i], SLOT_DEPTH + 2 * seg - slots[4 + i]]
        rear.append((min(ws) - 10, max(ws) + 10, 20))
    wall("deck-wall-rear.stl", seg, notches=rear)
    # end walls: Ethernet / Pi USB-C / 12 V barrel / hub PSU
    wall("deck-wall-end.stl", end, notches=[(40, 72, 22), (104, 136, 22)])


def deck_lid():
    """Half lid (165 x 190). Rails underneath drop inside the walls and stop
    clear of the posts; the two halves butt at the deck's midline."""
    W, D = DECK_W / 2, DECK_D
    tris = []
    vents = []
    for row in range(int((D - 60) // 32)):
        y = 40 + row * 32
        for col in range(int((W - 30) // 44)):
            x = 18 + col * 44
            vents.append((x, y, x + 36, y + 3))
            vents.append((x, y + 9, x + 36, y + 12))
    layer(tris, (0, 0, W, D), vents, 0, WALL)
    inner = INSET + WALL + CLR
    rail_h, g = 5.0, POST + CLR
    box(tris, g, inner, -rail_h, W - g, inner + WALL, 0)              # front
    box(tris, g, D - inner - WALL, -rail_h, W - g, D - inner, 0)      # rear
    box(tris, inner, g, -rail_h, inner + WALL, D - g, 0)              # outer end
    write_stl(OUT / "deck-lid.stl", tris)                            # print rails-up


# --------------------------------------------------------------- trays


def relay_tray():
    """Tray for the 8-channel relay module: four slotted M3 bosses (±3 mm
    in X absorbs hole-pattern variance)."""
    hx, hy = RELAY_HOLES
    W, D = hx + 28, hy + 26
    tris = []
    ox, oy = (W - hx) / 2, (D - hy) / 2
    holes = [(ox, oy), (ox + hx, oy), (ox, oy + hy), (ox + hx, oy + hy)]
    slots = [(x - 4.35, y - 1.35, x + 4.35, y + 1.35) for x, y in holes]
    layer(tris, (0, 0, W, D), plank_corners(W, D, 5) + slots, 0, T)
    for (x, y), s in zip(holes, slots):
        layer(tris, (x - 7.5, y - 4.5, x + 7.5, y + 4.5), [s], T, T + 6)
    write_stl(OUT / "relay-tray.stl", tris)


def psu_cradle_side():
    """12 V brick standing on its long edge: rails locate it, end stops and
    two straps hold it. Footprint BRICK length x thickness."""
    bx, by = BRICK[0], BRICK[2]
    W, D = bx + 16, by + 12
    tris = []
    bottom, top = plank_corners(W, D, 4), plank_corners(W, D, 4)
    for cx in (W * 0.3, W * 0.7):
        zip_station(bottom, top, cx, 2, D - 2)
    layer(tris, (0, 0, W, D), bottom, 0, GROOVE)
    layer(tris, (0, 0, W, D), top, GROOVE, T)
    for y0 in (4, D - 6):
        box(tris, 10, y0, T, W - 10, y0 + 2, T + 10)
    for x0 in (6, W - 8):
        box(tris, x0, 6, T, x0 + 2, D - 6, T + 14)
    write_stl(OUT / "psu-cradle-side.stl", tris)


# ---------------------------------------------------------------- rack


def rack_4slot():
    """Four upright board slots on one 160 x 80 base.

    Per slot (centre cx): USB-C socket clamp at the front edge (facing the
    deck's rear wall), junction pocket behind it, fin at the back. The
    board hangs on the fin's front face — a 12 mm rib fits between its
    two header rows, a ledge carries its bottom edge — held by two cable
    ties around board + fin, located by edge notches. Prints upright.
    """
    tris = []
    bottom, top = plank_corners(RACK_W, RACK_D, 5), plank_corners(RACK_W, RACK_D, 5)
    slots = [SLOT_X0 + SLOT_PITCH * i for i in range(4)]
    for cx in slots:
        zip_station_x(bottom, top, 12, cx - 17, cx + 17)        # socket strap
    layer(tris, (0, 0, RACK_W, RACK_D), bottom, 0, GROOVE)
    layer(tris, (0, 0, RACK_W, RACK_D), top, GROOVE, T)
    fin_y0 = RACK_D - 14
    pw, pd = POCKET
    for cx in slots:
        for rx in (cx - SOCKET_W / 2 - 2, cx + SOCKET_W / 2):
            box(tris, rx, 3, T, rx + 2, 20, T + SOCKET_H)
        pocket = (cx - pw / 2, 24, cx + pw / 2, 24 + pd)
        interior = (pocket[0] + 2, 26, pocket[2] - 2, 24 + pd - 2)
        layer(tris, pocket, [interior], T, T + 6)
        layer(tris, pocket, [interior,
                             (cx - 9, 24, cx + 9, 26),                    # front: wires in/out
                             (cx - 6, 24 + pd - 2, cx + 6, 24 + pd)],     # rear: male pigtail up
              T + 6, T + POCKET_H)
        fin = (cx - FIN_W / 2, fin_y0, cx + FIN_W / 2, fin_y0 + FIN_T)
        narrow = (cx - FIN_W / 2 + 2, fin_y0, cx + FIN_W / 2 - 2, fin_y0 + FIN_T)
        z = T
        for z0, z1 in ((T + BOARD_LIFT + 12, T + BOARD_LIFT + 18),
                       (T + BOARD_LIFT + 42, T + BOARD_LIFT + 48)):
            layer(tris, fin, [], z, z0)
            layer(tris, narrow, [], z0, z1)
            z = z1
        layer(tris, fin, [], z, FIN_TOP)
        box(tris, cx - 6, fin_y0 - 3, T + BOARD_LIFT - 2, cx + 6, fin_y0, FIN_TOP)      # rib
        box(tris, cx - FIN_W / 2, fin_y0 - 12, T + BOARD_LIFT - 4,
            cx + FIN_W / 2, fin_y0, T + BOARD_LIFT - 2)                                # ledge
    write_stl(OUT / "rack-4slot.stl", tris)


def rack_pocket_lid():
    pw, pd = POCKET
    tris = []
    layer(tris, (0, 0, pw, pd), [(pw / 2 - 3, pd - 6, pw / 2 + 3, pd)], 0, WALL)
    box(tris, 2 + CLR, 2 + CLR, -3, pw - 2 - CLR, pd - 6, 0)
    write_stl(OUT / "rack-pocket-lid.stl", tris)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    bay_corner_post()
    bay_splice_post()
    deck_walls()
    deck_lid()
    relay_tray()
    psu_cradle_side()
    rack_4slot()
    rack_pocket_lid()
