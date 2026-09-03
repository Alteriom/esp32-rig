#!/usr/bin/env python3
"""Build chassis-viewer.html: an interactive 3D view of the assembled rig.

Embeds every unique chassis STL once (vertices as 0.1 mm int16, base64)
and instances them at the placements render_chassis.build() records, plus
the context meshes (plank, Pi, hub, relay module, brick, devkits, cables)
merged by colour. Open the file in any browser; three.js is loaded from
cdnjs, everything else is inline. Not part of CI.

    python3 build_viewer.py          # writes ./chassis-viewer.html
"""

import base64
import json
from collections import Counter
from pathlib import Path

import numpy as np

import render_chassis as rc

HERE = Path(__file__).parent
OUT = HERE / "chassis-viewer.html"


def pack(tris):
    """(N,3,3) float mm -> base64 of int16 in 0.1 mm."""
    q = np.rint(np.asarray(tris, dtype=np.float64).reshape(-1) * 10).astype("<i2")
    return base64.b64encode(q.tobytes()).decode("ascii")


# 1. placements only (STLs are not appended to the scene here)
real_place = rc.place


def record_only(scene, name, color, dx, dy, dz=0, k=0):
    rc.PLACED.append((name, dx, dy, dz, k))


rc.place = record_only
context = rc.build(lids=True, pocket_lids=True)
rc.place = real_place
placements = list(rc.PLACED)

# 2. unique parts
parts = {name: pack(rc.P[name]) for name in sorted({p[0] for p in placements})}
counts = Counter(p[0] for p in placements)

# 3. context meshes merged by colour
by_color = {}
for tris, color in context:
    by_color.setdefault(tuple(round(c, 3) for c in color), []).append(tris)
context_meshes = [
    {"color": list(c), "kind": "plank" if c == tuple(round(v, 3) for v in rc.rr.WOOD) else "context",
     "data": pack(np.concatenate(ts))}
    for c, ts in by_color.items()
]

PART_INFO = {
    "bay-corner-post": ("Corner post", "12 × 12 × 45 mm, two 6 mm slots, screw foot"),
    "bay-splice-post": ("Splice post", "in-line post at the bay midline"),
    "bay-wall-long": ("Long wall", "144 × 45 mm, 10 mm screw flange inside"),
    "bay-wall-short-outer": ("End wall, outer", "188 mm, two 32 × 22 cable entries"),
    "bay-wall-short-inner": ("End wall, inner", "188 mm, 36 × 28 channel port"),
    "bay-lid": ("Bay lid", "150 × 200 mm vented half, drop-in rails"),
    "relay-tray": ("Relay tray", "slotted M3 bosses, 158 × 69 mm"),
    "psu-cradle": ("PSU cradle", "12 V brick, end stops + two straps"),
    "cable-channel-200": ("Cable channel", "200 mm, 34 × 24, notches every 50"),
    "cable-channel-100": ("Cable channel, half", "100 mm"),
    "cable-channel-lid-200": ("Channel lid", "200 mm press-fit"),
    "cable-channel-lid-100": ("Channel lid, half", "100 mm press-fit"),
    "board-station": ("Board station", "socket clamp + junction pocket + devkit rails"),
    "station-pocket-lid": ("Pocket lid", "friction-fit over the wire nuts"),
    "pi5-tile": ("Pi 5 tile", "58 × 49 pattern on 6 mm bosses"),
    "hub-strap-tile": ("Hub strap tile", "three zip-tie stations"),
}

data = {
    "parts": [
        {"name": n, "label": PART_INFO.get(n, (n, ""))[0], "spec": PART_INFO.get(n, (n, ""))[1],
         "qty": counts[n], "lid": "lid" in n, "data": parts[n]}
        for n in sorted(parts, key=lambda n: list(PART_INFO).index(n) if n in PART_INFO else 99)
    ],
    "placements": [{"name": n, "x": x, "y": y, "z": z, "k": k} for n, x, y, z, k in placements],
    "context": context_meshes,
    "stations": rc.STATION_X,
}

TEMPLATE = (HERE / "viewer_template.html").read_text(encoding="utf-8")
html = TEMPLATE.replace("/*__DATA__*/", "const DATA = " + json.dumps(data, separators=(",", ":")) + ";")
OUT.write_text(html, encoding="utf-8")
print(f"{OUT.name}: {len(parts)} parts, {len(placements)} placements, "
      f"{sum(len(t) for t, _ in context)} context triangles, {OUT.stat().st_size // 1024} KiB")
