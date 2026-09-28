"""After the demo rig's service has started: what a start undoes, put back.

A standalone service runs a discovery when it starts, which on a host with no
boards publishes an empty snapshot (and a discovery run saying so), and it marks any run left `running` as
interrupted. This puts the demo's board snapshot back and starts one run that
is visibly in progress, so the overview has a live pipeline to show.

    STATE=/tmp/demo python runner/screenshots/demo_after_start.py
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import demo_rig
from alteriom_hil.jobstore import JobStore

state = Path(os.environ.get("STATE") or "/var/lib/alteriom-hil")
saved = state / "inventory.demo.json"
if saved.is_file():
    shutil.copyfile(saved, state / "inventory.json")
demo_rig.STATE = state
store = JobStore(state / "farm.sqlite3")
# The discovery the start ran found no boards (there are none here): not the demo's story.
with store.connect() as db:
    db.execute("DELETE FROM jobs WHERE kind='inventory'")
demo_rig.run(store, "rig-example", "running", 0.05, by="ci", sha=demo_rig.SHAS[1],
             boards=("esp32-01", "esp32-c3-01"), passed=0, failed=0)
print("demo rig: snapshot restored, one run in progress")
