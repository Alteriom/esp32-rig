#!/usr/bin/env python3
"""Build chassis-viewer.html: an interactive 3D view of the assembled rig.

Embeds every unique chassis STL once (vertices as 0.1 mm int16, base64)
and instances them at the placements render_chassis.build() records, plus
the context meshes (plank, Pi, hub, relay module, brick, devkits, cables)
merged by colour. Open the file in any browser; three.js is loaded from
cdnjs, everything else is inline. Not part of CI.

    python3 build_viewer.py            # writes ./chassis-viewer.html
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
    "relay-tray": ("Relay tray", "slotted M3 bosses, 158 × 69 mm"),
    "deck-wall-long": ("Deck wall, front", "159 × 50 mm, screw flange inside"),
    "deck-wall-rear": ("Deck wall, rear", "159 mm, one 20 × 20 cable notch per slot"),
    "deck-wall-end": ("Deck end wall", "178 mm, two 32 × 22 cable entries"),
    "deck-lid": ("Deck lid", "165 × 190 mm vented half, drop-in rails"),
    "psu-cradle-side": ("PSU cradle, on edge", "brick standing on its long edge, rails + straps"),
    "rack-4slot": ("Card rack, 4 slots", "fin + strap notches + junction pocket + socket clamp, 40 mm pitch"),
    "rack-pocket-lid": ("Rack pocket lid", "friction-fit, notch for the male pigtail"),
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
    "views": rc.VIEWS,
    "title": "Rig Chassis",
    "dims": "base <b>330 × 272 mm</b> · deck <b>330 × 190 × 50</b> · boards <b>40 mm pitch</b>",
    "layout": [["0–330", "0–190", "control deck"], ["16–174", "16–85", "relay tray"],
               ["190–300", "16–86", "hub strap tile, ports to the rear"], ["16–120", "90–172", "Pi 5 tile"],
               ["190–306", "100–142", "PSU cradle, brick on edge"], ["6–166 / 165–325", "192–272", "rack-4slot × 2"],
               ["26 + 40·n", "—", "board slot n = 0…7"]],
}

TEMPLATE = (HERE / "viewer_template.html").read_text(encoding="utf-8")
html = TEMPLATE.replace("/*__DATA__*/", "const DATA = " + json.dumps(data, separators=(",", ":")) + ";")
rows = "".join(f"<tr><td class=\"num\">{a}</td><td class=\"num\">{b}</td><td>{c}</td></tr>" for a, b, c in data["layout"])
start = html.index("<tr><th>x</th>"); end = html.index("</table>", start)
html = html[:start] + "<tr><th>x</th><th>y</th><th>part</th></tr>" + rows + html[end:]
html = html.replace(">Control bay</button>", ">Control deck</button>").replace(">Board station</button>", ">Card rack</button>")
OUT.write_text(html, encoding="utf-8")
print(f"{OUT.name}: {len(parts)} parts, {len(placements)} placements, "
      f"{sum(len(t) for t, _ in context)} context triangles, {OUT.stat().st_size // 1024} KiB")
