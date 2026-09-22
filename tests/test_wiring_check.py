"""The wiring check: every jumper between an instrument and a board, driven
from one end and read at the other.

It has to be trustworthy before any hardware exists to trust it against, so
what is held here is what a real rig cannot show on demand: the canary's pin
tables are the farm's, the simulated wire behaves like a wire, a broken one is
reported by name, and a wiring failure never pauses the farm.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from alteriom_hil.pins import INPUT_ONLY_PINS, WIREABLE_PINS
from alteriom_hil.protocol import BoardClient
from alteriom_hil.serial_capture import SerialCapture
from alteriom_hil.sim import SimHub

REPO = Path(__file__).resolve().parents[1]
PLATFORM = REPO / "canary" / "firmware" / "src" / "canary_platform.h"


def test_the_canary_refuses_exactly_the_pins_the_farm_does():
    """The farm checks wiring where it is written; the canary checks it again
    where it drives. Two tables of one fact drift, so they are compared."""
    source = PLATFORM.read_text(encoding="utf-8").split("the pins an instrument may be wired to", 1)[1]
    branches = {
        "esp8266": "defined(ESP8266)",
        "esp32-c3": "defined(CONFIG_IDF_TARGET_ESP32C3)",
        "esp32-c6": "defined(CONFIG_IDF_TARGET_ESP32C6)",
        "esp32-s3": "defined(CONFIG_IDF_TARGET_ESP32S3)",
        "esp32": "defined(CONFIG_IDF_TARGET_ESP32)\n",
    }
    for family, condition in branches.items():
        block = source.split(condition, 1)[1].split("#e", 1)[0]
        assert f'kPinTable = "pins:{family}"' in block, family
        wireable = [int(n) for n in re.search(r"kWireablePins\[\] = \{([^}]*)\}", block).group(1).split(",")]
        input_only = [int(n) for n in re.search(r"kInputOnlyPins\[\] = \{([^}]*)\}", block).group(1).split(",")]
        assert wireable[-1] == -1 and input_only[-1] == -1, family
        assert set(wireable[:-1]) == WIREABLE_PINS[family], family
        assert set(input_only[:-1]) == set(INPUT_ONLY_PINS.get(family, ())), family
    # A family with no table falls through to none, never to another's.
    fallback = source.split("#else\nstatic const char *const kPinTable", 1)[1].split("#endif", 1)[0]
    assert '"pins:none"' in fallback and "kWireablePins[] = {-1}" in fallback
    assert "esp32-c5" not in WIREABLE_PINS


def _client(part) -> BoardClient:
    return BoardClient("sim", SerialCapture(part.open_host_stream).start())


def test_a_simulated_wire_carries_what_drives_it():
    hub = SimHub(2, instruments=True)
    board = hub.boards[0]
    instrument = hub.instruments[0]
    wires = [(channel, pin) for inst, channel, part, pin in hub.wires if part is board]
    assert wires == [(4, 25), (13, 26), (34, 27)], "two lines either end drives, one only the board does"

    # One client per part: two captures on one simulated port would each take
    # half of its replies.
    clients = {id(board): _client(board), id(instrument): _client(instrument)}

    def cmd(part, **fields):
        client = clients[id(part)]
        client.clear_pending()
        client.send_cmd(fields.pop("cmd"), **fields)
        return client.wait_for(lambda e: e["evt"] not in ("boot", "resetting"), "reply", 2)

    # The instrument drives, the board reads it.
    assert cmd(instrument, cmd="write", ch=4, level=1)["level"] == 1
    assert cmd(board, cmd="gpio_read", pin=25)["level"] == 1
    cmd(instrument, cmd="write", ch=4, level=0)
    assert cmd(board, cmd="gpio_read", pin=25)["level"] == 0
    # The board drives, the instrument reads it -- including on an input-only channel.
    cmd(instrument, cmd="mode", ch=34, mode="input")
    assert cmd(board, cmd="gpio_write", pin=27, level=1)["ok"] is True
    assert cmd(instrument, cmd="read", ch=34)["level"] == 1
    assert cmd(instrument, cmd="adc", ch=34)["mv"] == 3300
    # Nothing driving: the pull decides.
    cmd(board, cmd="gpio_release", pin=27)
    cmd(board, cmd="gpio_mode", pin=26, mode="pullup")
    assert cmd(instrument, cmd="read", ch=13)["level"] == 1
    # Neither end may reach what it must not.
    refused = cmd(board, cmd="gpio_write", pin=9, level=1)
    assert refused["ok"] is False and refused["error"] == "not a wireable pin on this family"
    assert cmd(board, cmd="gpio_write", pin=34, level=1)["error"] == "an input-only pin"
    assert cmd(instrument, cmd="write", ch=34, level=1)["error"] == "this channel is input-only"
    # A reset forgets every pin, as the part does.
    cmd(board, cmd="gpio_write", pin=25, level=1)
    clients[id(board)].send_cmd("reset")
    clients[id(board)].wait_for(lambda e: e["evt"] == "boot", "boot after reset", 2)
    assert board.pins.get(25)["mode"] == "input"
    for client in clients.values():
        client.capture.stop()


def _run_canary_wiring(env_extra: dict) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "ALTERIOM_HIL_MODE": "sim",
        "ALTERIOM_HIL_SIM_BOARDS": "2",
        "PYTHONPATH": os.pathsep.join([str(REPO / "rig"), str(REPO / "core"), os.environ.get("PYTHONPATH", "")]),
        **env_extra,
    }
    env.pop("ALTERIOM_HIL_BOARD_MAP", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_wiring.py", "-q", "-p", "no:cacheprovider"],
        cwd=REPO / "suites" / "canary", env=env, capture_output=True, text=True, timeout=300,
    )


def test_the_wiring_check_passes_on_whole_wires_and_names_a_broken_one():
    whole = _run_canary_wiring({})
    assert whole.returncode == 0, whole.stdout + whole.stderr
    assert "2 passed" in whole.stdout

    broken = _run_canary_wiring({"ALTERIOM_HIL_SIM_BROKEN_WIRES": "sim-1001:25,sim-1001:27"})
    assert broken.returncode == 1, broken.stdout + broken.stderr
    output = broken.stdout
    assert "1 failed, 1 passed" in output
    # Each broken wire by name, each direction that could not carry a level.
    assert "sim-io channel 14 -> sim-1001 GPIO25: the instrument drove 1, the board read 0" in output
    assert "sim-io channel 14 -> sim-1001 GPIO25: the board drove 1, the instrument read 0" in output
    assert "sim-io channel 35 -> sim-1001 GPIO27: the board drove 1, the instrument read 0" in output
    # The whole wire on the same board is not blamed.
    assert "GPIO26" not in output

    # With no instrument, there is nothing to check, and it says so.
    none = _run_canary_wiring({"ALTERIOM_HIL_SIM_INSTRUMENTS": "0"})
    assert none.returncode == 0 and "2 skipped" in none.stdout


# ---- the service --------------------------------------------------------------------


def _service():
    spec = importlib.util.spec_from_file_location("farm_service_wiring", REPO / "runner" / "farm_service.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_run_given_some_boards_gets_only_their_wires(tmp_path):
    farm_service = _service()
    manager = farm_service.FarmManager.__new__(farm_service.FarmManager)
    manager.state = tmp_path
    manager.board_map = tmp_path / "board-map.active.yaml"
    manager.board_map.write_text(yaml.safe_dump({
        "boards": [
            {"id": "esp32-c6-01", "target": "esp32-c6", "port": "/dev/a"},
            {"id": "esp32-c3-01", "target": "esp32-c3", "port": "/dev/b"},
        ],
        "instruments": [
            {"id": "io-01", "kind": "esp32-io", "port": "/dev/c", "mac": "24:6f:28:00:00:99", "wiring": [
                {"channel": 25, "board": "esp32-c6-01", "pin": 18},
                {"channel": 26, "board": "esp32-c3-01", "pin": 4},
            ]},
            {"id": "io-02", "kind": "esp32-io", "port": "/dev/d", "mac": "24:6f:28:00:00:98", "wiring": [
                {"channel": 25, "board": "esp32-c3-01", "pin": 5},
            ]},
        ],
    }))

    class Spec:
        exclusive = True
        needs = ()
        label = "canary"

    class Log:
        def write(self, text):
            pass

        def flush(self):
            pass

    scoped = yaml.safe_load(manager._scoped_board_map(Spec(), "job1", Log(), named=["esp32-c6-01"]).read_text())
    assert [board["id"] for board in scoped["boards"]] == ["esp32-c6-01"]
    assert scoped["instruments"] == [{
        "id": "io-01", "kind": "esp32-io", "port": "/dev/c", "mac": "24:6f:28:00:00:99",
        "wiring": [{"channel": 25, "board": "esp32-c6-01", "pin": 18}],
    }], "only the wires to its own board, and no instrument with none"


def test_a_wiring_failure_never_pauses_the_farm(tmp_path):
    """An unplugged instrument fails every wired board. That is the
    instrument, not the farm's network, and pausing every queued run for it
    would stop runs that never touch a wire."""
    farm_service = _service()
    manager = farm_service.FarmManager.__new__(farm_service.FarmManager)
    manager.state = tmp_path
    manager.inventory_snapshot = lambda annotate=False: {"boards": []}
    results = tmp_path / "results.xml"
    results.write_text(
        """<?xml version="1.0"?><testsuites><testsuite name="pytest">
        <testcase classname="t" name="test_every_wire_carries_a_level_both_ways[esp32-c6-01]" time="1"><failure message="no level"/></testcase>
        <testcase classname="t" name="test_every_wire_carries_a_level_both_ways[esp32-c3-01]" time="1"><failure message="no level"/></testcase>
        <testcase classname="t" name="test_the_broker_takes[esp32-c6-01]" time="1"/>
        <testcase classname="t" name="test_the_broker_takes[esp32-c3-01]" time="1"/>
        </testsuite></testsuites>""",
        encoding="utf-8",
    )
    summary = manager._record_board_health("j" * 32, results, "a1" * 20)
    assert summary["farm_wide"] == []
    assert summary["boards"] == {"esp32-c6-01": "failed", "esp32-c3-01": "failed"}, "each board still marked"
    health = json.loads((tmp_path / "board-health.json").read_text())
    assert health["esp32-c6-01"]["failed"] == ["test_every_wire_carries_a_level_both_ways"]
    assert health["esp32-c6-01"]["farm_wide"] == []
