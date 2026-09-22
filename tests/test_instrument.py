"""An I/O instrument: test equipment the farm owns, with an identity of its own.

The first one is an ESP32 on jumper wires. It is an ESP32 to discovery, so the
properties worth keeping are the ones that stop it being treated as a board --
claimed by its MAC before anything else, never offered for registration as a
board, never in the list a suite flashes -- and the ones that stop a jumper
being recorded where it would break a board: the flash bus, the console,
native USB, a strapping pin.
"""

from __future__ import annotations

import importlib.util
import json
import queue
import re
import sys
from argparse import Namespace
from contextlib import nullcontext
from pathlib import Path

import pytest
from alteriom_hil import farm_shared
import yaml

from alteriom_hil.board import Board, BoardMap
from alteriom_hil.instrument import (
    KINDS,
    Instrument,
    InstrumentClient,
    instruments_document,
    instruments_path_for,
    load_instruments,
    validate_instruments,
    wired_to,
)
from alteriom_hil.inventory import (
    DetectedDevice,
    load_inventory_snapshot,
    publish_inventory,
    reconcile,
    write_active_map,
)
from alteriom_hil.pins import WIREABLE_PINS, check_wireable
from alteriom_hil.protocol import ProtocolError

REPO = Path(__file__).resolve().parents[1]
RUNNER = REPO / "runner"
# The rig's own scripts, examples and schemas, which are beside its package
# now (docs/public-release-plan.md, step 12f).
RIG = REPO / "rig"
FIRMWARE = REPO / "instruments" / "esp32-io" / "firmware" / "src" / "main.cpp"

C6 = Board(id="esp32-c6-01", port="/dev/ttyACM0", chip="esp32c6", target="esp32-c6", mac="40:4c:ca:00:00:01")
CLASSIC = Board(id="esp32-01", port="/dev/ttyUSB0", chip="esp32", target="esp32", mac="24:6f:28:00:00:01")
IO_MAC = "24:6f:28:00:00:99"


def io(**overrides) -> Instrument:
    fields = {"id": "io-01", "kind": "esp32-io", "mac": IO_MAC, "port": "/dev/ttyUSB3"}
    fields.update(overrides)
    return Instrument(**fields)


# ---- the firmware and the farm agree on what the instrument is ---------------


def test_the_firmware_channel_table_is_the_farms():
    """Two tables of one thing drift. A channel the farm believes can drive and
    the firmware refuses fails at the bench; one the firmware drives and the
    farm believes input-only is a wire the validation lets both ends fight on."""
    source = FIRMWARE.read_text(encoding="utf-8")
    table = source.split("static const Channel CHANNELS[] = {", 1)[1].split("};", 1)[0]
    entries = re.findall(r"\{(\d+),\s*(true|false),\s*(true|false),\s*(true|false)\}", table)
    firmware = {int(pin): (drive == "true", pull == "true", adc == "true") for pin, drive, pull, adc in entries}
    farm = {pin: (ch.drive, ch.pull, ch.adc) for pin, ch in KINDS["esp32-io"].channels.items()}
    assert firmware == farm
    assert 'KIND = "esp32-io"' in source and "PROTOCOL_VERSION = 1" in source
    # And it leaves out what it must: an instrument is an ESP32 too, and
    # driving its own strapping, console or flash pins would break it.
    assert not set(firmware) & {0, 1, 2, 3, 5, 6, 7, 8, 9, 10, 11, 12, 15}


def test_every_channel_starts_and_ends_harmless():
    source = FIRMWARE.read_text(encoding="utf-8")
    setup = source.split("void setup()", 1)[1].split("void loop()", 1)[0]
    assert "releaseAll();" in setup, "an instrument boots with every channel an input"
    release = source.split("void releaseAll()", 1)[1].split("\n}", 1)[0]
    assert "pinMode(CHANNELS[i].pin, INPUT);" in release


# ---- where a jumper may land ---------------------------------------------------


def test_a_jumper_may_not_land_on_a_pin_that_breaks_the_board():
    forbidden = {
        "esp32": {0, 1, 2, 3, 5, 6, 7, 8, 9, 10, 11, 12, 15},
        "esp32-c3": {2, 8, 9, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21},
        "esp32-c6": {4, 5, 8, 9, 12, 13, 15, 16, 17, 24, 25, 26, 27, 28, 29, 30},
        "esp32-s3": {0, 3, 19, 20, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 43, 44, 45, 46, 47, 48},
        "esp8266": {0, 1, 2, 3, 6, 7, 8, 9, 10, 11, 15, 16},
    }
    for target, pins in forbidden.items():
        assert not WIREABLE_PINS[target] & pins, target
        for pin in pins:
            with pytest.raises(ValueError, match="not a wireable pin"):
                check_wireable(target, pin)
    # A family nobody has checked cannot be wired at all.
    with pytest.raises(ValueError, match="no pin table for esp32-c5"):
        check_wireable("esp32-c5", 2)


def test_wiring_is_checked_where_it_is_written():
    boards = [C6, CLASSIC]
    ok = io(wiring=[{"channel": 25, "board": "esp32-c6-01", "pin": 6}, {"channel": 34, "board": "esp32-c6-01", "pin": 7}])
    validate_instruments([ok], boards)

    cases = [
        (io(wiring=[{"channel": 2, "board": "esp32-c6-01", "pin": 6}]), "has no channel 2"),
        (io(wiring=[{"channel": 25, "board": "esp32-c6-01", "pin": 6}, {"channel": 25, "board": "esp32-c6-01", "pin": 7}]), "wired twice"),
        (io(wiring=[{"channel": 25, "board": "nowhere", "pin": 6}]), "not a registered board"),
        (io(wiring=[{"channel": 25, "board": "esp32-c6-01", "pin": 9}]), "not a wireable pin"),
        # An input-only channel to an input-only pin: nothing can drive it.
        (io(wiring=[{"channel": 34, "board": "esp32-01", "pin": 35}]), "both input-only"),
        (io(mac=CLASSIC.mac), "registered as a board too"),
        (io(id="esp32-01"), "registered board's"),
    ]
    for instrument, message in cases:
        with pytest.raises(ValueError, match=message):
            validate_instruments([instrument], boards)

    # One board pin wired from two instruments.
    other = io(id="io-02", mac="24:6f:28:00:00:98", wiring=[{"channel": 26, "board": "esp32-c6-01", "pin": 6}])
    with pytest.raises(ValueError, match="GPIO6 is wired to both"):
        validate_instruments([ok, other], boards)
    with pytest.raises(ValueError, match="duplicate instrument ids"):
        validate_instruments([io(), io(mac="24:6f:28:00:00:98")])
    with pytest.raises(ValueError, match="unknown kind"):
        io(kind="rp2040-io")
    assert wired_to([ok], "esp32-c6-01") == ["io-01 channel 25", "io-01 channel 34"]


def test_the_registry_round_trips_and_lives_beside_the_boards(tmp_path):
    registry = tmp_path / "inventory.yaml"
    path = instruments_path_for(registry)
    assert path == tmp_path / "instruments.yaml"
    assert load_instruments(path) == [], "no file is no instruments, not an error"
    written = [io(usb_path="hub-1", wiring=[{"channel": 25, "board": "esp32-c6-01", "pin": 6, "note": "button"}])]
    path.write_text(instruments_document(written))
    document = yaml.safe_load(path.read_text())
    assert "usb_path" not in document["instruments"][0], "a transport detail, like a board's"
    loaded = load_instruments(path, [C6])
    assert loaded[0].wiring[0].note == "button" and loaded[0].mac == IO_MAC


# ---- the inventory keeps it apart from the boards --------------------------------


def test_an_instrument_is_claimed_before_anything_can_take_it_for_a_board(tmp_path):
    devices = [
        DetectedDevice("/dev/ttyACM0", "esp32c6", "esp32-c6", C6.mac),
        DetectedDevice("/dev/ttyUSB3", "esp32", "esp32", IO_MAC, usb_path="hub-7"),
    ]
    # A legacy board registered by port alone, on the port the instrument is
    # on now: it must not claim the instrument.
    by_port = Board(id="old", port="/dev/ttyUSB3", chip="esp32", target="esp32")
    result = reconcile([C6, by_port], devices, [io(port="/dev/stale")])
    assert [board.id for board in result.boards] == ["esp32-c6-01"]
    assert result.missing == ["old"]
    assert result.unregistered == [], "never offered for registration as a board"
    assert [(item.id, item.port, item.usb_path) for item in result.instruments] == [("io-01", "/dev/ttyUSB3", "hub-7")]

    # Absent, it is a missing instrument, not a missing board.
    assert reconcile([C6], devices[:1], [io()]).missing_instruments == ["io-01"]
    # And an ESP answering at that MAC as another family is not the instrument.
    with pytest.raises(ValueError, match="identifies as esp32-c6"):
        reconcile([], [DetectedDevice("/dev/ttyACM9", "esp32c6", "esp32-c6", IO_MAC)], [io()])

    # The active map carries it beside the boards, where a suite that knows
    # nothing of instruments never reads it.
    active = tmp_path / "active.yaml"
    write_active_map(active, result)
    assert [board.id for board in BoardMap.load(active)] == ["esp32-c6-01"]
    assert yaml.safe_load(active.read_text())["instruments"][0]["id"] == "io-01"


def test_publishing_the_fleet_reads_the_instrument_registry(tmp_path, monkeypatch):
    registry = tmp_path / "inventory.yaml"
    registry.write_text(yaml.safe_dump({"boards": [{"id": C6.id, "port": C6.port, "chip": C6.chip, "target": C6.target, "mac": C6.mac}]}))
    instruments_path_for(registry).write_text(instruments_document([io(wiring=[{"channel": 25, "board": C6.id, "pin": 6}])]))
    devices = {
        "/dev/ttyACM0": DetectedDevice("/dev/ttyACM0", "esp32c6", "esp32-c6", C6.mac),
        "/dev/ttyUSB3": DetectedDevice("/dev/ttyUSB3", "esp32", "esp32", IO_MAC),
    }
    monkeypatch.setattr("alteriom_hil.inventory.serial_ports", lambda: sorted(devices))
    snapshot = publish_inventory(registry, tmp_path / "active.yaml", tmp_path / "state", probe=devices.__getitem__)
    assert [board["id"] for board in snapshot["boards"]] == [C6.id]
    assert snapshot["instruments"][0]["id"] == "io-01" and snapshot["instruments"][0]["target"] == "esp32"
    assert snapshot["unregistered"] == []

    # A second instrument registered since: missing until discovery sees it,
    # and its MAC no longer offered as an unregistered board.
    state = tmp_path / "state"
    stale = json.loads((state / "inventory.json").read_text())
    stale["unregistered"] = [{"port": "/dev/ttyUSB4", "chip": "esp32", "target": "esp32", "mac": "24:6f:28:00:00:98"}]
    (state / "inventory.json").write_text(json.dumps(stale))
    instruments_path_for(registry).write_text(instruments_document([io(), io(id="io-02", mac="24:6f:28:00:00:98")]))
    seen = load_inventory_snapshot(state, registry)
    assert seen["missing_instruments"] == ["io-02"] and seen["unregistered"] == []
    assert seen["registered_instruments"] == 2


# ---- the service will not register one as a board --------------------------------


def _service():
    # The launcher, a console script now (docs/public-release-plan.md, 12e).
    from alteriom_hil import launcher

    return launcher


def test_the_service_refuses_an_instrument_as_a_board_and_a_wired_board_going(tmp_path, monkeypatch):
    farm_service = _service()
    registry = tmp_path / "inventory.yaml"
    registry.write_text(yaml.safe_dump({"boards": [{"id": C6.id, "port": C6.port, "chip": C6.chip, "target": C6.target, "mac": C6.mac}]}))
    instruments_path_for(registry).write_text(instruments_document([io(wiring=[{"channel": 25, "board": C6.id, "pin": 6}])]))
    manager = farm_service.FarmManager.__new__(farm_service.FarmManager)
    manager.registry = registry
    manager.board_map = tmp_path / "active.yaml"
    manager.state = tmp_path

    from alteriom_hil import rig_manager  # the rig's half reads its own names

    monkeypatch.setattr(farm_shared, "RIG_LOCK_PATH", tmp_path / "rig.lock")
    monkeypatch.setattr(
        rig_manager, "discover",
        lambda: ([DetectedDevice("/dev/ttyUSB3", "esp32", "esp32", IO_MAC)], []),
    )
    with pytest.raises(ValueError, match="is the instrument io-01, not a board"):
        manager.register_device("esp32-02", IO_MAC)
    with pytest.raises(ValueError, match="an instrument's id"):
        manager.register_device("io-01", "24:6f:28:00:00:55")
    with pytest.raises(ValueError, match="wired to io-01 channel 25; unwire it first"):
        manager.unregister_device(C6.id)


# ---- the admin CLI ---------------------------------------------------------------------


def test_the_admin_cli_registers_wires_and_unwires_an_instrument(tmp_path, monkeypatch):
    sys.path.insert(0, str(RUNNER))
    from alteriom_hil import admin_cli

    inventory = tmp_path / "inventory.yaml"
    inventory.write_text(yaml.safe_dump({"boards": [{"id": C6.id, "port": C6.port, "chip": C6.chip, "target": C6.target, "mac": C6.mac}]}))
    config = yaml.safe_load((RIG / "hil-config.example.yaml").read_text())
    config["paths"]["inventory"] = str(inventory)
    config["paths"]["board_map"] = str(tmp_path / "board-map.active.yaml")
    config["paths"]["state"] = str(tmp_path / "state")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    monkeypatch.setattr(admin_cli, "require_root", lambda: None)
    monkeypatch.setattr(admin_cli, "publish_after_change", lambda payload: None)
    monkeypatch.setattr(admin_cli, "refresh_health", lambda: None)

    assert admin_cli.command_instruments_add(Namespace(config=config_path, id="io-01", kind="esp32-io", mac=IO_MAC.upper(), port="/dev/ttyUSB3")) == 0
    wire = Namespace(config=config_path, id="io-01", channel=25, board=C6.id, pin=6, note="button")
    assert admin_cli.command_instruments_wire(wire) == 0
    stored = load_instruments(instruments_path_for(inventory), [C6])
    assert stored[0].mac == IO_MAC and stored[0].wiring[0].pin == 6

    with pytest.raises(admin_cli.hil_config.ConfigError, match="already wired"):
        admin_cli.command_instruments_wire(Namespace(**{**vars(wire), "pin": 7}))
    with pytest.raises(admin_cli.hil_config.ConfigError, match="not a wireable pin"):
        admin_cli.command_instruments_wire(Namespace(**{**vars(wire), "channel": 26, "pin": 12}))
    with pytest.raises(admin_cli.hil_config.ConfigError, match="registered as a board too"):
        admin_cli.command_instruments_add(Namespace(config=config_path, id="io-02", kind="esp32-io", mac=C6.mac, port="/dev/x"))
    # A board with a wire on it cannot quietly go.
    with pytest.raises(admin_cli.hil_config.ConfigError, match="unwire it first"):
        admin_cli.command_boards_remove(Namespace(config=config_path, id=C6.id))

    assert admin_cli.command_instruments_unwire(Namespace(config=config_path, id="io-01", channel=25)) == 0
    assert admin_cli.command_instruments_remove(Namespace(config=config_path, id="io-01")) == 0
    assert load_instruments(instruments_path_for(inventory)) == []
    # The board registry was never rewritten by any of it.
    assert yaml.safe_load(inventory.read_text())["boards"][0]["id"] == C6.id

    parsed = admin_cli.parser().parse_args(["instruments", "wire", "io-01", "--channel", "25", "--board", C6.id, "--pin", "6"])
    assert parsed.func is admin_cli.command_instruments_wire


def test_discovery_offers_an_unregistered_esp32_as_an_instrument_too(tmp_path, monkeypatch, capsys):
    sys.path.insert(0, str(RUNNER))
    from alteriom_hil import admin_cli

    inventory = tmp_path / "inventory.yaml"
    inventory.write_text(yaml.safe_dump({"boards": [{"id": C6.id, "port": C6.port, "chip": C6.chip, "target": C6.target, "mac": C6.mac}]}))
    config = yaml.safe_load((RIG / "hil-config.example.yaml").read_text())
    config["paths"]["inventory"] = str(inventory)
    config["paths"]["board_map"] = str(tmp_path / "board-map.active.yaml")
    config["paths"]["state"] = str(tmp_path / "state")
    # The offer is for a rig that does not register its own boards. One that
    # does registers the instrument as a board, and `instruments add
    # --replace-board` is how an operator says which it is -- the test below.
    config["inventory"] = {"auto_register": False}
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    found = {
        "/dev/ttyACM0": DetectedDevice("/dev/ttyACM0", "esp32c6", "esp32-c6", C6.mac),
        "/dev/ttyUSB3": DetectedDevice("/dev/ttyUSB3", "esp32", "esp32", IO_MAC),
    }
    monkeypatch.setattr("alteriom_hil.inventory.serial_ports", lambda: sorted(found))
    monkeypatch.setattr("alteriom_hil.inventory.probe_port", lambda port: found[port])
    monkeypatch.setattr(admin_cli, "rig_lock", nullcontext)
    admin_cli.command_boards_discover(Namespace(config=config_path, json=False))
    out = capsys.readouterr().out
    assert f"instruments add --id <id> --mac {IO_MAC} --port /dev/ttyUSB3 --kind esp32-io" in out


# ---- talking to one ------------------------------------------------------------------


class FakeCapture:
    def __init__(self, replies):
        self.replies = replies  # cmd -> reply event, or a callable of the command
        self.events = queue.Queue()
        self.writes = []
        self.raw_log = []

    def write_line(self, value):
        command = json.loads(value)
        self.writes.append(command)
        reply = self.replies.get(command["cmd"])
        if callable(reply):
            reply = reply(command)
        if reply is not None:
            self.events.put(reply)

    def next_event(self, timeout):
        try:
            return self.events.get(timeout=min(timeout, 0.05))
        except queue.Empty:
            return None

    def drain(self):
        return []


INFO = {"evt": "info", "role": "instrument", "kind": "esp32-io", "protocol": 1, "fw": "abc", "mac": IO_MAC}


def test_the_client_refuses_a_device_that_is_not_an_instrument():
    board = InstrumentClient("io-01", FakeCapture({"info": {"evt": "info", "role": None, "node": 7}}))
    with pytest.raises(ProtocolError, match="not an instrument: is the esp32-io firmware flashed"):
        board.info(timeout=0.5)
    newer = InstrumentClient("io-01", FakeCapture({"info": {**INFO, "protocol": 2}}))
    with pytest.raises(ProtocolError, match="speaks instrument protocol 2"):
        newer.info(timeout=0.5)
    assert InstrumentClient("io-01", FakeCapture({"info": INFO})).info(timeout=0.5)["fw"] == "abc"


def test_the_client_speaks_the_firmware_protocol():
    capture = FakeCapture({
        "write": lambda c: {"evt": "write", "ch": c["ch"], "level": c["level"]},
        "read": lambda c: {"evt": "read", "ch": c["ch"], "level": 1},
        "adc": lambda c: {"evt": "adc", "ch": c["ch"], "mv": 1650},
        "count": lambda c: {"evt": "count", "ch": c["ch"], "edges": 12, "ms": c["ms"]},
        "wait_edge": lambda c: {"evt": "edge", "ch": c["ch"], "timeout": True},
        "mode": lambda c: {"evt": "error", "cmd": "mode", "error": "this channel has no internal pulls"},
        "release": {"evt": "release", "ok": True},
    })
    client = InstrumentClient("io-01", capture)
    client.write(25, True)
    assert client.read(34) == 1
    assert client.millivolts(34) == 1650
    assert client.count(34, 500) == 12
    assert client.wait_edge(34, timeout_ms=100) is None
    client.release()
    assert capture.writes[0] == {"cmd": "write", "ch": 25, "level": 1}
    assert capture.writes[3] == {"cmd": "count", "ch": 34, "ms": 500, "edge": "rising"}
    # What the instrument refuses is raised with its reason.
    with pytest.raises(ProtocolError, match="mode refused: this channel has no internal pulls"):
        client.mode(25, "pullup")
    # What the farm already knows it cannot do never reaches the wire.
    sent = len(capture.writes)
    for call, message in (
        (lambda: client.write(34, 1), "input-only"),
        (lambda: client.millivolts(25), "cannot measure a voltage"),
        (lambda: client.mode(34, "pullup"), "no internal pulls"),
        (lambda: client.read(2), "no channel 2"),
    ):
        with pytest.raises(ValueError, match=message):
            call()
    assert len(capture.writes) == sent


def test_an_instrument_a_rig_registered_as_a_board_is_corrected_not_refused(tmp_path, monkeypatch):
    """On a rig that registers its own boards, an instrument arrives as one:
    discovery cannot tell them apart and only an operator can. The refusal
    stays -- flashing a suite onto an instrument is what it prevents -- and
    `--replace-board` is the operator saying which it is."""
    sys.path.insert(0, str(RUNNER))
    from alteriom_hil import admin_cli
    from alteriom_hil.inventory import load_registry

    inventory = tmp_path / "inventory.yaml"
    inventory.write_text(yaml.safe_dump({"boards": [
        {"id": C6.id, "port": C6.port, "chip": C6.chip, "target": C6.target, "mac": C6.mac},
        {"id": "esp32-0099", "port": "/dev/ttyUSB3", "chip": "esp32", "target": "esp32", "mac": IO_MAC},
    ]}))
    config = yaml.safe_load((RIG / "hil-config.example.yaml").read_text())
    config["paths"]["inventory"] = str(inventory)
    config["paths"]["board_map"] = str(tmp_path / "board-map.active.yaml")
    config["paths"]["state"] = str(tmp_path / "state")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    monkeypatch.setattr(admin_cli, "require_root", lambda: None)
    monkeypatch.setattr(admin_cli, "publish_after_change", lambda payload: None)

    refused = Namespace(config=config_path, id="io-01", mac=IO_MAC, port="/dev/ttyUSB3",
                        kind="esp32-io", replace_board=False)
    with pytest.raises(Exception, match="registered as a board"):
        admin_cli.command_instruments_add(refused)

    corrected = Namespace(config=config_path, id="io-01", mac=IO_MAC, port="/dev/ttyUSB3",
                          kind="esp32-io", replace_board=True)
    assert admin_cli.command_instruments_add(corrected) == 0
    assert [board.id for board in load_registry(inventory)] == [C6.id]
