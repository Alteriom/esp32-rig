"""A demo rig's state: what the guide's screenshots are taken of.

Writes, into STATE (default /var/lib/alteriom-hil), what a rig with four
boards and a week of work would hold -- a board registry and the snapshot its
last discovery published, the boards' health check verdicts, the host's own
health, and a job history (the admin key, "farm", runs the health check; "ci" and a person
run the rest) across the example project, the health check and
painlessMesh, with each finished run's report, results and metrics. Nothing
in it is anybody's: the MACs are locally administered (02:...), the names are
made up, and the runs never ran.

    STATE=/tmp/demo python runner/screenshots/demo_rig.py

Then start the service on that state (runner/screenshots/README.md).
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alteriom_hil.jobstore import JobStore

STATE = Path(os.environ.get("STATE") or "/var/lib/alteriom-hil")
NOW = datetime.now(timezone.utc)
BOARDS = [
    {"id": "esp32-01", "chip": "esp32", "target": "esp32", "port": "/dev/ttyUSB0", "mac": "02:00:00:00:00:01"},
    {"id": "esp32-02", "chip": "esp32", "target": "esp32", "port": "/dev/ttyUSB1", "mac": "02:00:00:00:00:02"},
    {"id": "esp32-c3-01", "chip": "esp32c3", "target": "esp32-c3", "port": "/dev/ttyACM0", "mac": "02:00:00:00:00:03"},
    {"id": "esp8266-01", "chip": "esp8266", "target": "esp8266", "port": "/dev/ttyUSB2", "mac": "02:00:00:00:00:04"},
]
SHAS = ["3f9a1c2e" + "0" * 32, "7b21d4e0" + "1" * 32, "c0ffee42" + "2" * 32, "a11ce5ed" + "3" * 32]
CHECKS = ["test_the_board_boots_and_says_what_it_is", "test_the_serial_path_is_clean",
          "test_flash_keeps_a_value_across_a_reset", "test_the_rig_can_reset_it",
          "test_the_radio_sees_the_rigs_network"]


def iso(delta: timedelta) -> str:
    return (NOW - delta).isoformat()


def write_boards() -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    registry = ["# A demo rig's boards (made up; locally administered MACs).", "boards:"]
    for board in BOARDS:
        registry += [f"  - id: {board['id']}", f"    mac: \"{board['mac']}\"", f"    port: {board['port']}",
                     f"    chip: {board['chip']}", f"    target: {board['target']}", "    tags: [demo]"]
    (STATE / "inventory.yaml").write_text("\n".join(registry) + "\n", encoding="utf-8")
    snapshot = {"boards": [{**board, "baud": 115200, "flash_baud": 921600, "power_hub": None, "power_port": None,
                            "tags": ["demo"], "usb_path": f"1-1.{n + 1}"} for n, board in enumerate(BOARDS)],
                "missing": [], "unregistered": [], "instruments": [], "missing_instruments": [], "probe_errors": [],
                "updated_at": iso(timedelta(minutes=12))}
    (STATE / "inventory.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    health = {}
    for n, board in enumerate(BOARDS):
        failed = ["test_the_radio_sees_the_rigs_network"] if board["id"] == "esp8266-01" else []
        health[board["id"]] = {
            "id": board["id"], "mac": board["mac"], "checked_at": iso(timedelta(hours=3, minutes=n)),
            "canary_version": "1.0.7", "canary_revision": "0aa7b2e1" + "0" * 32, "job_id": None,
            "verdict": "failed" if failed else "passed", "failed": failed, "farm_wide": [],
            "checks": {name: ("failed" if name in failed else "passed") for name in CHECKS},
        }
    (STATE / "board-health.json").write_text(json.dumps(health, indent=2), encoding="utf-8")
    # What the rig read off each chip the first time it flashed it.
    chips = {"esp32": ("ESP32-D0WD-V3 (revision v3.1)", "4MB", "CP2102"),
             "esp32c3": ("ESP32-C3 (QFN32) (revision v0.4)", "4MB", "USB-Serial/JTAG"),
             "esp8266": ("ESP8266EX", "4MB", "CH340")}
    (STATE / "chip-details.json").write_text(json.dumps({
        board["mac"].lower(): {"description": chips[board["chip"]][0], "flash_size": chips[board["chip"]][1],
                               "transport": chips[board["chip"]][2]} for board in BOARDS}, indent=2), encoding="utf-8")
    (STATE / "status.json").write_text(json.dumps({
        "schema": 1, "hostname": "demo-rig", "status": "ok", "timestamp": iso(timedelta(minutes=2)),
        "checks": [{"name": name, "status": "ok", "message": message} for name, message in (
            ("mode", "ALTERIOM_HIL_MODE=hardware"), ("disk", "92 GB free"), ("power", "5.1 V, no undervoltage"),
            ("usb", "4 boards on the bus"), ("backup", "last backup 6 h ago"))],
    }, indent=2), encoding="utf-8")


def run(store: JobStore, profile: str, status: str, hours_ago: float, *, by: str, sha: str, branch: str = "main",
        boards=("esp32-01", "esp32-02"), passed: int = 6, failed: int = 0, minutes: float = 4.5) -> str:
    targets = sorted({next(b["target"] for b in BOARDS if b["id"] == board) for board in boards})
    request = {"profile": profile, "ref": sha, "resolved_sha": sha, "branch": branch, "targets": targets,
               "submitted_by": by, "project": profile}
    stages = [("build", "Firmware bundle", "hardware", f"Supplied by {profile} CI"),
              ("discover", "Discover hardware", "hardware", f"{len(boards)} of {len(BOARDS)} boards allocated"),
              ("flash", "Flash", "hardware", f"Flashed {len(boards)} boards"),
              ("test", "Validation suite", "suite", "Validation suite passed" if status == "passed" else f"{failed} failed"),
              ("report", "Report", "suite", "Reports written")]
    created = timedelta(hours=hours_ago)
    # How long each stage takes on a rig like this: the flash and the suite are the run.
    took = [0.1, 0.08, 0.3 * len(boards), max(minutes - 0.2 - 0.3 * len(boards), 0.5), 0.02]
    progress = []
    at = 0.2
    for n, (name, label, group, summary) in enumerate(stages):
        state = "passed" if status == "passed" or name != "test" else "failed"
        if status == "running":
            state = "passed" if n < 3 else "running" if n == 3 else "pending"
        progress.append({"name": name, "label": label, "group": group, "status": state,
                         "summary": summary if state != "pending" else "",
                         "started_at": iso(created - timedelta(minutes=at)) if state != "pending" else None,
                         "finished_at": iso(created - timedelta(minutes=at + took[n])) if state in ("passed", "failed") else None})
        at += took[n]
    logs = STATE / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    job = store.create("suite", request, logs / "pending.log", progress)
    jid = job["id"]
    log = logs / f"{jid}.log"
    log.write_text(f"Fetching {profile} at {sha[:12]}\n" + "".join(f"flashing {board}: ok\n" for board in boards)
                   + f"== {passed} passed, {failed} failed ==\n", encoding="utf-8")
    with store.connect() as db:
        db.execute("UPDATE jobs SET log_path=?, created_at=?, started_at=?, worker=? WHERE id=?",
                   (str(log), iso(created), iso(created - timedelta(minutes=0.2)), "local", jid))
    if status == "running":
        with store.connect() as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (jid,))
        return jid
    evidence = STATE / "runs" / jid
    (evidence / "metrics").mkdir(parents=True, exist_ok=True)
    (evidence / "serial").mkdir(parents=True, exist_ok=True)
    cases = "".join(f'<testcase classname="tests" name="test_{i}[{board}]" time="0.8"/>'
                    for i in range(passed // len(boards) or 1) for board in boards)
    cases += "".join(f'<testcase classname="tests" name="test_sum_rejects_overflow[{boards[0]}]" time="1.2">'
                     f'<failure message="expected an error, got 0">expected an error, got 0</failure></testcase>'
                     for _ in range(failed))
    (evidence / "results.xml").write_text(f'<?xml version="1.0"?><testsuites><testsuite name="pytest" tests="{passed + failed}" '
                                          f'failures="{failed}">{cases}</testsuite></testsuites>', encoding="utf-8")
    verdict = "PASSED" if status == "passed" else "FAILED"
    (evidence / "metrics" / "report.md").write_text(
        f"# {profile} at {sha[:12]}\n\n## Headline\n\n- **Validation gate: {verdict}**.\n"
        f"- {passed + failed} tests on {len(boards)} boards: {passed} passed, {failed} failed.\n", encoding="utf-8")
    for board in boards:
        (evidence / "serial" / f"{board}.serial.log").write_text('{"evt":"boot"}\n{"evt":"info","family":"esp32"}\n', encoding="utf-8")
    wall = (minutes - 0.2) * 60   # from its start, which was 0.2 min after it was queued
    result = {"summary": "Validation suite passed" if status == "passed" else f"{failed} test(s) failed",
              "boards": len(boards), "board_ids": list(boards), "flashed": True, "profile": profile, "project": profile,
              "revision": sha, "report": str(evidence / "metrics" / "report.md"), "results": str(evidence / "results.xml"),
              "passed": passed, "failed": failed,
              "metrics": {"boards": len(boards), "wall_seconds": round(wall, 1),
                          "board_minutes": round(len(boards) * wall / 60, 2), "queue_wait_seconds": 3.0,
                          "cpu_seconds": round(wall / 9, 1), "evidence_bytes": 24_000 + 3_000 * len(boards),
                          "bundle_bytes": 1_200_000 * len(boards)}}
    write_bundle(jid, profile, sha, branch, by, targets)
    store.update(jid, status, result)
    with store.connect() as db:
        db.execute("UPDATE jobs SET finished_at=? WHERE id=?", (iso(created - timedelta(minutes=minutes)), jid))
    return jid


def write_bundle(jid: str, profile: str, sha: str, branch: str, by: str, targets: list[str]) -> None:
    """The firmware bundle a run flashed, as its project's CI handed it over:
    a schema-2 manifest, one image per family, and where it came from."""
    repo = {"canary": "esp32-hil-firmware", "rig-example": "esp32-rig-example", "painlessmesh": "painlessMesh"}[profile]
    folder = STATE / "artifacts" / jid
    manifest = {"schema": 2, "painlessmesh_sha": sha, "git_sha": sha, "farm_sha": sha, "version": None, "targets": {}}
    for family in targets:
        (folder / family).mkdir(parents=True, exist_ok=True)
        image = folder / family / "flash-image.bin"
        image.write_bytes(bytes([0xE9]) + bytes(range(256)) * (4000 + 97 * len(family)))
        manifest["targets"][family] = {"image": f"{family}/flash-image.bin", "flash_offset": "0x0",
                                       "sha256": hashlib.sha256(image.read_bytes()).hexdigest()}
    manifest.pop("version")
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (folder / "provenance.json").write_text(json.dumps({
        "repo": f"https://github.com/Alteriom/{repo}", "workflow": "hil.yml", "run_id": str(9000000 + int(jid[:4], 16)),
        "commit": sha, "branch": branch, "actor": by}), encoding="utf-8")


def write_runs() -> None:
    store = JobStore(STATE / "farm.sqlite3")
    everyone = ("esp32-01", "esp32-02", "esp32-c3-01", "esp8266-01")
    plan = [
        ("canary", "passed", 150, "farm", SHAS[0], "main", everyone, 20, 0),
        ("rig-example", "passed", 120, "ci", SHAS[0], "main", ("esp32-01", "esp32-c3-01"), 6, 0),
        ("painlessmesh", "failed", 96, "ci", SHAS[1], "feature/ota", ("esp32-01", "esp32-02", "esp8266-01"), 38, 2),
        ("painlessmesh", "passed", 70, "ci", SHAS[2], "main", ("esp32-01", "esp32-02", "esp8266-01"), 40, 0),
        ("rig-example", "passed", 49, "ci", SHAS[1], "main", ("esp32-01", "esp32-c3-01"), 6, 0),
        ("canary", "passed", 26, "farm", SHAS[0], "main", everyone, 20, 0),
        ("rig-example", "failed", 20, "maria", SHAS[3], "fix/sum", ("esp32-01",), 2, 1),
        ("rig-example", "passed", 8, "maria", SHAS[3], "fix/sum", ("esp32-01", "esp32-c3-01"), 6, 0),
        ("painlessmesh", "passed", 3, "ci", SHAS[2], "main", ("esp32-01", "esp32-02", "esp8266-01"), 40, 0),
    ]
    for profile, status, hours, by, sha, branch, boards, passed, failed in plan:
        run(store, profile, status, hours, by=by, sha=sha, branch=branch, boards=boards, passed=passed, failed=failed,
            minutes=round(1.2 + 0.7 * len(boards) + 0.05 * (passed + failed) + (hours % 7) / 10, 2))


def write_version() -> None:
    """What an installed release says about itself (ALTERIOM_HIL_VERSION_FILE)."""
    (STATE / "version.json").write_text(json.dumps({
        "version": os.environ.get("DEMO_VERSION", "1.0.196"), "base": "1.0", "commit": "d3m0" + "0" * 36, "short": "d3m0000",
        "subject": "A demo rig", "installed_at": iso(timedelta(days=1))}), encoding="utf-8")


if __name__ == "__main__":
    write_boards()
    write_runs()
    write_version()
    print(f"demo rig state written to {STATE}")
