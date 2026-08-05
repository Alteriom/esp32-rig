#!/usr/bin/env python3
"""Render preview images of the assembled MVP rig.

Loads the real printable tiles from ../mounts/stl/ and combines them with
simply-modelled parts (plank, Pi, hub, ESP32 devkits, zip ties, USB
cables) so the pictures match what the blueprint + mounts actually build.

Not part of CI. Needs numpy + matplotlib:

    python3 -m pip install numpy matplotlib
    python3 render_rig.py            # writes ./*.png

Renderer: perspective camera + per-triangle z-buffer rasterizer (no GPU,
no external 3D packages), 2x supersampled. Units: mm, Z up.
"""

import math
import struct
from pathlib import Path

import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = Path(__file__).parent
STL = HERE.parent / "mounts" / "stl"

# ------------------------------------------------------------------ geometry


def load_stl(path):
    data = Path(path).read_bytes()
    n = struct.unpack("<I", data[80:84])[0]
    dt = np.dtype([("n", "<3f4"), ("v", "<9f4"), ("attr", "<u2")])
    rec = np.frombuffer(data, dtype=dt, count=n, offset=84)
    return rec["v"].reshape(n, 3, 3).astype(np.float64)


def box(x0, y0, z0, x1, y1, z1):
    v = np.array(
        [
            [x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
            [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1],
        ],
        dtype=float,
    )
    idx = [
        (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
        (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
        (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    ]
    return v[np.array(idx)]


def translate(tris, dx, dy, dz):
    return tris + np.array([dx, dy, dz])


def bezier_tube(p0, p1, p2, p3, r=1.9, n=None):
    """A cable: cubic bezier approximated by overlapping little cubes."""
    if n is None:  # dense enough that cubes overlap into a smooth tube
        poly = np.linalg.norm(np.diff(np.stack([p0, p1, p2, p3]), axis=0), axis=1)
        n = max(60, int(poly.sum() / (r * 0.8)))
    t = np.linspace(0, 1, n)[:, None]
    pts = (
        (1 - t) ** 3 * p0
        + 3 * (1 - t) ** 2 * t * p1
        + 3 * (1 - t) * t**2 * p2
        + t**3 * p3
    )
    return np.concatenate(
        [box(x - r, y - r, z - r, x + r, y + r, z + r) for x, y, z in pts]
    )


# ------------------------------------------------------------------ renderer


def normalize(v):
    return v / np.linalg.norm(v)


class View:
    def __init__(self, eye, target, fovy=32, width=1600, height=1000, ss=2):
        self.eye = np.array(eye, float)
        self.target = np.array(target, float)
        self.fovy, self.W, self.H, self.ss = fovy, width, height, ss
        f = normalize(self.target - self.eye)
        r = normalize(np.cross(f, np.array([0.0, 0.0, 1.0])))
        self.basis = (r, np.cross(r, f), f)

    def project(self, pts):
        """World (N,3) -> pixel (N,2) in final-image coordinates."""
        r, u, f = self.basis
        p = np.atleast_2d(pts) - self.eye
        x, y, z = p @ r, p @ u, p @ f
        scale = (self.H / 2) / math.tan(math.radians(self.fovy) / 2)
        return np.stack([self.W / 2 + x / z * scale, self.H / 2 - y / z * scale], 1)


L1 = normalize(np.array([-0.45, -0.5, 0.75]))
L2 = normalize(np.array([0.7, 0.25, 0.35]))


def render(scene, view, bg=(0.965, 0.965, 0.975)):
    W, H = view.W * view.ss, view.H * view.ss
    img = np.ones((H, W, 3)) * np.array(bg)
    zbuf = np.full((H, W), np.inf)
    r, u, f = view.basis
    scale = (H / 2) / math.tan(math.radians(view.fovy) / 2)
    for tris, color in scene:
        p = tris - view.eye
        x, y, z = p @ r, p @ u, p @ f  # each (N,3)
        n = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
        nl = np.linalg.norm(n, axis=1, keepdims=True)
        n = np.divide(n, nl, out=np.zeros_like(n), where=nl > 0)
        shade = (
            0.34
            + 0.48 * np.clip(n @ L1, 0, None)
            + 0.26 * np.clip(n @ L2, 0, None)
        )
        cols = np.clip(np.array(color) * shade[:, None], 0, 1)
        sx = W / 2 + x / z * scale
        sy = H / 2 - y / z * scale
        ok = (z > 5).all(axis=1)
        for i in np.nonzero(ok)[0]:
            tx, ty, tz = sx[i], sy[i], z[i]
            x0 = max(int(tx.min()), 0)
            x1 = min(int(tx.max()) + 1, W)
            y0 = max(int(ty.min()), 0)
            y1 = min(int(ty.max()) + 1, H)
            if x0 >= x1 or y0 >= y1:
                continue
            d = (tx[1] - tx[0]) * (ty[2] - ty[0]) - (tx[2] - tx[0]) * (ty[1] - ty[0])
            if abs(d) < 1e-9:
                continue
            gx, gy = np.meshgrid(
                np.arange(x0, x1) + 0.5, np.arange(y0, y1) + 0.5
            )
            w0 = ((tx[1] - gx) * (ty[2] - gy) - (tx[2] - gx) * (ty[1] - gy)) / d
            w1 = ((tx[2] - gx) * (ty[0] - gy) - (tx[0] - gx) * (ty[2] - gy)) / d
            w2 = 1 - w0 - w1
            inside = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
            if not inside.any():
                continue
            zi = w0 * tz[0] + w1 * tz[1] + w2 * tz[2]
            sub = zbuf[y0:y1, x0:x1]
            upd = inside & (zi < sub)
            sub[upd] = zi[upd]
            img[y0:y1, x0:x1][upd] = cols[i]
    s = view.ss
    return img.reshape(H // s, s, W // s, s, 3).mean((1, 3))


# ------------------------------------------------------------------- models

WOOD = (0.76, 0.60, 0.41)
TILE = (0.88, 0.47, 0.13)      # printed PETG, orange
PCB_DARK = (0.07, 0.09, 0.13)  # devkit PCB
SHIELD = (0.74, 0.75, 0.78)
GREEN_PCB = (0.02, 0.42, 0.21)
HUB_BODY = (0.13, 0.13, 0.15)
TIE = (0.93, 0.93, 0.88)
CABLE = (0.16, 0.16, 0.18)
UPLINK = (0.16, 0.30, 0.65)
LED = (0.25, 0.95, 0.35)

TILE_ESP = load_stl(STL / "esp32-board-tile.stl")
TILE_PI = load_stl(STL / "pi5-tile.stl")
TILE_HUB = load_stl(STL / "hub-strap-tile.stl")


def esp32_node(scene, ox, oy, label_pts):
    """Board tile + devkit + two zip ties at plank position (ox, oy)."""
    scene.append((translate(TILE_ESP, ox, oy, 0), TILE))
    t = lambda g: translate(g, ox, oy, 0)
    scene.append((t(box(30, 16, 3.2, 83, 44, 4.8)), PCB_DARK))     # PCB
    scene.append((t(box(62, 21, 4.8, 77, 39, 7.9)), SHIELD))       # module can
    scene.append((t(box(77, 21, 4.8, 83, 39, 6.2)), PCB_DARK))     # antenna
    scene.append((t(box(31, 26.5, 4.8, 38, 33.5, 7.6)), SHIELD))   # micro-USB
    scene.append((t(box(40, 17.5, 4.8, 44, 21.5, 6.8)), HUB_BODY)) # button
    scene.append((t(box(40, 38.5, 4.8, 44, 42.5, 6.8)), HUB_BODY)) # button
    for cx in (40, 62):                                            # zip ties
        scene.append((t(box(cx - 2.5, 8, 3.2, cx + 2.5, 11, 9.2)), TIE))
        scene.append((t(box(cx - 2.5, 49, 3.2, cx + 2.5, 52, 9.2)), TIE))
        scene.append((t(box(cx - 2.5, 8, 9.2, cx + 2.5, 52, 10.4)), TIE))
    label_pts["usb"] = np.array([ox + 31, oy + 30, 7])
    label_pts["antenna"] = np.array([ox + 83, oy + 30, 6])


def pi_node(scene, ox, oy):
    scene.append((translate(TILE_PI, ox, oy, 0), TILE))
    t = lambda g: translate(g, ox, oy, 0)
    scene.append((t(box(9.5, 13, 9.2, 94.5, 69, 10.8)), GREEN_PCB))  # Pi PCB
    scene.append((t(box(88, 18, 10.8, 102, 31, 26)), SHIELD))        # USB stack
    scene.append((t(box(88, 35, 10.8, 102, 48, 26)), SHIELD))        # USB stack
    scene.append((t(box(88, 52, 10.8, 100, 65, 24)), HUB_BODY))      # Ethernet
    scene.append((t(box(14, 62, 10.8, 65, 67, 19)), HUB_BODY))       # GPIO
    scene.append((t(box(40, 30, 10.8, 55, 45, 13)), SHIELD))         # SoC
    return np.array([ox + 95, oy + 24, 18])                          # USB anchor


def hub_node(scene, ox, oy):
    scene.append((translate(TILE_HUB, ox, oy, 0), TILE))
    t = lambda g: translate(g, ox, oy, 0)
    scene.append((t(box(5, 14, 3.2, 105, 56, 27)), HUB_BODY))        # body
    ports = []
    for i in range(7):
        px = 12 + i * 13
        scene.append((t(box(px, 12.6, 8, px + 9, 14.5, 15)), (0.03,) * 3))
        scene.append((t(box(px + 3, 12.6, 17, px + 6, 14.5, 19)), LED))
        ports.append(np.array([ox + px + 4.5, oy + 13, 11]))
    for cx in (20, 55, 90):                                          # straps
        scene.append((t(box(cx - 2.5, 7, 3.2, cx + 2.5, 10, 28)), TIE))
        scene.append((t(box(cx - 2.5, 60, 3.2, cx + 2.5, 63, 28)), TIE))
        scene.append((t(box(cx - 2.5, 7, 28, cx + 2.5, 63, 29.2)), TIE))
    uplink = np.array([ox + 2, oy + 35, 15])
    return ports, uplink


def build_rig():
    """The full MVP rig on a 1.2 m plank. Returns scene + label anchors."""
    scene, labels = [], {}
    scene.append((box(0, 0, -18, 1200, 200, 0), WOOD))               # plank
    pi_usb = pi_node(scene, 22, 62)
    labels["pi"] = np.array([75, 100, 26])
    ports, uplink = hub_node(scene, 150, 66)
    labels["hub"] = np.array([205, 100, 29])
    node_x = [360, 720, 1080]
    for i, bx in enumerate(node_x):
        pts = {}
        esp32_node(scene, bx, 70, pts)
        labels[f"esp{i}"] = pts["usb"] + np.array([25, 0, 4])
        # USB cable: hub port -> front lane -> board's micro-USB
        p0 = ports[i]
        p3 = pts["usb"] + np.array([-4, 0, 0])
        p1 = p0 + np.array([15, -55, -6])
        p2 = p3 + np.array([-120, -45, -4])
        scene.append((bezier_tube(p0, p1, p2, p3), CABLE))
    scene.append(  # hub uplink to the Pi's USB stack
        (bezier_tube(pi_usb, pi_usb + np.array([35, -35, 2]),
                     uplink + np.array([-25, -40, 4]), uplink, r=2.2), UPLINK)
    )
    labels["cable"] = np.array([560, 55, 4])
    return scene, labels


def save(path, img, view=None, notes=(), title=None, footer=None):
    h, w, _ = img.shape
    fig = plt.figure(figsize=(w / 100, h / 100), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.imshow(img)
    ax.set_axis_off()
    style = dict(
        fontsize=15, color="#111111", family="DejaVu Sans",
        bbox=dict(boxstyle="round,pad=0.45", fc="#ffffff", ec="#88888880"),
    )
    for text, world, tx, ty in notes:
        px, py = view.project(world)[0]
        ax.annotate(
            text, (px, py), (tx, ty),
            arrowprops=dict(arrowstyle="-", color="#333333", lw=1.4),
            **style,
        )
    if title:
        ax.text(0.012, 0.975, title, transform=ax.transAxes, fontsize=21,
                weight="bold", va="top", color="#111111")
    if footer:
        ax.text(0.012, 0.03, footer, transform=ax.transAxes, fontsize=13.5,
                va="bottom", color="#222222",
                bbox=dict(boxstyle="round,pad=0.5", fc="#ffffffd0",
                          ec="#88888880"))
    fig.savefig(HERE / path)
    plt.close(fig)
    print(path)


def main():
    scene, labels = build_rig()

    # 1 — annotated overview
    view = View((300, -880, 560), (612, 110, -30), fovy=37)
    img = render(scene, view)
    save(
        "rig-overview.png", img, view,
        notes=[
            ("Raspberry Pi 4/5 — self-hosted runner:\nesptool flash + pytest + serial capture",
             labels["pi"], 60, 110),
            ("Powered USB hub (uhubctl)\nper-port power on/off = power HAL",
             labels["hub"], 330, 240),
            ("ESP32 node 01 on printed tile\n(zip-tied, antenna overhangs)",
             labels["esp0"], 620, 140),
            ("One USB cable per board:\nflash + serial + power",
             labels["cable"], 830, 620),
            ("nodes at ≥0.5 m pitch (RF)", labels["esp2"], 1240, 300),
        ],
        title="Alteriom ESP32 HIL rig — MVP (3 nodes, wired for 6)",
        footer="Flow: PR labelled run-hil → GitHub Actions (self-hosted) → flash agent firmware\n"
               "→ pytest drives JSON serial protocol → pass/fail on the PR + serial-log artifacts",
    )

    # 2 — control end: Pi + hub
    view = View((30, -420, 330), (170, 110, 10), fovy=30)
    save("rig-detail-control.png", render(scene, view), view,
         notes=[
             ("pi5-tile.stl — 6 mm standoff bosses", labels["pi"] - np.array([40, 30, 20]), 90, 780),
             ("hub-strap-tile.stl — zip-strapped hub", np.array([260, 100, 20]), 950, 180),
             ("per-port power LEDs", np.array([230, 80, 19]), 1050, 620),
         ],
         title="Control end — runner + switchable hub")

    # 3 — node close-up
    view = View((310, -240, 205), (408, 96, 0), fovy=31)
    save("rig-detail-node.png", render(scene, view), view,
         notes=[
             ("29 mm channel locates any devkit clone", np.array([395, 86, 5]), 120, 750),
             ("zip tie through tile,\ngroove underneath", np.array([422, 121, 10]), 990, 160),
             ("antenna end overhangs the tile", np.array([445, 100, 6]), 1050, 560),
         ],
         title="ESP32 node — esp32-board-tile.stl")

    # 4 — the three printable tiles
    tiles = []
    tiles.append((translate(TILE_ESP, 0, 20, 0), TILE))
    tiles.append((translate(TILE_PI, 110, 0, 0), TILE))
    tiles.append((translate(TILE_HUB, 244, 6, 0), TILE))
    tiles.append((box(-40, -40, -10, 400, 130, 0), (0.88, 0.88, 0.90)))
    view = View((150, -330, 300), (172, 45, -5), fovy=30)
    save("tiles-printset.png", render(tiles, view), view,
         notes=[
             ("esp32-board-tile", np.array([40, 25, 4]), 150, 800),
             ("pi5-tile", np.array([162, 40, 8]), 700, 830),
             ("hub-strap-tile", np.array([300, 40, 3]), 1150, 800),
         ],
         title="Printable mount set (hardware/mounts/stl)")


if __name__ == "__main__":
    main()
