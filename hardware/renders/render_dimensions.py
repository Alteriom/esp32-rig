#!/usr/bin/env python3
"""Draw the dimensioned drawing of the chassis (chassis-dimensions.svg).

Three orthographic views with dimension lines — plan, front elevation
and side elevation — every number taken from generate_chassis.py, so the
drawing cannot drift from the STLs. Pure Python, no dependencies:

    python3 render_dimensions.py      # writes ./chassis-dimensions.svg
    # PNG: chromium --headless=new --hide-scrollbars --window-size=1600,1188
    #        --screenshot=chassis-dimensions.png file://$PWD/chassis-dimensions.svg
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mounts"))
import generate_chassis as gc  # noqa: E402

HERE = Path(__file__).parent
OUT = HERE / "chassis-dimensions.svg"

W, H = 1600, 1100
S = 1.8                     # px per mm
INK = "#1f2933"
DIM = "#b7561a"             # dimension lines
FILL_DECK = "#e3e6ea"
FILL_PART = "#c9ced5"
FILL_LID = "#f2c48f"
FILL_RACK = "#b9bfc7"
FILL_BOARD = "#3a4a3f"

out = []


def w(s):
    out.append(s)


def text(x, y, s, size=12, anchor="middle", weight="normal", color=INK, rot=None, family="sans-serif"):
    t = f' transform="rotate({rot} {x} {y})"' if rot is not None else ""
    w(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" text-anchor="{anchor}" font-weight="{weight}" '
      f'fill="{color}" font-family="{family}"{t}>{s}</text>')


def rect(x, y, wd, ht, fill="none", stroke=INK, sw=1.2, dash=None):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    w(f'<rect x="{x:.1f}" y="{y:.1f}" width="{wd:.1f}" height="{ht:.1f}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{d}/>')


def line(x0, y0, x1, y1, color=INK, sw=1, dash=None):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    w(f'<line x1="{x0:.1f}" y1="{y0:.1f}" x2="{x1:.1f}" y2="{y1:.1f}" stroke="{color}" stroke-width="{sw}"{d}/>')


class Frame:
    """Maps mm (u, v) of one view into page px; v grows upward."""

    def __init__(self, ox, oy, scale=S):
        self.ox, self.oy, self.s = ox, oy, scale

    def x(self, u):
        return self.ox + u * self.s

    def y(self, v):
        return self.oy - v * self.s

    def box(self, u0, v0, u1, v1, **kw):
        rect(self.x(u0), self.y(v1), (u1 - u0) * self.s, (v1 - v0) * self.s, **kw)

    def dim_h(self, u0, u1, v, label=None, offset=0, above=True):
        """Horizontal dimension between u0 and u1 at height v (+offset px)."""
        y = self.y(v) - offset if above else self.y(v) + offset
        x0, x1 = self.x(u0), self.x(u1)
        for xx, vv in ((x0, u0), (x1, u1)):
            line(xx, self.y(v), xx, y + (6 if above else -6), DIM, 0.8)
        line(x0, y, x1, y, DIM, 1)
        for xx, d in ((x0, 1), (x1, -1)):
            w(f'<polygon points="{xx:.1f},{y:.1f} {xx + d * 8:.1f},{y - 3:.1f} {xx + d * 8:.1f},{y + 3:.1f}" fill="{DIM}"/>')
        text((x0 + x1) / 2, y - 4, label or f"{u1 - u0:g}", size=12, color=DIM, family="monospace")

    def dim_v(self, v0, v1, u, label=None, offset=0, right=True):
        x = self.x(u) + offset if right else self.x(u) - offset
        y0, y1 = self.y(v0), self.y(v1)
        for yy, uu in ((y0, v0), (y1, v1)):
            line(self.x(u), yy, x + (-6 if right else 6), yy, DIM, 0.8)
        line(x, y0, x, y1, DIM, 1)
        for yy, d in ((y0, -1), (y1, 1)):
            w(f'<polygon points="{x:.1f},{yy:.1f} {x - 3:.1f},{yy + d * 8:.1f} {x + 3:.1f},{yy + d * 8:.1f}" fill="{DIM}"/>')
        text(x + (10 if right else -10), (y0 + y1) / 2 + 4, label or f"{v1 - v0:g}", size=12,
             color=DIM, anchor="start" if right else "end", family="monospace")


# geometry from the generator
DW, DD, DH = gc.DECK_W, gc.DECK_D, gc.DECK_H
RW, RD = gc.RACK_W, gc.RACK_D
RY = DD + gc.RACK_GAP
BW, BD = gc.BASE_W, gc.BASE_D
SLOTS = gc.slot_centres()
FIN_Y = RY + RD - 14
FIN_TOP = gc.FIN_TOP
T = gc.T
relay_w, relay_d = gc.RELAY_HOLES[0] + 28, gc.RELAY_HOLES[1] + 26
psu_w, psu_d = gc.BRICK[0] + 16, gc.BRICK[2] + 12
RELAY_AT, HUB_AT, PI_AT, PSU_AT = (16, 16), (190, 16), (16, 90), (190, 100)

w(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="sans-serif">')
w(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')
text(40, 36, "Chassis — dimensioned drawing (mm)", size=22, anchor="start", weight="bold")
text(40, 58, f"Base {BW:g} × {BD:g}, deck {DW:g} × {DD:g} × {DH:g}, rack 2 × {RW:g} × {RD:g}, boards at {gc.SLOT_PITCH:g} mm pitch, "
     f"fins {FIN_TOP:.0f} tall. Every value comes from hardware/mounts/generate_chassis.py.", size=13, anchor="start", color="#3d4852")

# ------------------------------------------------------------------ plan
pl = Frame(140, 660)
text(pl.x(0), pl.y(BD) - 62, "PLAN (from above, front edge at the bottom)", size=13, anchor="start", weight="bold")
pl.box(0, 0, BW, BD, fill="#faf7f2", stroke="#8a7a66", sw=1.5)                       # base
pl.box(0, 0, DW, DD, fill=FILL_DECK)                                                 # deck footprint
pl.box(gc.INSET, gc.INSET, DW - gc.INSET, DD - gc.INSET, stroke=INK, sw=2)            # walls
for x0 in gc.RACK_X:
    pl.box(x0, RY, x0 + RW, RY + RD, fill=FILL_RACK)
pl.box(*RELAY_AT, RELAY_AT[0] + relay_w, RELAY_AT[1] + relay_d, fill=FILL_PART)
text(pl.x(RELAY_AT[0] + relay_w / 2), pl.y(RELAY_AT[1] + relay_d / 2) + 4, "relay-tray", size=11)
pl.box(*HUB_AT, HUB_AT[0] + 110, HUB_AT[1] + 70, fill=FILL_PART)
text(pl.x(HUB_AT[0] + 55), pl.y(HUB_AT[1] + 35) + 4, "hub (ports → rack)", size=11)
pl.box(*PI_AT, PI_AT[0] + 104, PI_AT[1] + 82, fill=FILL_PART)
text(pl.x(PI_AT[0] + 52), pl.y(PI_AT[1] + 41) + 4, "pi5-tile", size=11)
pl.box(*PSU_AT, PSU_AT[0] + psu_w, PSU_AT[1] + psu_d, fill=FILL_PART)
text(pl.x(PSU_AT[0] + psu_w / 2), pl.y(PSU_AT[1] + psu_d / 2) + 4, "12 V brick on edge", size=11)
for i, cx in enumerate(SLOTS):
    pl.box(cx - gc.POCKET[0] / 2, RY + 24, cx + gc.POCKET[0] / 2, RY + 24 + gc.POCKET[1], fill=FILL_LID)
    pl.box(cx - gc.FIN_W / 2, FIN_Y, cx + gc.FIN_W / 2, FIN_Y + gc.FIN_T, fill=INK)
    pl.box(cx - gc.SOCKET_W / 2 - 2, RY + 3, cx + gc.SOCKET_W / 2 + 2, RY + 20, stroke="#6b7280", sw=0.8)
    pl.box(cx - 10, DD - gc.INSET - gc.WALL, cx + 10, DD - gc.INSET, fill="#ffffff", stroke=INK, sw=0.8)   # rear-wall notch
    text(pl.x(cx), pl.y(RY + RD) - 6, f"{i + 1}", size=11, weight="bold")
for x in (gc.SLOT_DEPTH + (DW - 3 * gc.POST + 4 * gc.SLOT_DEPTH) / 2,):
    line(pl.x(x + gc.POST / 2), pl.y(0), pl.x(x + gc.POST / 2), pl.y(DD), "#6b7280", 0.8, "4 3")
# dimensions
pl.dim_h(0, BW, BD, offset=22)
pl.dim_h(0, DW, 0, offset=40, above=False, label=f"{DW:g} deck")
pl.dim_v(0, DD, BW, offset=26)
pl.dim_v(RY, RY + RD, BW, offset=26, label=f"{RD:g} rack")
pl.dim_v(0, BD, BW, offset=70)
pl.dim_h(SLOTS[0], SLOTS[1], RY + RD, offset=52, label=f"{gc.SLOT_PITCH:g} pitch")
pl.dim_h(SLOTS[3], SLOTS[4], RY + RD, offset=52, label=f"{SLOTS[4] - SLOTS[3]:g}")
pl.dim_h(0, SLOTS[0], RY + RD, offset=22, label=f"{SLOTS[0]:g}")
pl.dim_h(gc.RACK_X[0], gc.RACK_X[0] + RW, 0, offset=70, above=False, label=f"{RW:g} rack module")
pl.dim_v(0, RELAY_AT[1], 0, offset=26, right=False, label=f"{RELAY_AT[1]:g}")
pl.dim_v(RELAY_AT[1], RELAY_AT[1] + relay_d, 0, offset=26, right=False, label=f"{relay_d:g}")
pl.dim_v(PI_AT[1], PI_AT[1] + 82, 0, offset=26, right=False, label="82")

# ----------------------------------------------------------- front elevation
fe = Frame(140, 1040)
text(fe.x(0), fe.y(FIN_TOP) - 40, "FRONT ELEVATION (looking at the deck's front wall; rack behind)", size=13, anchor="start", weight="bold")
fe.box(0, -12, BW, 0, fill="#faf7f2", stroke="#8a7a66")                 # base board (12 mm)
for cx in SLOTS:                                                        # fins + boards behind the deck
    fe.box(cx - gc.FIN_W / 2, 0, cx + gc.FIN_W / 2, FIN_TOP, fill=FILL_RACK, stroke="#6b7280", sw=0.8)
    fe.box(cx - 13, T + gc.BOARD_LIFT, cx + 13, T + gc.BOARD_LIFT + gc.BOARD_H, fill=FILL_BOARD, stroke="none")
fe.box(0, 0, DW, DH, fill=FILL_DECK, sw=1.5)                            # deck wall
fe.box(0, DH, DW, DH + gc.WALL, fill=FILL_LID)                          # lid
fe.dim_v(0, DH, DW, offset=26, label=f"{DH:g} wall")
fe.dim_v(0, FIN_TOP, DW, offset=70, label=f"{FIN_TOP:.0f} fin")
fe.dim_v(T + gc.BOARD_LIFT, T + gc.BOARD_LIFT + gc.BOARD_H, 0, offset=26, right=False, label=f"{gc.BOARD_H:g} board")
fe.dim_v(0, T + gc.BOARD_LIFT, 0, offset=70, right=False, label=f"{T + gc.BOARD_LIFT:.0f} lift")
fe.dim_h(0, DW, -12, offset=22, above=False)

# ------------------------------------------------------------ side elevation
se = Frame(960, 1040)
text(se.x(0), se.y(FIN_TOP) - 40, "SIDE ELEVATION (from the right end; front at the left)", size=13, anchor="start", weight="bold")
se.box(0, -12, BD, 0, fill="#faf7f2", stroke="#8a7a66")
se.box(0, 0, DD, DH, fill=FILL_DECK, sw=1.5)
se.box(0, DH, DD, DH + gc.WALL, fill=FILL_LID)
se.box(PSU_AT[1], T, PSU_AT[1] + psu_d, T + gc.BRICK[1], fill=FILL_PART, stroke="#6b7280", sw=0.8)   # brick on edge
text(se.x(PSU_AT[1] + psu_d / 2), se.y(T + gc.BRICK[1] / 2) + 4, "brick", size=10, rot=-90)
se.box(RY, 0, RY + RD, T, fill=FILL_RACK)                                       # rack base
se.box(RY + 3, T, RY + 20, T + gc.SOCKET_H, fill=FILL_PART, stroke="#6b7280", sw=0.8)   # socket
se.box(RY + 24, T, RY + 24 + gc.POCKET[1], T + gc.POCKET_H, fill=FILL_LID, stroke="#6b7280", sw=0.8)   # pocket
se.box(FIN_Y, T, FIN_Y + gc.FIN_T, FIN_TOP, fill=INK)                          # fin
se.box(FIN_Y - 12, T + gc.BOARD_LIFT - 4, FIN_Y, T + gc.BOARD_LIFT - 2, fill=INK)   # ledge
se.box(FIN_Y - 4.6, T + gc.BOARD_LIFT, FIN_Y - 3, T + gc.BOARD_LIFT + gc.BOARD_H, fill=FILL_BOARD, stroke="none")  # PCB
se.box(FIN_Y - 7.6, T + gc.BOARD_LIFT + 30, FIN_Y - 4.6, T + gc.BOARD_LIFT + 45, fill="#8a9099", stroke="none")   # module can
line(se.x(FIN_Y - 6), se.y(T + gc.BOARD_LIFT), se.x(RY + 24 + gc.POCKET[1] - 2), se.y(T + gc.POCKET_H), "#6b7280", 1.5, "3 3")  # male pigtail path
se.dim_h(0, DD, -12, offset=22, above=False, label=f"{DD:g} deck")
se.dim_h(RY, RY + RD, -12, offset=22, above=False, label=f"{RD:g}")
se.dim_h(0, BD, -12, offset=52, above=False)
se.dim_v(0, DH, 0, offset=26, right=False, label=f"{DH:g}")
se.dim_v(0, T + gc.POCKET_H, BD, offset=26, label=f"{T + gc.POCKET_H:.0f} pocket")
se.dim_v(T + gc.POCKET_H, T + gc.BOARD_LIFT, BD, offset=26, label=f"{gc.BOARD_LIFT - gc.POCKET_H:.0f} plug room")
se.dim_v(T + gc.BOARD_LIFT, FIN_TOP, BD, offset=26, label=f"{FIN_TOP - T - gc.BOARD_LIFT:.0f}")
se.dim_v(0, FIN_TOP, BD, offset=62, label=f"{FIN_TOP:.0f} total")
se.dim_h(RY + 24, RY + 24 + gc.POCKET[1], FIN_TOP, offset=10, label=f"{gc.POCKET[1]:g} pocket")

# legend
LX, LY = 1000, 110
text(LX, LY, "Legend", size=12, anchor="start", weight="bold")
for i, (f, s) in enumerate(((FILL_DECK, "control deck footprint / walls"), (FILL_RACK, "rack modules, fins"),
                            (FILL_LID, "lids and junction pockets (orange PETG)"), (FILL_PART, "trays and tiles"),
                            (FILL_BOARD, "board (devkit) outline"))):
    rect(LX, LY + 12 + i * 18, 14, 10, fill=f, stroke="#6b7280", sw=0.6)
    text(LX + 22, LY + 21 + i * 18, s, size=11, anchor="start")
text(LX, LY + 118, "MEASURE before printing: RELAY_HOLES, BRICK, SOCKET_W / SOCKET_H (generate_chassis.py).",
     size=11, anchor="start", color="#3d4852")
text(LX, LY + 136, f"Boards: antenna up, USB down; bottom edge rests on the fin's ledge {T + gc.BOARD_LIFT:.0f} mm above the base.",
     size=11, anchor="start", color="#3d4852")

w("</svg>")
OUT.write_text("\n".join(out) + "\n", encoding="utf-8")
print(f"wrote {OUT}")
