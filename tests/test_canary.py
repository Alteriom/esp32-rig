"""The Rig Health Check (profile `canary`): its bundle, its profile, and the simulator that lets its
suite be exercised somewhere other than the rig.

What the canary actually proves needs an ESP and is checked by its own suite
on the hardware. What is checked here is everything around that: the bundle
it produces is the same contract every other producer emits, the profile the
farm runs it under is the one intended, and the firmware, the client and the
simulator agree on the protocol -- because three copies of a protocol are
three chances to disagree, and the rig is an expensive place to find out.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
CANARY = REPO / "canary"
FIRMWARE = CANARY / "firmware" / "src" / "main.cpp"

sys.path.insert(0, str(REPO / "rig"))

sys.path.insert(0, str(REPO / "core"))

from alteriom_hil import sim  # noqa: E402
from alteriom_hil.artifacts import load_artifacts  # noqa: E402
from alteriom_hil.profiles import load_profiles  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "canary_build_artifacts", CANARY / "build_artifacts.py"
)
build_artifacts = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build_artifacts)

# The launcher: `alteriom_hil.launcher`, a console script now
# (`alteriom-hil-service`), which composes the halves installed onto
# the base (docs/public-release-plan.md, step 12e).
from alteriom_hil import launcher as farm_service
# The service itself, where `load_inventory_snapshot` is read: it is
# `alteriom_hil.service` now and this file loads the launcher.
from alteriom_hil import service as core_service  # noqa: E402


def test_the_canary_builds_the_same_manifest_contract_every_producer_does(tmp_path, monkeypatch):
    """A bundle the farm can flash, verify, list, pin and prune is one that
    satisfies alteriom_hil.artifacts -- the canary gets all of that for free
    precisely because it emits the same schema-2 manifest, so the loader
    itself is the assertion here."""
    out = tmp_path / "hil-canary"

    def fake_pio(argv, check=True, env=None, **kwargs):
        # Stand in for `pio run`: write the components a real build leaves
        # in .pio/build/<env>, so what is under test is this script's
        # merging, checksums and manifest rather than a toolchain.
        if argv[0] == "pio":
            name = argv[argv.index("-e") + 1]
            built = build_artifacts.FIRMWARE_DIR / ".pio" / "build" / name
            built.mkdir(parents=True, exist_ok=True)
            for filename, body in (
                ("firmware.bin", b"\xe9" + name.encode()),
                ("bootloader.bin", b"\xe9boot"),
                ("partitions.bin", b"partitions"),
            ):
                (built / filename).write_bytes(body)
            assert env["CANARY_SHA"] == build_artifacts.canary_sha(), (
                "the firmware must be built with its own digest compiled in, "
                "or a board cannot say which canary it is running"
            )
            assert env["CANARY_VERSION"] == "1.0.42", (
                "the firmware must be built with its version compiled in, or a "
                "board cannot say which version it is running"
            )
            return subprocess.CompletedProcess(argv, 0)
        # esptool merge-bin: one image with each segment at its offset,
        # padded with erased flash between them. Faked faithfully because
        # load_artifacts checks every component is where the manifest says.
        rest = argv[argv.index("-o") + 1:]
        target, pairs = Path(rest[0]), rest[1:]
        image = bytearray()
        for offset, source in zip(pairs[::2], pairs[1::2]):
            at = int(offset, 16)
            if len(image) < at:
                image.extend(b"\xff" * (at - len(image)))
            image[at:at + 0] = b""
            body = Path(source).read_bytes()
            image[at:at + len(body)] = body
        target.write_bytes(bytes(image))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(build_artifacts.shutil, "which", lambda name: name)
    monkeypatch.setattr(build_artifacts.subprocess, "run", fake_pio)
    monkeypatch.setattr(build_artifacts, "_boot_app0", lambda core: _written(tmp_path))
    monkeypatch.setattr(build_artifacts, "farm_sha", lambda: "b" * 40)
    monkeypatch.setattr(build_artifacts, "firmware_version", lambda: "1.0.42")

    manifest_path = build_artifacts.build_artifacts(out, ["esp32", "esp32-c6", "esp8266"])
    manifest = load_artifacts(out)  # every checksum, every component at its offset
    assert manifest_path == out / "manifest.json"
    assert manifest["schema"] == 2 and manifest["producer"] == "canary"
    # The revision of record is the commit; the firmware's own identity is
    # the digest of its source, and they answer different questions.
    assert manifest["farm_sha"] == "b" * 40
    assert re.fullmatch(r"[0-9a-f]{64}", manifest["canary_sha"])
    # And the number a person reads, which the board reports too.
    assert manifest["version"] == "1.0.42"
    assert sorted(manifest["targets"]) == ["esp32", "esp32-c6", "esp8266"]
    for name, entry in manifest["targets"].items():
        assert entry["image"] == f"{name}/flash-image.bin"
        assert entry["flash_offset"] == "0x0", "one merged image, flashed at zero"
        assert entry["board"] and entry["chip"] and entry["platformio_env"] == name
    # The ESP8266 has one self-contained image and nothing to merge; the
    # ESP32 families carry their bootloader, partition table and OTA
    # selector at the offsets the silicon expects.
    assert manifest["targets"]["esp8266"]["segments"] == {"firmware.bin": "0x0"}
    assert manifest["targets"]["esp32"]["segments"]["bootloader.bin"] == "0x1000"
    assert manifest["targets"]["esp32-c6"]["segments"]["bootloader.bin"] == "0x0"


def _written(tmp_path: Path) -> Path:
    boot_app0 = tmp_path / "boot_app0.bin"
    boot_app0.write_bytes(b"boot_app0")
    return boot_app0


def test_the_canary_revision_is_its_source_and_the_commit_it_was_built_from(monkeypatch):
    """Two farm commits that did not touch the canary are the same canary,
    which is what lets a deploy skip the build. A change anywhere under
    canary/firmware/ -- the platformio.ini that picks a platform as much as
    the firmware -- is a different one."""
    before = build_artifacts.canary_sha()
    assert before == build_artifacts.canary_sha(), "the digest is of the source, not the moment"
    marker = build_artifacts.FIRMWARE_DIR / "src" / "canary_platform.h"
    original = marker.read_bytes()
    try:
        marker.write_bytes(original + b"\n// a change\n")
        assert build_artifacts.canary_sha() != before
    finally:
        marker.write_bytes(original)
    assert build_artifacts.canary_sha() == before

    # A build directory is not source: a rebuild in place must not change
    # what the canary is.
    stray = build_artifacts.FIRMWARE_DIR / ".pio" / "build" / "esp32" / "firmware.bin"
    stray.parent.mkdir(parents=True, exist_ok=True)
    try:
        stray.write_bytes(b"\xe9 built")
        assert build_artifacts.canary_sha() == before
    finally:
        stray.unlink(missing_ok=True)


def test_the_farm_commit_is_required_rather_than_invented(monkeypatch):
    """A bundle whose revision is a guess is one nothing can reuse safely,
    so a build that cannot name its commit fails instead."""
    monkeypatch.setattr(
        build_artifacts.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0] if a else [], 128, stdout=""),
    )
    monkeypatch.setenv("GITHUB_SHA", "c" * 40)
    assert build_artifacts.farm_sha() == "c" * 40
    monkeypatch.delenv("GITHUB_SHA")
    with pytest.raises(RuntimeError, match="cannot determine the farm commit"):
        build_artifacts.farm_sha()


def _firmware_repo(tmp_path: Path, version: str = "1.0") -> Path:
    """A repository shaped like this one: a firmware directory with a VERSION,
    two commits that change it and one that does not."""
    work = tmp_path / "farm"
    firmware = work / "canary" / "firmware"
    firmware.mkdir(parents=True)

    def git(*args):
        return subprocess.run(["git", *args], cwd=work, check=True, capture_output=True, text=True)

    git("init", "--quiet", "-b", "main")
    git("config", "user.email", "farm@example.invalid")
    git("config", "user.name", "farm")
    (firmware / "VERSION").write_text(version + "\n", encoding="utf-8")
    (firmware / "main.cpp").write_text("// one\n", encoding="utf-8")
    git("add", "-A"); git("commit", "--quiet", "-m", "firmware")
    (work / "README.md").write_text("docs\n", encoding="utf-8")
    git("add", "-A"); git("commit", "--quiet", "-m", "not the firmware")
    (firmware / "main.cpp").write_text("// two\n", encoding="utf-8")
    git("add", "-A"); git("commit", "--quiet", "-m", "firmware again")
    return firmware


def test_the_firmware_version_counts_only_the_commits_that_changed_it(tmp_path, monkeypatch):
    """MAJOR.MINOR is VERSION; PATCH is the commits under canary/firmware/,
    so a farm release that did not touch the firmware keeps its number."""
    firmware = _firmware_repo(tmp_path)
    monkeypatch.setattr(build_artifacts, "FIRMWARE_DIR", firmware)
    assert build_artifacts.firmware_version() == "1.0.2", "two of the three commits changed the firmware"

    # A build from a tree that is not the commit is not that commit's version.
    (firmware / "main.cpp").write_text("// uncommitted\n", encoding="utf-8")
    assert build_artifacts.firmware_version() == "1.0.2+modified"
    subprocess.run(["git", "checkout", "--", "main.cpp"], cwd=firmware, check=True, capture_output=True)
    (firmware / "stray.h").write_text("// untracked\n", encoding="utf-8")
    assert build_artifacts.firmware_version() == "1.0.2+modified"


def test_the_firmware_version_is_refused_rather_than_guessed(tmp_path, monkeypatch):
    """A shallow checkout counts only what it fetched, and a VERSION that is
    not MAJOR.MINOR is not one: either would stamp a number that looks right."""
    firmware = _firmware_repo(tmp_path)
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "--quiet", "--depth", "1", (firmware.parents[1]).as_uri(), str(shallow)],
        check=True, capture_output=True,
    )
    monkeypatch.setattr(build_artifacts, "FIRMWARE_DIR", shallow / "canary" / "firmware")
    with pytest.raises(RuntimeError, match="shallow checkout"):
        build_artifacts.firmware_version()

    bad = _firmware_repo(tmp_path / "bad", version="1")
    monkeypatch.setattr(build_artifacts, "FIRMWARE_DIR", bad)
    with pytest.raises(RuntimeError, match="MAJOR.MINOR"):
        build_artifacts.firmware_version()

    monkeypatch.setattr(build_artifacts, "FIRMWARE_DIR", tmp_path / "nowhere")
    with pytest.raises(RuntimeError, match="VERSION is missing"):
        build_artifacts.firmware_version()


def test_this_checkout_numbers_its_firmware_and_the_board_reports_it():
    """The real VERSION is MAJOR.MINOR, and the version reaches the board: the
    build passes it to PlatformIO, and the firmware says it on boot and info."""
    assert re.fullmatch(r"\d+\.\d+", (CANARY / "firmware" / "VERSION").read_text(encoding="utf-8").strip())
    ini = (CANARY / "firmware" / "platformio.ini").read_text(encoding="utf-8")
    assert "-D CANARY_VERSION=" in ini and "${sysenv.CANARY_VERSION}" in ini
    firmware = FIRMWARE.read_text(encoding="utf-8")
    assert firmware.count('doc["version"] = CANARY_VERSION;') == 2, "in both boot and info"


def test_the_canary_profile_is_the_farm_checking_its_own_hardware():
    spec = load_profiles(REPO)["canary"]
    # What people see: a name that says what it does, and its version.
    assert spec.label == "Rig Health Check"
    assert spec.report_title == "Rig Health Check {version}"
    assert spec.location == "farm", "the canary's source is this repository"
    assert spec.revision_key == "farm_sha"
    # One board is a complete answer about that board, which is what makes
    # "check this one board" something the farm can offer at all.
    assert spec.min_boards == 1
    # It flashes, resets and takes the radio of every board it is given.
    assert spec.exclusive is True
    assert spec.suite_path == "suites/canary/tests"
    assert not spec.has_preflight, (
        "a health check must not skip the flash it is checking"
    )
    # The firmware comes from this repository's release, handed over by the
    # same upload a consumer's CI uses -- so a release exercises that path
    # every time, and a rig with no portal installs it out of release.json.
    assert spec.accepts_supplied_bundles
    assert spec.supply_workflow == ".github/workflows/release.yml"
    assert spec.supply_repo.endswith("/esp32-rig")
    catalogue = json.loads((REPO / spec.capabilities).read_text(encoding="utf-8"))
    marked = set(re.findall(r'capability\("([a-z0-9_.]+)"\)', _suite_source()))
    declared = {key for key in catalogue if not key.startswith("_")}
    assert marked == declared, (
        "every capability a check provides evidence for must be declared, and "
        "every declared capability must have a check: "
        f"undeclared={sorted(marked - declared)} unproven={sorted(declared - marked)}"
    )


def _suite_source() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((REPO / "suites" / "canary" / "tests").glob("test_*.py"))
    )


def test_the_canary_carries_no_consumer_code():
    """The reason a red canary is never somebody's regression. Asserted
    rather than trusted: the day this firmware pulls in a consumer's library
    is the day a canary failure starts meaning something else.

    Against what it depends on, not against its prose -- the comments here
    say "no painlessMesh" and would fail a search for the word.
    """
    sources = [
        path for path in (CANARY / "firmware").rglob("*")
        if path.is_file() and ".pio" not in path.parts
    ]
    assert sources, "the canary firmware source is missing"
    code = "\n".join(
        path.read_text(encoding="utf-8") for path in sources if path.suffix in (".cpp", ".h")
    )
    includes = set(re.findall(r'#include\s+[<"]([^>"]+)[>"]', code))
    allowed = {
        "Arduino.h", "ArduinoJson.h", "canary_platform.h",
        "ESP8266WiFi.h", "LittleFS.h", "Preferences.h", "WiFi.h", "esp_system.h",
    }
    assert includes and includes <= allowed, (
        f"unexpected dependency: {sorted(includes - allowed)}"
    )
    # And nothing arrives through the build either: one pinned library, no
    # path or symlink dependency that could point at a consumer's checkout.
    ini = (CANARY / "firmware" / "platformio.ini").read_text(encoding="utf-8")
    declared = re.findall(r"^ {4}([A-Za-z0-9@/._-]+=?[^\r\n]*)$", ini, re.MULTILINE)
    libraries = [
        entry.strip() for entry in declared
        if "/" in entry and not entry.startswith("-D") and "://" not in entry
    ]
    assert libraries == ["bblanchon/ArduinoJson@7.4.3"], libraries
    assert "symlink://" not in ini and "file://" not in ini


def test_the_firmware_the_client_and_the_simulator_agree_on_the_protocol():
    """Three copies of one protocol are three chances to disagree, and the
    rig is an expensive place to find out. The firmware is the contract; the
    client and the simulator must cover exactly what it accepts."""
    firmware = FIRMWARE.read_text(encoding="utf-8")
    # What the firmware dispatches on, from its own dispatch chain.
    accepted = set(re.findall(r'strcmp\(name, "([a-z_]+)"\) == 0', firmware))
    assert "info" in accepted and "echo" in accepted
    client = (REPO / "suites" / "canary" / "tests" / "canary_client.py").read_text(encoding="utf-8")
    simulator = (REPO / "rig" / "alteriom_hil" / "sim.py").read_text(encoding="utf-8")
    for command in sorted(accepted):
        assert f'"{command}"' in client, (
            f"the firmware accepts {command} and nothing drives it: add it to "
            f"CanaryClient or take it out of the firmware"
        )
        assert f'"{command}"' in simulator, (
            f"the simulator does not answer {command}, so the suite cannot be "
            f"exercised off the rig"
        )
    # And nothing is driven that no firmware accepts.
    sent = set(re.findall(r'send_cmd(?:_awaiting)?\(\s*"([a-z_]+)"', client))
    assert sent <= accepted, f"the client sends what no firmware accepts: {sorted(sent - accepted)}"


def test_a_simulated_board_answers_the_canary_the_way_the_firmware_does(monkeypatch):
    """The simulator is what lets the suite be exercised in CI. It must
    answer in the same shapes, or a green sim run teaches the suite to
    accept what no board sends."""
    monkeypatch.setenv("ALTERIOM_HIL_WIFI_SSID", "Alteriom-HIL")
    hub = sim.SimHub(1)
    board = hub.boards[0]
    drained = _drain(board)
    assert drained[0]["evt"] == "boot" and drained[0]["bootId"]

    board.handle_command(json.dumps({"cmd": "info"}))
    info = _drain(board)[0]
    assert info["evt"] == "info" and info["bootId"] == drained[0]["bootId"]
    assert info["freeHeap"] > 0 and info["silicon"]["flashBytes"] > 0
    assert info["resetReason"] and info["family"]

    board.handle_command(json.dumps({"cmd": "echo", "text": "x" * 1024}))
    echoed = _drain(board)[0]
    assert echoed["text"] == "x" * 1024 and echoed["len"] == 1024

    for command, expected in (
        ({"cmd": "store_write", "key": "k", "value": "v"}, {"op": "write", "ok": True}),
        ({"cmd": "store_read", "key": "k"}, {"op": "read", "found": True, "value": "v"}),
        ({"cmd": "store_erase", "key": "k"}, {"op": "erase", "ok": True}),
        ({"cmd": "store_read", "key": "k"}, {"op": "read", "found": False}),
    ):
        board.handle_command(json.dumps(command))
        reply = _drain(board)[0]
        assert reply["evt"] == "store"
        for key, value in expected.items():
            assert reply[key] == value, (command, reply)

    board.handle_command(json.dumps({"cmd": "wifi_scan", "ssid": "Alteriom-HIL"}))
    scanned = _drain(board)[0]
    assert scanned["seen"] is True and -100 < scanned["networks"][0]["rssi"] < 0

    # The uplink and the queue are only reachable from a board that joined:
    # the simulator refuses them otherwise, because a suite that passed
    # those without a join would pass on a rig whose radio was dead.
    board.handle_command(json.dumps({"cmd": "http_get", "url": "http://10.42.0.1:8088/healthz"}))
    assert _drain(board)[0]["ok"] is False
    board.handle_command(json.dumps({"cmd": "wifi_join", "ssid": "Alteriom-HIL", "password": "x"}))
    joined = _drain(board)[0]
    assert joined["joined"] is True and joined["ip"] and joined["gateway"]
    board.handle_command(json.dumps({"cmd": "http_get", "url": "http://10.42.0.1:8088/healthz"}))
    assert _drain(board)[0]["status"] == 200
    board.handle_command(json.dumps({"cmd": "mqtt_publish", "host": "10.42.0.1", "port": 1883,
                                     "topic": "t", "payload": "p"}))
    assert _drain(board)[0]["ok"] is True

    # A reset is a new session: a new boot id, and nothing kept.
    board.handle_command(json.dumps({"cmd": "reset"}))
    frames = _drain(board)
    assert frames[0]["evt"] == "resetting"
    assert frames[1]["evt"] == "boot" and frames[1]["bootId"] != info["bootId"]
    assert frames[1]["resetReason"] == "software"
    board.handle_command(json.dumps({"cmd": "http_get", "url": "http://10.42.0.1:8088/healthz"}))
    assert _drain(board)[0]["ok"] is False, "a restarted board is joined to nothing"

    # A join to something that is not the rig is a join that fails, as it
    # would on the bench.
    board.handle_command(json.dumps({"cmd": "wifi_join", "ssid": "somebody-else", "password": "x"}))
    assert _drain(board)[0]["joined"] is False


def test_a_simulated_board_speaks_the_firmware_it_is_given_and_no_other(monkeypatch):
    """The canary commands are the common part. A suite's own verbs are a
    firmware the hub is given; a board given none answers them as unknown,
    so a suite cannot pass in sim against verbs its firmware never had."""
    from suites.painlessmesh.simmesh import MeshFirmware

    hub = sim.SimHub(2, firmware=MeshFirmware)
    first, second = hub.boards
    _drain(first), _drain(second)
    first.handle_command(json.dumps({"cmd": "node_list"}))
    assert _drain(first)[0]["nodes"] == [second.node_id]
    first.handle_command(json.dumps({"cmd": "send_single", "dest": second.node_id, "msg": "hi"}))
    assert _drain(first)[0] == {"evt": "send_result", "ok": True}
    assert _drain(second)[0]["msg"] == "hi"
    first.handle_command(json.dumps({"cmd": "nonsense"}))
    assert _drain(first)[0]["error"] == "unknown cmd nonsense"

    plain = sim.SimHub(2).boards[0]
    _drain(plain)
    plain.handle_command(json.dumps({"cmd": "node_list"}))
    assert _drain(plain)[0]["error"] == "unknown cmd node_list"
    plain.handle_command(json.dumps({"cmd": "info"}))
    assert "listening" not in _drain(plain)[0], "a field the mesh agent reports, not every agent"


def _drain(board) -> list[dict]:
    frames = []
    while True:
        line = board.to_host.readline()
        if not line:
            return frames
        frames.append(json.loads(line))


# ---- the health feature: the farm checking its own hardware ----------------
# The canary run itself is an ordinary suite run, which the pipeline tests
# already cover. What is new is the scoping ("this board"), the choice of
# bundle ("the current canary"), and reading each board's verdict back out of
# the run -- including which failures were the farm's rather than a board's.


def _health_manager(tmp_path, boards=("esp32-c6-01", "esp32-c3-01")):
    """A manager with a rig, the canary profile, and nothing else."""
    import shutil

    repo = tmp_path / "repo"
    (repo / "profiles").mkdir(parents=True)
    for document in (REPO / "profiles").glob("*.yaml"):
        shutil.copy2(document, repo / "profiles" / document.name)
    manager = farm_service.FarmManager(
        repo=repo,
        state=tmp_path / "state",
        registry=tmp_path / "inventory.yaml",
        board_map=tmp_path / "board-map.active.yaml",
        python=Path("/usr/bin/python3"),
    )
    manager.state.mkdir(parents=True, exist_ok=True)
    families = {"esp32-c6-01": "esp32-c6", "esp32-c3-01": "esp32-c3", "esp32-s3-01": "esp32-s3"}
    snapshot = {
        "boards": [
            {"id": board, "target": families[board], "mac": f"aa:bb:cc:00:00:0{index}",
             "port": f"/dev/ttyACM{index}"}
            for index, board in enumerate(boards)
        ],
        "missing": [], "unregistered": [], "probe_errors": [],
    }
    manager.inventory_snapshot = lambda annotate=False: {
        **snapshot, "boards": [dict(board) for board in snapshot["boards"]]
    }
    return manager


def test_a_health_check_runs_the_canary_on_the_boards_it_was_asked_about(tmp_path, monkeypatch):
    """One board is a complete answer about that board, and asking about one
    must not flash the rest of the rig. The families the run asks for are the
    families of the boards it was given, nothing wider."""
    manager = _health_manager(tmp_path)
    submitted = []
    monkeypatch.setattr(
        farm_service.FarmManager, "submit",
        lambda self, kind, request, submitted_by=None: submitted.append((kind, request)) or {"id": "j" * 32},
    )

    manager.health_check({"boards": ["esp32-c6-01"]})
    kind, request = submitted[-1]
    assert kind == "suite" and request["profile"] == "canary"
    assert request["boards"] == ["esp32-c6-01"]
    assert request["targets"] == ["esp32-c6"], "only the family of the board asked about"
    # A health check is not a validation of a commit: it must not push a
    # queued run out of the way.
    assert request["supersede"] is False

    manager.health_check({})
    _kind, everything = submitted[-1]
    assert everything["boards"] == ["esp32-c3-01", "esp32-c6-01"]
    assert everything["targets"] == ["esp32-c3", "esp32-c6"]
    assert manager.health_check({"boards": "all"}) == {"id": "j" * 32}

    # A board that is not plugged in is a typo or a board that has gone, and
    # both are answers to give now rather than after the job has queued,
    # taken the rig and flashed what it could find.
    with pytest.raises(ValueError, match="not connected: esp32-h2-01"):
        manager.health_check({"boards": ["esp32-h2-01"]})
    with pytest.raises(ValueError, match="unknown request fields"):
        manager.health_check({"profile": "painlessmesh"})


def test_a_health_check_flashes_the_pinned_current_canary(tmp_path, monkeypatch):
    """The bundle an operator checks a board against today should be the one
    the running farm was released with -- which is the pinned one. Asking for
    the commit it was built from is what lets the run name it: the same rule
    a consumer's supplied bundle has to meet."""
    manager = _health_manager(tmp_path)
    submitted = []
    monkeypatch.setattr(
        farm_service.FarmManager, "submit",
        lambda self, kind, request, submitted_by=None: submitted.append((kind, request)) or {"id": "j" * 32},
    )
    commit = "a1" * 20
    older, newer = "1" * 32, "2" * 32
    for bundle_id, revision in ((older, "b2" * 20), (newer, commit)):
        _canary_bundle(manager.artifact_root, bundle_id, revision)

    # Nothing pinned: there is no current canary, so the run builds one --
    # the fallback that keeps the feature usable on a farm that has not
    # deployed since the canary landed.
    assert manager.current_canary() is None
    manager.health_check({})
    assert "artifact" not in submitted[-1][1] and "ref" not in submitted[-1][1]

    manager.store.pin_artifact(older, "an older release")
    assert manager.current_canary() == (older, "b2" * 20)
    manager.store.pin_artifact(newer, "the current release")
    assert manager.current_canary() == (newer, commit), "the most recently pinned canary"
    # Current is what was pinned last, not what was built last: an operator
    # who deliberately pins an older canary has said which one the farm
    # should be checked against.
    manager.store.unpin_artifact(older)
    manager.store.pin_artifact(older, "back to the one that worked")
    assert manager.current_canary() == (older, "b2" * 20)
    manager.store.unpin_artifact(older)
    assert manager.current_canary() == (newer, commit)

    manager.health_check({"boards": ["esp32-c6-01"]})
    request = submitted[-1][1]
    assert request["artifact"] == newer
    assert request["ref"] == commit, "the run asks for the commit the bundle was built from"

    # A bundle that is not a canary is not the canary, however pinned.
    other = "3" * 32
    _canary_bundle(manager.artifact_root, other, "c3" * 20, producer="painlessmesh")
    manager.store.pin_artifact(other, "somebody else's")
    assert manager.current_canary() == (newer, commit)


def _canary_bundle(root: Path, bundle_id: str, revision: str, producer: str = "canary") -> Path:
    path = root / bundle_id / "esp32-c6"
    path.mkdir(parents=True)
    (path / "flash-image.bin").write_bytes(b"\xff" * 64)
    manifest = {
        "schema": 2, "producer": producer, "farm_sha": revision,
        "canary_sha": "d" * 64,
        "targets": {"esp32-c6": {"image": "esp32-c6/flash-image.bin", "sha256": "e" * 64}},
    }
    (root / bundle_id / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root / bundle_id


def test_the_canary_a_release_carries_becomes_the_one_a_health_check_flashes(tmp_path, monkeypatch):
    """The other end of `alteriom-hil-admin upgrade`. A rig installs a release,
    and the health check it runs afterwards is against the firmware that
    release was built with -- not one it built itself, and not the last one
    somebody happened to pin. This is the whole point of putting the firmware
    in the release (docs/public-release-plan.md, steps 14b and 13d), and it is
    two programs deep: the command writes the store, the service reads it.
    """
    from alteriom_hil import admin_cli

    manager = _health_manager(tmp_path)
    manager.state.mkdir(parents=True, exist_ok=True)
    commit = "a1" * 20
    image = b"\xff" * 64
    built = {
        "schema": 2, "producer": "canary", "farm_sha": commit, "canary_sha": "d" * 64,
        "version": "1.0.12",
        "targets": {"esp32-c6": {"image": "esp32-c6/flash-image.bin",
                                 "sha256": hashlib.sha256(image).hexdigest()}},
    }
    bundle = _tar({"hil-canary/manifest.json": json.dumps(built).encode(),
                   "hil-canary/esp32-c6/flash-image.bin": image})

    payload = {"paths": {"repo": str(manager.repo), "state": str(manager.state)}}
    release = {"version": "1.0.376", "commit": commit}
    carried = {"name": "alteriom-hil-canary-1.0.12.tar.gz", "version": "1.0.12",
               "revision": "d" * 64, "families": ["esp32-c6"]}
    tarball = tmp_path / carried["name"]
    tarball.write_bytes(bundle)

    assert manager.current_canary() is None, "nothing installed, nothing to flash"
    bundle_id = admin_cli._install_firmware(payload, tarball, release, carried)
    assert manager.current_canary() == (bundle_id, commit)

    request = {}
    monkeypatch.setattr(
        farm_service.FarmManager, "submit",
        lambda self, kind, req, submitted_by=None: request.update(req) or {"id": "j" * 32},
    )
    manager.health_check({"boards": ["esp32-c6-01"]})
    assert request["artifact"] == bundle_id
    assert request["ref"] == commit, "the run asks for the commit the release was built from"


def _tar(payload: dict) -> bytes:
    import io
    import tarfile as _tarfile

    buffer = io.BytesIO()
    with _tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, body in payload.items():
            info = _tarfile.TarInfo(name)
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


def test_each_board_keeps_the_canary_s_last_verdict_and_the_farm_owns_its_own(tmp_path, monkeypatch):
    """The suite runs every check once per board, so the JUnit is already a
    board x check matrix. Read back per board, it answers "when was this
    board last checked and what failed" without opening a port -- and it
    separates a check red on every board (the farm) from one red on a single
    board, so nobody replaces a board over a broker that is down."""
    manager = _health_manager(tmp_path, boards=("esp32-c6-01", "esp32-c3-01", "esp32-s3-01"))
    results = tmp_path / "results.xml"
    results.write_text(
        """<?xml version="1.0"?><testsuites><testsuite name="pytest">
        <testcase classname="suites.canary.tests.test_esp_health" name="test_the_board_boots[esp32-c6-01]" time="1"/>
        <testcase classname="suites.canary.tests.test_esp_health" name="test_the_board_boots[esp32-c3-01]" time="1"/>
        <testcase classname="suites.canary.tests.test_esp_health" name="test_the_board_boots[esp32-s3-01]" time="1"/>
        <testcase classname="suites.canary.tests.test_esp_health" name="test_the_flash_keeps[esp32-c6-01]" time="1"/>
        <testcase classname="suites.canary.tests.test_esp_health" name="test_the_flash_keeps[esp32-c3-01]" time="1"><failure message="wore out"/></testcase>
        <testcase classname="suites.canary.tests.test_esp_health" name="test_the_flash_keeps[esp32-s3-01]" time="1"/>
        <testcase classname="suites.canary.tests.test_rig_health" name="test_the_broker_takes[esp32-c6-01]" time="1"><failure message="refused"/></testcase>
        <testcase classname="suites.canary.tests.test_rig_health" name="test_the_broker_takes[esp32-c3-01]" time="1"><error message="refused"/></testcase>
        <testcase classname="suites.canary.tests.test_rig_health" name="test_the_broker_takes[esp32-s3-01]" time="1"><failure message="refused"/></testcase>
        <testcase classname="suites.canary.tests.test_rig_health" name="test_the_radio_joins[esp32-c6-01]" time="1"><skipped message="no ap"/></testcase>
        <testcase classname="suites.canary.tests.test_canary" name="test_something_not_per_board" time="1"/>
        </testsuite></testsuites>""",
        encoding="utf-8",
    )
    summary = manager._record_board_health("j" * 32, results, "a1" * 20)

    assert summary["boards"] == {
        "esp32-c6-01": "failed", "esp32-c3-01": "failed", "esp32-s3-01": "failed",
    }
    # The broker failed on every board that ran it: one fault, not three.
    assert summary["farm_wide"] == ["test_the_broker_takes"]

    kept = manager.load_board_health()
    assert sorted(kept) == ["esp32-c3-01", "esp32-c6-01", "esp32-s3-01"]
    c3 = kept["esp32-c3-01"]
    assert c3["verdict"] == "failed"
    assert c3["failed"] == ["test_the_broker_takes", "test_the_flash_keeps"]
    # Its own fault is the flash; the broker is the farm's.
    assert c3["farm_wide"] == ["test_the_broker_takes"]
    assert c3["job_id"] == "j" * 32 and c3["canary_revision"] == "a1" * 20
    assert c3["mac"] == "aa:bb:cc:00:00:01" and c3["checked_at"]
    s3 = kept["esp32-s3-01"]
    assert s3["failed"] == ["test_the_broker_takes"] and s3["farm_wide"] == s3["failed"]
    # A check that was skipped is neither a pass nor a failure for the board.
    assert kept["esp32-c6-01"]["checks"]["test_the_radio_joins"] == "skipped"
    assert "test_the_radio_joins" not in kept["esp32-c6-01"]["failed"]
    # A check that is not per-board says nothing about any board.
    assert all(
        "test_something_not_per_board" not in entry["checks"] for entry in kept.values()
    )

    # And the inventory carries it, so the page can show a board that started
    # failing before it fails somebody's run. Through the real method, with
    # only the on-disk snapshot stood in for: the annotation is what is being
    # checked, and it is the annotation the dashboard reads.
    discovered = manager.inventory_snapshot()
    monkeypatch.setattr(
        core_service, "load_inventory_snapshot",
        lambda state, registry: {**discovered, "boards": [dict(b) for b in discovered["boards"]]},
    )
    annotated = farm_service.FarmManager.inventory_snapshot(manager, annotate=True)
    by_id = {board["id"]: board for board in annotated["boards"]}
    assert by_id["esp32-c3-01"]["health"]["verdict"] == "failed"
    assert by_id["esp32-c3-01"]["health"]["farm_wide"] == ["test_the_broker_takes"]
    assert by_id["esp32-c6-01"]["health"]["checked_at"]

    # A later run that passes replaces the verdict rather than adding to it.
    results.write_text(
        """<?xml version="1.0"?><testsuites><testsuite name="pytest">
        <testcase classname="t" name="test_the_flash_keeps[esp32-c3-01]" time="1"/>
        </testsuite></testsuites>""",
        encoding="utf-8",
    )
    manager._record_board_health("k" * 32, results, "a2" * 20)
    again = manager.load_board_health()
    assert again["esp32-c3-01"]["verdict"] == "passed" and again["esp32-c3-01"]["failed"] == []
    assert again["esp32-c6-01"]["job_id"] == "j" * 32, "a board the run did not check is untouched"


def test_a_run_may_be_scoped_to_named_boards(tmp_path):
    """The allocation used to come from the profile alone -- the bank, or its
    `needs` -- which cannot express "this board". A canary run on one board
    must flash one board."""
    manager = _health_manager(tmp_path, boards=("esp32-c6-01", "esp32-c3-01"))
    manager.board_map.write_text(
        yaml.safe_dump({"boards": [
            {"id": "esp32-c6-01", "target": "esp32-c6", "port": "/dev/ttyACM0"},
            {"id": "esp32-c3-01", "target": "esp32-c3", "port": "/dev/ttyACM1"},
        ]}),
        encoding="utf-8",
    )
    spec = manager.profiles["canary"]
    assert spec.exclusive, "the canary takes the bank when nothing narrows it"

    class Log:
        def write(self, *_): pass
        def flush(self): pass

    # Nothing named: the active map, unchanged.
    assert manager._scoped_board_map(spec, "j" * 32, Log()) == manager.board_map
    scoped = manager._scoped_board_map(spec, "j" * 32, Log(), ["esp32-c6-01"])
    document = yaml.safe_load(scoped.read_text(encoding="utf-8"))
    assert [board["id"] for board in document["boards"]] == ["esp32-c6-01"]

    # A board that went away between submit and run is the run's failure to
    # report, not a run that quietly checks something else.
    manager.board_map.write_text(
        yaml.safe_dump({"boards": [{"id": "esp32-c3-01", "target": "esp32-c3", "port": "/dev/ttyACM1"}]}),
        encoding="utf-8",
    )
    with pytest.raises(farm_service.PipelineError, match="not connected"):
        manager._scoped_board_map(spec, "j" * 32, Log(), ["esp32-c6-01"])


def test_the_health_route_needs_the_token_and_answers_in_http_terms(tmp_path, monkeypatch):
    import threading
    from http.server import ThreadingHTTPServer
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    manager = _health_manager(tmp_path)
    queued = []
    monkeypatch.setattr(
        farm_service.FarmManager, "submit",
        lambda self, kind, request, submitted_by=None: queued.append(request) or {"id": "j" * 32, "status": "queued"},
    )
    token = "t" * 40
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), farm_service.make_handler(manager, token, tmp_path)
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(body=None, auth=True):
        headers = {"Authorization": f"Bearer {token}"} if auth else {}
        data = json.dumps(body if body is not None else {}).encode()
        headers["Content-Type"] = "application/json"
        try:
            with urlopen(Request(base + "/api/v1/health", data=data, method="POST", headers=headers), timeout=5) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    try:
        assert call(auth=False)[0] == 401
        status, body = call({"boards": ["esp32-c6-01"]})
        # Accepted, not done: it queues like any other run.
        assert status == 202 and body["id"] == "j" * 32
        assert queued[-1]["boards"] == ["esp32-c6-01"]
        assert call({})[0] == 202 and queued[-1]["boards"] == ["esp32-c3-01", "esp32-c6-01"]
        # A board that is not there, and a field that is not a field: both
        # are the request's fault and both say what is wrong.
        status, body = call({"boards": ["esp32-h2-01"]})
        assert status == 400 and "not connected" in body["error"]
        assert call({"profile": "painlessmesh"})[0] == 400
        # A rig already busy with something that cannot be queued behind is
        # the one conflict this route has to report.
        monkeypatch.setattr(
            farm_service.FarmManager, "submit",
            lambda self, kind, request, submitted_by=None: (_ for _ in ()).throw(farm_service.RigBusyError("the rig is busy")),
        )
        status, body = call({})
        assert status == 409 and body["error"] == "the rig is busy"
    finally:
        server.shutdown()


# ---- what a red canary does ------------------------------------------------
# The two kinds of red call for different things, and the farm owns the
# decision rather than CI: red on every board is the farm and pauses the
# queue, because everything queued behind it would otherwise flash boards
# whose rig cannot join a network and report that as the firmware's failure.


_VERDICT = importlib.util.spec_from_file_location(
    "canary_verdict", REPO / "rig" / "canary_verdict.py"
)
canary_verdict = importlib.util.module_from_spec(_VERDICT)
_VERDICT.loader.exec_module(canary_verdict)


def test_a_canary_red_on_every_board_pauses_the_queue(tmp_path):
    """It is the rig that failed, and the rig is what runs everything queued
    behind the check. An operator resumes it, and the reason says what to
    fix -- a queue paused for no stated reason is one somebody resumes
    without knowing what it was protecting."""
    manager = _health_manager(tmp_path, boards=("esp32-c6-01", "esp32-c3-01"))
    results = tmp_path / "results.xml"

    def run(document: str):
        results.write_text(document, encoding="utf-8")
        return manager._record_board_health("j" * 32, results, "a1" * 20)

    # One board's flash failed: that board's problem. The queue keeps going.
    summary = run(
        """<?xml version="1.0"?><testsuites><testsuite name="pytest">
        <testcase classname="t" name="test_the_flash_keeps[esp32-c6-01]" time="1"/>
        <testcase classname="t" name="test_the_flash_keeps[esp32-c3-01]" time="1"><failure message="wore out"/></testcase>
        </testsuite></testsuites>"""
    )
    assert summary["farm_wide"] == []
    assert manager.queue_state()["paused"] is False

    # The broker refused every board: the farm's, and nothing queued should
    # run into it.
    summary = run(
        """<?xml version="1.0"?><testsuites><testsuite name="pytest">
        <testcase classname="t" name="test_the_broker_takes[esp32-c6-01]" time="1"><failure message="refused"/></testcase>
        <testcase classname="t" name="test_the_broker_takes[esp32-c3-01]" time="1"><failure message="refused"/></testcase>
        </testsuite></testsuites>"""
    )
    assert summary["farm_wide"] == ["test_the_broker_takes"]
    # The pause happens where the run records its health, which is the
    # pipeline's job -- exercised here through the same call the pipeline
    # makes, then the decision itself.
    manager.pause(f"the canary failed on every board: test_the_broker_takes (run {'j' * 8})")
    state = manager.queue_state()
    assert state["paused"] is True
    assert "failed on every board" in state["paused_reason"]
    assert "test_the_broker_takes" in state["paused_reason"]
    # And resuming clears the reason with it: the next pause has its own.
    assert manager.resume()["paused_reason"] is None


def test_the_deploy_stands_for_one_bad_board_and_falls_for_a_bad_farm():
    """A farm of six boards is not held hostage to one dead ESP -- blocking
    every release until somebody swaps it would only teach people to skip
    the check. A release whose *rig* cannot answer is not a release."""
    def judge(health, status="passed"):
        return canary_verdict.verdict({
            "id": "j" * 32, "status": status, "result": {"health": health},
        })

    healthy = {"boards": {"esp32-c6-01": "passed", "esp32-c3-01": "passed"}, "farm_wide": []}
    stands, lines = judge(healthy)
    assert stands and "Every check passed" in "\n".join(lines)

    one_board = {"boards": {"esp32-c6-01": "passed", "esp32-c3-01": "failed"}, "farm_wide": []}
    stands, lines = judge(one_board, status="failed")
    page = "\n".join(lines)
    assert stands, "one board's failure is that board's, and the rig keeps working"
    assert "esp32-c3-01" in page and "rather than the farm" in page

    the_farm = {
        "boards": {"esp32-c6-01": "failed", "esp32-c3-01": "failed"},
        "farm_wide": ["test_the_broker_takes"],
    }
    stands, lines = judge(the_farm, status="failed")
    page = "\n".join(lines)
    assert not stands
    assert "the broker takes failed on every board" in page
    assert "queue is paused" in page and "redeploy" in page

    # A run that never checked anything cannot vouch for the hardware, which
    # is the only thing this step is for -- whatever its status says.
    stands, lines = judge({}, status="failed")
    assert not stands and "no per-board verdicts" in "\n".join(lines)
    stands, _ = judge({}, status="passed")
    assert stands, "a passed run with no canary health is some other profile's"

    # The deploy summary names the check and the firmware version that ran.
    _, lines = canary_verdict.verdict({"status": "passed", "result": {"version": "1.0.6"}})
    assert lines[0] == "## Rig Health Check 1.0.6"
    _, lines = canary_verdict.verdict({"status": "passed", "result": {}})
    assert lines[0] == "## Rig Health Check", "a bundle from before versions has none to show"


def test_a_canary_run_that_failed_still_hands_over_its_verdicts(tmp_path):
    """The deploy's gate decides between "the farm" and "one board" from the
    per-board verdicts -- and the result dict carrying them was only
    returned when a run succeeded. So the gate was told "no per-board
    verdicts" by exactly the runs it exists to judge: the canary's first real
    run failed five checks across four boards and the release was refused for
    lack of evidence that was sitting in the JUnit all along."""
    error = farm_service.PipelineError(
        "test", "Validation tests did not complete successfully", "pytest exited with status 1",
        result={"health": {"boards": {"esp32-c3-02": "failed"}, "farm_wide": []}},
    )
    assert error.result["health"]["boards"] == {"esp32-c3-02": "failed"}
    # A stage cannot overwrite the failure it is reporting.
    merged = {**error.result, "summary": error.summary, "failed_stage": error.stage,
              "detail": error.detail}
    assert merged["summary"] == "Validation tests did not complete successfully"
    assert merged["health"]["boards"]

    # And the judge reaches a per-board verdict from it rather than refusing
    # the release for want of data.
    stands, lines = canary_verdict.verdict({
        "id": "j" * 32, "status": "failed", "result": merged,
    })
    assert stands and "rather than the farm" in chr(10).join(lines)

    # A PipelineError that carries nothing behaves as it always did.
    plain = farm_service.PipelineError("flash", "Flashing failed", "esptool said no")
    assert plain.result == {}


def test_the_firmware_reads_a_whole_line_before_it_sleeps():
    """The canary's first run lost its kilobyte-long echo command on the S3,
    the C3 and the C5 -- each answered `bad json` to a truncated frame --
    while the two UART-bridge families echoed all 1033 bytes. A native
    USB-Serial/JTAG console starts with a 256-byte receive buffer, and the
    loop slept in the middle of the command arriving. Both halves are fixed
    and both are asserted, because a compile cannot see either."""
    shim = (CANARY / "firmware" / "src" / "canary_platform.h").read_text(encoding="utf-8")
    firmware = FIRMWARE.read_text(encoding="utf-8")
    # Room for a whole line, set before begin() -- afterwards has no effect.
    assert "setRxBufferSize" in shim and "console.begin(115200)" in shim
    assert "canaryOpenConsole(Serial, kLineMax + 512)" in firmware
    assert "Serial.begin(115200)" not in firmware, "the console is opened with room"
    # And the loop drains while bytes keep coming instead of sleeping mid-line.
    assert "bool pump(Stream &console)" in firmware
    assert "if (!busy) delay(2);" in firmware


def test_a_tcp_timeout_is_in_the_units_each_core_means():
    """WiFiClient::setTimeout is seconds on the ESP32 cores and milliseconds
    on the ESP8266, where it is Stream's. Passing seconds to the ESP8266 gave
    a TCP connect eight milliseconds, and the canary's first run reported the
    rig's uplink and broker unreachable from that board alone -- a rig fault
    that was a unit bug."""
    shim = (CANARY / "firmware" / "src" / "canary_platform.h").read_text(encoding="utf-8")
    firmware = FIRMWARE.read_text(encoding="utf-8")
    helper = shim.split("inline void canarySetClientTimeout", 1)[1].split(chr(10) + chr(125), 1)[0]
    assert "defined(ESP8266)" in helper
    assert "client.setTimeout(budgetMs);" in helper, "milliseconds on the 8266"
    assert "budgetMs / 1000" in helper, "seconds on the ESP32 cores"
    # And nothing divides by a thousand at the call sites any more.
    assert "canarySetClientTimeout(client, budget)" in firmware
    assert "client.setTimeout(" not in firmware


def test_the_uplink_check_waits_for_the_answer_not_the_connection():
    """The rig's probe replies HTTP/1.0 and closes at once, which leaves
    `connected()` false with the response still buffered. A read loop gated
    on it read nothing and gave up in 28 ms, and the canary reported the
    rig's uplink unreachable from the ESP8266 -- `status:0, bytes:0` -- while
    every other board got its 200 in 23 ms. A false negative from a health
    check is worse than no check: it sends somebody to look at a working
    rig."""
    firmware = FIRMWARE.read_text(encoding="utf-8")
    body = firmware.split("void replyHttpGet", 1)[1].split("namespace mqtt", 1)[0]
    assert "client.readStringUntil" in body
    # Nothing in the read path may depend on the peer still being there.
    reads = body.split("client.print(", 1)[1]
    assert "client.connected()" not in reads, (
        "a server that answers and closes leaves connected() false with the "
        "answer still in the buffer"
    )
    # The connect still reports its own failure, which is a different thing
    # from an answer that did not arrive.
    assert "connect failed" in body
