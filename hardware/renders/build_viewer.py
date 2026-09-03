#!/usr/bin/env python3
"""Build chassis-viewer.html: an interactive 3D view of the assembled rig.

Embeds every unique chassis STL once (vertices as 0.1 mm int16, base64)
and instances them at the placements render_chassis.build() records, plus
the context meshes (plank, Pi, hub, relay module, brick, devkits, cables)
merged by colour. Open the file in any browser; three.js is loaded from
cdnjs, everything else is inline. Not part of CI.

    python3 build_viewer.py            # linear rig  -> chassis-viewer.html
    python3 build_viewer.py compact    # compact rig -> compact-viewer.html
"""

import base64
import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np

import render_chassis as rc

HERE = Path(__file__).parent
COMPACT = len(sys.argv) > 1 and sys.argv[1] == "compact"
if COMPACT:
    import render_compact as assembly
else:
    assembly = rc
OUT = HERE / ("compact-viewer.html" if COMPACT else "chassis-viewer.html")


def pack(tris):
    """(N,3,3) float mm -> base64 of int16 in 0.1 mm."""
    q = np.rint(np.asarray(tris, dtype=np.float64).reshape(-1) * 10).astype("<i2")
    return base64.b64encode(q.tobytes()).decode("ascii")


# 1. placements only (STLs are not appended to the scene here)
real_place = rc.place


def record_only(scene, name, color, dx, dy, dz=0, k=0):
    rc.PLACED.append((name, dx, dy, dz, k))


rc.place = record_only
if COMPACT:
    assembly.place = record_only
context = assembly.build(lids=True, pocket_lids=True)
rc.place = real_place
if COMPACT:
    assembly.place = real_place
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
    "stations": rc.STATION_X,
    "compact": COMPACT,
    "views": ({"overview": {"eye": [-260, -620, 520], "target": [165, 140, 10]},
               "bay": {"eye": [60, -520, 470], "target": [165, 120, 0]},
               "station": {"eye": [-120, 40, 250], "target": [95, 240, 50]}}
              if COMPACT else
              {"overview": {"eye": [150, -1250, 780], "target": [600, 90, -60]},
               "bay": {"eye": [0, -560, 440], "target": [178, 105, 0]},
               "station": {"eye": [rc.STATION_X[0] - 60, -200, 190], "target": [rc.STATION_X[0] + 62, 95, 4]}}),
    "title": "Compact Rig Chassis" if COMPACT else "Rig Chassis",
    "lede": ("Boards upright in a card rack behind the control deck: 330 × 272 mm, eight ports, every printed part under 165 mm."
             if COMPACT else
             "Enclosed, relay-switched, eight rig ports on a 1.2 m plank. Every printed part here is the real STL at its assembly position."),
    "dims": ("base <b>330 × 272 mm</b> · deck <b>330 × 190 × 50</b> · boards <b>40 mm pitch</b>" if COMPACT
             else "plank <b>1200 × 200 mm</b> · bay <b>300 × 200 × 45</b>"),
    "layout": ([["0–330", "0–190", "control deck"], ["16–174", "16–85", "relay tray"], ["190–300", "16–86", "hub strap tile, ports to the rear"],
                ["16–120", "90–172", "Pi 5 tile"], ["190–306", "100–142", "PSU cradle, brick on edge"],
                ["6–166 / 165–325", "192–272", "rack-4slot × 2"], ["26 + 40·n", "—", "board slot n = 0…7"]]
               if COMPACT else
               [["0–300", "0–200", "control bay"], ["16–120", "100–182", "Pi 5 tile"], ["16–126", "22–92", "hub strap tile"],
                ["126–284", "116–185", "relay tray"], ["150–270", "30–95", "PSU cradle"], ["280–1180", "16–50", "cable channel"],
                ["320 + 145·n", "60–130", "board station n = 0…5"]]),
}

TEMPLATE = (HERE / "viewer_template.html").read_text(encoding="utf-8")
html = TEMPLATE.replace("/*__DATA__*/", "const DATA = " + json.dumps(data, separators=(",", ":")) + ";")
html = html.replace("<title>Alteriom Rig Chassis</title>", f"<title>Alteriom {data['title']}</title>")
html = html.replace("<h1>Rig Chassis</h1>", f"<h1>{data['title']}</h1>")
html = html.replace(
    "<p class=\"lede\">Enclosed, relay-switched, eight rig ports on a 1.2 m plank. Every printed part here is the real STL at its assembly position.</p>",
    f"<p class=\"lede\">{data['lede']}</p>")
rows = "".join(f"<tr><td class=\"num\">{a}</td><td class=\"num\">{b}</td><td>{c}</td></tr>" for a, b, c in data["layout"])
start = html.index("<tr><th>x</th>"); end = html.index("</table>", start)
html = html[:start] + "<tr><th>x</th><th>y</th><th>part</th></tr>" + rows + html[end:]
if COMPACT:
    html = html.replace(">Board station</button>", ">Card rack</button>")
OUT.write_text(html, encoding="utf-8")
print(f"{OUT.name}: {len(parts)} parts, {len(placements)} placements, "
      f"{sum(len(t) for t, _ in context)} context triangles, {OUT.stat().st_size // 1024} KiB")
