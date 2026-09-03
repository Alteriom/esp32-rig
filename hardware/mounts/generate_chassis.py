#!/usr/bin/env python3
"""Generate the rig chassis STLs: control bay, cable channel, board stations.

Same rules as generate_mounts.py (stdlib only, axis-aligned boxes, Z=0 on
the plank, millimetres) — this file reuses its primitives and adds the
parts that turn the open plank into an enclosed, wire-managed rig for the
relay-switched build described in docs/relay-power-wiring.md:

    python3 generate_chassis.py       # writes stl/*.stl (chassis set)

Parts (see README.md "Chassis" for print settings and assembly):

  control bay (300 x 200 footprint = one plank end, 45 mm walls)
    bay-corner-post.stl        x4  slotted post, walls slide in
    bay-splice-post.stl        x2  in-line post joining two long segments
    bay-wall-long.stl          x4  144 mm segment, screw flange, 2 per side
    bay-wall-short-outer.stl   x1  188 mm end wall with two cable entries
    bay-wall-short-inner.stl   x1  188 mm end wall with the channel port
    bay-lid.stl                x2  150 x 200 vented panel, drop-in rails
    relay-tray.stl             x1  8-ch relay module: slotted M3 bosses
    psu-cradle.stl             x1  12 V brick: end stops + straps
  cable channel (front edge of the plank)
    cable-channel-200.stl      xN  U-channel, exit notches every 50 mm
    cable-channel-100.stl      x1  half segment to finish the run
    cable-channel-lid-200.stl  xN  press-fit lid
    cable-channel-lid-100.stl  x1
  board station (one per board / rig port)
    board-station.stl          xN  devkit channel + USB-C socket clamp +
                                   wire-nut junction pocket
    station-pocket-lid.stl     xN  friction-fit lid for the pocket

  compact set (330 x 272 footprint, boards upright in a card rack)
    deck-wall-long.stl         x2  159 mm front segment
    deck-wall-rear.stl         x2  159 mm rear segment, one cable notch per slot
    deck-wall-end.stl          x2  178 mm end wall with two cable entries
    deck-lid.stl               x2  165 x 190 vented half
    rack-4slot.stl             x2  four upright board slots: fin + strap
                                   notches + junction pocket + socket clamp
    rack-pocket-lid.stl        x8  pocket lid with the male-pigtail notch
    psu-cradle-side.stl        x1  12 V brick standing on its edge
    (posts, relay-tray, pi5-tile and hub-strap-tile are shared)

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
BAY_W, BAY_D = 300.0, 200.0   # bay footprint = plank width x one end
BAY_H = 50.0        # clears Pi USB stack (27), hub (30), brick on its edge (48)
INSET = WALL        # walls sit one wall-thickness in from the plank edge
FLANGE = 10.0       # screw flange along the inside foot of every wall
POST = 12.0         # corner / splice post side
SLOT_DEPTH = POST / 2   # how far a wall end slides into a post
CH_Y = 16.0         # channel floor starts here (clears posts + wall flanges)
CH_W, CH_H = 34.0, 24.0       # cable channel outer width / wall height
SOCKET_W = 20.0     # MEASURE: USB-C female pigtail overmold width + 2
SOCKET_H = 9.0      # MEASURE: overmold height; clamp ribs stand this tall
RELAY_HOLES = (130.0, 43.0)   # MEASURE: 8-ch module hole pattern (x, y)
BRICK = (100.0, 45.0)         # MEASURE: 12 V brick footprint (x, y)


def zip_station_x(bottom_cuts, top_cuts, cy, x_near, x_far):
    """Tie station running along X (generate_mounts' runs along Y)."""
    y0, y1 = cy - TIE_W / 2, cy + TIE_W / 2
    bottom_cuts.append((x_near, y0, x_far, y1))
    top_cuts.append((x_near, y0, x_near + SLOT_H, y1))
    top_cuts.append((x_far - SLOT_H, y0, x_far, y1))


def plank_corners(W, D, inset=6.0):
    return [sq(inset, inset, PLANK_HOLE), sq(W - inset, inset, PLANK_HOLE),
            sq(inset, D - inset, PLANK_HOLE), sq(W - inset, D - inset, PLANK_HOLE)]


# --------------------------------------------------------------- bay posts


def bay_corner_post():
    """12 mm post at a bay corner; the two walls slide into 6 mm-deep slots.

    Oriented for the front-left corner: wall along +X leaves through the
    +X face, wall along +Y through the +Y face. Rotate for the others.
    A 10 mm foot inside the corner takes one plank screw.
    """
    tris = []
    slot_w = WALL + 2 * CLR
    slot_x = (POST / 2, INSET, POST, INSET + slot_w)          # wall along X
    slot_y = (INSET, POST / 2, INSET + slot_w, POST)          # wall along Y
    layer(tris, (0, 0, POST, POST), [slot_x, slot_y], 0, BAY_H)
    foot = (POST, POST, POST + 10, POST + 10)
    layer(tris, foot, [sq(POST + 5, POST + 5, PLANK_HOLE)], 0, T)
    write_stl(OUT / "bay-corner-post.stl", tris)


def bay_splice_post():
    """In-line post: a through slot along X joins two long-wall segments."""
    tris = []
    slot_w = WALL + 2 * CLR
    slot = (0, INSET, POST, INSET + slot_w)
    layer(tris, (0, 0, POST, POST), [slot], 0, BAY_H)
    foot = (0, POST, POST, POST + 10)
    layer(tris, foot, [sq(POST / 2, POST + 5, PLANK_HOLE)], 0, T)
    write_stl(OUT / "bay-splice-post.stl", tris)


# --------------------------------------------------------------- bay walls


def bay_wall(name, length, notches=()):
    """Wall segment along X: plate at y 0..WALL, flange inward to y FLANGE.

    `notches` are (x0, x1, height) openings cut up from the plank surface
    through both the wall and its flange (cable entries, channel port).
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
    # wall above the flange, notches continue up to their height
    z = T
    for h in sorted({n[2] for n in notches}):
        cuts = [(x0, 0, x1, WALL) for x0, x1, nh in notches if nh > z]
        layer(tris, (0, 0, length, WALL), cuts, z, min(h, BAY_H))
        z = min(h, BAY_H)
    if z < BAY_H:
        layer(tris, (0, 0, length, WALL), [], z, BAY_H)
    write_stl(OUT / name, tris)


def bay_walls():
    # 3 posts + 2 segments per long side, each end 6 mm inside a post
    seg = (BAY_W - 3 * POST + 4 * SLOT_DEPTH) / 2          # 144
    bay_wall("bay-wall-long.stl", seg)
    short = BAY_D - 2 * POST + 2 * SLOT_DEPTH               # 188
    # outer end wall (plank end): Ethernet / Pi USB-C / 12 V barrel / hub PSU
    bay_wall("bay-wall-short-outer.stl", short,
             notches=[(40, 72, 22), (104, 136, 22)])
    # inner end wall: the cable channel passes through at the front corner
    # wall coordinate 0 is inside the front post (bay y = POST - SLOT_DEPTH)
    c0 = CH_Y - (POST - SLOT_DEPTH) - 1
    bay_wall("bay-wall-short-inner.stl", short,
             notches=[(c0, c0 + CH_W + 2, CH_H + WALL + 2)])


def bay_lid(name="bay-lid.stl", W=BAY_W / 2, D=BAY_D):
    """Half lid. Rails underneath drop inside the walls; the two halves
    butt at the bay's midline. Vent slots over the hot parts.
    """
    tris = []
    vents = []
    for row in range(int((D - 60) // 32)):
        y = 40 + row * 32
        for col in range(int((W - 30) // 44)):
            x = 18 + col * 44
            vents.append((x, y, x + 36, y + 3))
            vents.append((x, y + 9, x + 36, y + 12))
    layer(tris, (0, 0, W, D), vents, 0, WALL)
    inner = INSET + WALL + CLR                    # inside face of the walls
    rail_h = 5.0
    # rails hang inside the walls and stop clear of the posts (12 mm at
    # the outer corners, the splice post at the bay midline = this edge)
    g = POST + CLR
    box(tris, g, inner, -rail_h, W - g, inner + WALL, 0)              # front
    box(tris, g, D - inner - WALL, -rail_h, W - g, D - inner, 0)      # rear
    box(tris, inner, g, -rail_h, inner + WALL, D - g, 0)              # outer end
    # print rails-up (plate on the bed); Z=0 is the plate's underside
    write_stl(OUT / name, tris)


# --------------------------------------------------------------- relay tray


def relay_tray():
    """Tray for the 8-channel relay module: four slotted M3 bosses (±3 mm
    in X absorbs hole-pattern variance). Sized to sit beside the Pi tile
    inside the bay; the IN and VBUS bundles tie down on the cradle/hub
    tiles next to it."""
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


# --------------------------------------------------------------- psu cradle


def psu_cradle():
    """12 V brick: end stops locate it, two straps hold it."""
    bx, by = BRICK
    W, D = bx + 20, by + 20
    tris = []
    bottom, top = plank_corners(W, D), plank_corners(W, D)
    for cx in (W * 0.28, W * 0.72):
        zip_station(bottom, top, cx, 4, D - 4)
    layer(tris, (0, 0, W, D), bottom, 0, GROOVE)
    layer(tris, (0, 0, W, D), top, GROOVE, T)
    for x0 in (8, W - 10):
        box(tris, x0, 10, T, x0 + 2, D - 10, T + 12)
    write_stl(OUT / "psu-cradle.stl", tris)


# ------------------------------------------------------------ cable channel


def cable_channel(length):
    """U-channel along the plank's front edge; 10 mm exit notches every
    50 mm in the rear wall (the station side), screw holes in the floor."""
    tris = []
    holes = [sq(x, CH_W / 2, PLANK_HOLE) for x in range(25, int(length), 50)]
    layer(tris, (0, 0, length, CH_W), holes, 0, T)
    box(tris, 0, 0, T, length, WALL, CH_H)                      # front wall
    rear = [(0, CH_W - WALL, length, CH_W)]
    notch_h = 12.0
    layer(tris, rear[0], [], T, CH_H - notch_h)
    notches = [(x - 5, CH_W - WALL, x + 5, CH_W) for x in range(50, int(length), 50)]
    layer(tris, rear[0], notches, CH_H - notch_h, CH_H)
    write_stl(OUT / f"cable-channel-{int(length)}.stl", tris)


def cable_channel_lid(length):
    tris = []
    layer(tris, (0, 0, length, CH_W), [], 0, WALL)
    inner = WALL + CLR
    box(tris, 2, inner, -5, length - 2, inner + WALL, 0)
    box(tris, 2, CH_W - inner - WALL, -5, length - 2, CH_W - inner, 0)
    write_stl(OUT / f"cable-channel-lid-{int(length)}.stl", tris)


# ------------------------------------------------------------ board station

ST_W, ST_D = 130.0, 70.0
POCKET = (4.0, 26.0, 40.0, 66.0)     # x0 y0 x1 y1, outer
POCKET_H = 16.0


def board_station():
    """One rig port + one board.

    Left third: the USB-C female pigtail sits in a clamp facing the front
    edge (the cable channel), its wires run into a lidded pocket holding
    the three wire nuts and the two red joins; the male pigtail leaves the
    pocket through the right-hand notch to the board's USB socket.
    Right two thirds: the same 29 mm devkit channel + tie stations as
    esp32-board-tile.stl, antenna overhanging the right edge.
    """
    tris = []
    bottom, top = plank_corners(ST_W, ST_D), plank_corners(ST_W, ST_D)
    for cx in (58, 80, 102):                       # devkit straps (Y-run)
        zip_station(bottom, top, cx, 13, ST_D - 13)
    zip_station_x(bottom, top, 13.5, 4, 38)        # socket strap (X-run)
    layer(tris, (0, 0, ST_W, ST_D), bottom, 0, GROOVE)
    layer(tris, (0, 0, ST_W, ST_D), top, GROOVE, T)
    # devkit rails: 29 mm channel centred on the station
    cy = ST_D / 2
    box(tris, 46, cy - 16, T, ST_W - 8, cy - 13, T + 3)
    box(tris, 46, cy + 13, T, ST_W - 8, cy + 16, T + 3)
    # socket clamp ribs, socket opening faces y=0
    rib_x0 = (4 + 38) / 2 - SOCKET_W / 2 - 2
    box(tris, rib_x0, 4, T, rib_x0 + 2, 22, T + SOCKET_H)
    box(tris, rib_x0 + SOCKET_W + 2, 4, T, rib_x0 + SOCKET_W + 4, 22, T + SOCKET_H)
    # junction pocket: 2 mm walls, notches from 6 mm up
    px0, py0, px1, py1 = POCKET
    interior = (px0 + 2, py0 + 2, px1 - 2, py1 - 2)
    layer(tris, POCKET, [interior], T, T + 6)
    notches = [
        (12, py0, 30, py0 + 2),                    # front: socket wires + red pair
        (px1 - 2, 44, px1, 56),                    # right: male pigtail to the board
    ]
    layer(tris, POCKET, [interior] + notches, T + 6, T + POCKET_H)
    write_stl(OUT / "board-station.stl", tris)


def station_pocket_lid():
    px0, py0, px1, py1 = POCKET
    tris = []
    layer(tris, (0, 0, px1 - px0, py1 - py0), [], 0, WALL)
    box(tris, 2 + CLR, 2 + CLR, -3, px1 - px0 - 2 - CLR, py1 - py0 - 2 - CLR, 0)
    write_stl(OUT / "station-pocket-lid.stl", tris)


# ============================================================= compact set
#
# Boards stand upright in a card rack behind the control deck: antenna up,
# USB socket down, each board strapped to a fin with two cable ties. The
# junction pocket sits directly under the board, its USB-C socket facing
# the deck's rear wall, so the hub cable and the two red wires pass
# straight through one notch. No cable channel, no plank run: the whole
# rig is 330 x 272 mm.

DECK_W, DECK_D = 330.0, 190.0
SLOT_PITCH = 40.0        # board pitch; 8 boards = 2 rack modules of 4
FIN_T = 4.0
BOARD_LIFT = 55.0        # board's bottom edge above the rack base: room for
                         # the male pigtail's plug (~30 mm) above the pocket
BOARD_H = 55.0           # longest common devkit; the fin clears it
RACK_W, RACK_D = 160.0, 80.0


def deck_walls():
    seg = (DECK_W - 3 * POST + 4 * SLOT_DEPTH) / 2                 # 159
    end = DECK_D - 2 * POST + 2 * SLOT_DEPTH                        # 178
    bay_wall("deck-wall-long.stl", seg)
    # rear: one 20 x 20 notch per board slot (rack modules sit at x = 6 and
    # 165, slots at 20 + 40 n from each module's origin), same on both
    # segments
    bay_wall("deck-wall-rear.stl", seg,
             notches=[(cx - 10, cx + 10, 20) for cx in (20, 60, 100, 140)])
    bay_wall("deck-wall-end.stl", end, notches=[(40, 72, 22), (104, 136, 22)])
    bay_lid("deck-lid.stl", DECK_W / 2, DECK_D)


def psu_cradle_side():
    """12 V brick standing on its long edge: 100 x 30 footprint, 45 tall."""
    bx, by = BRICK[0], 30.0
    W, D = bx + 16, by + 12
    tris = []
    bottom, top = plank_corners(W, D, 4), plank_corners(W, D, 4)
    for cx in (W * 0.3, W * 0.7):
        zip_station(bottom, top, cx, 2, D - 2)
    layer(tris, (0, 0, W, D), bottom, 0, GROOVE)
    layer(tris, (0, 0, W, D), top, GROOVE, T)
    for y0 in (4, D - 6):                                   # side rails
        box(tris, 10, y0, T, W - 10, y0 + 2, T + 10)
    for x0 in (6, W - 8):                                   # end stops
        box(tris, x0, 6, T, x0 + 2, D - 6, T + 14)
    write_stl(OUT / "psu-cradle-side.stl", tris)


def rack_4slot():
    """Four upright board slots on one 160 x 80 base.

    Per slot (centre cx): socket clamp at the front edge (facing the deck),
    junction pocket behind it, and a fin at the back. The board hangs on
    the fin's front face — a 12 mm rib fits between its two header rows —
    held by two cable ties around board + fin, located by edge notches.
    Prints upright, fins are 4 mm thick and 118 mm tall.
    """
    tris = []
    bottom, top = plank_corners(RACK_W, RACK_D, 5), plank_corners(RACK_W, RACK_D, 5)
    slots = [20 + SLOT_PITCH * i for i in range(4)]
    for cx in slots:
        zip_station_x(bottom, top, 12, cx - 17, cx + 17)        # socket strap
    layer(tris, (0, 0, RACK_W, RACK_D), bottom, 0, GROOVE)
    layer(tris, (0, 0, RACK_W, RACK_D), top, GROOVE, T)
    fin_y0 = RACK_D - 14
    fin_top = T + BOARD_LIFT + BOARD_H + 5
    for cx in slots:
        # socket clamp ribs, mouth toward y = 0
        for rx in (cx - SOCKET_W / 2 - 2, cx + SOCKET_W / 2):
            box(tris, rx, 3, T, rx + 2, 20, T + SOCKET_H)
        # pocket: 34 x 40, 2 mm walls; front notch (socket wires + red
        # pair), rear notch (male pigtail up to the board)
        pocket = (cx - 17, 24, cx + 17, 64)
        interior = (cx - 15, 26, cx + 15, 62)
        layer(tris, pocket, [interior], T, T + 6)
        layer(tris, pocket, [interior, (cx - 9, 24, cx + 9, 26), (cx - 6, 62, cx + 6, 64)],
              T + 6, T + POCKET_H)
        # fin with strap notches on both vertical edges
        fin = (cx - 15, fin_y0, cx + 15, fin_y0 + FIN_T)
        narrow = (cx - 13, fin_y0, cx + 13, fin_y0 + FIN_T)
        z = T
        for z0, z1 in ((T + BOARD_LIFT + 12, T + BOARD_LIFT + 18),
                       (T + BOARD_LIFT + 42, T + BOARD_LIFT + 48)):
            layer(tris, fin, [], z, z0)
            layer(tris, narrow, [], z0, z1)
            z = z1
        layer(tris, fin, [], z, fin_top)
        # rib between the header rows, board rests on it
        box(tris, cx - 6, fin_y0 - 3, T + BOARD_LIFT - 2, cx + 6, fin_y0, fin_top)
        # ledge the board's bottom edge sits on
        box(tris, cx - 15, fin_y0 - 12, T + BOARD_LIFT - 4, cx + 15, fin_y0, T + BOARD_LIFT - 2)
    write_stl(OUT / "rack-4slot.stl", tris)


def rack_pocket_lid():
    tris = []
    layer(tris, (0, 0, 34, 40), [(14, 34, 20, 40)], 0, WALL)       # rear notch
    box(tris, 2 + CLR, 2 + CLR, -3, 34 - 2 - CLR, 34, 0)
    write_stl(OUT / "rack-pocket-lid.stl", tris)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    bay_corner_post()
    bay_splice_post()
    bay_walls()
    bay_lid()
    relay_tray()
    psu_cradle()
    cable_channel(200)
    cable_channel(100)
    cable_channel_lid(200)
    cable_channel_lid(100)
    board_station()
    station_pocket_lid()
    deck_walls()
    psu_cradle_side()
    rack_4slot()
    rack_pocket_lid()
