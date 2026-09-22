"""Device descriptors and driver plugins.

The move from tables in code to documents a family or a board is described by
is only safe if it changes nothing the farm does today. So what is pinned here
first is that every table read from the documents is the table the code used
to hold, value for value; then that a document which would mislead the farm is
refused where it is written; then that a chip the farm has never run can bring
its own drivers without the farm's code changing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from alteriom_hil import devices, plugins
from alteriom_hil.board import SUPPORTED_TARGETS, TARGET_CHIPS
from alteriom_hil.devices import DescriptorError, load_devices, load_families, parse_device, parse_family, wireable_signals
from alteriom_hil.flash import flash_esptool
from alteriom_hil.pins import INPUT_ONLY_PINS, WIREABLE_PINS

REPO = Path(__file__).resolve().parents[1]


# ---- nothing the farm does changed ------------------------------------------------


def test_the_tables_read_from_the_descriptors_are_the_ones_the_code_held():
    assert TARGET_CHIPS == {
        "esp32": "esp32", "esp32-c3": "esp32c3", "esp32-c5": "esp32c5",
        "esp32-c6": "esp32c6", "esp32-s3": "esp32s3", "esp8266": "esp8266",
    }
    assert list(TARGET_CHIPS) == ["esp32", "esp32-c3", "esp32-c5", "esp32-c6", "esp32-s3", "esp8266"]
    assert SUPPORTED_TARGETS == frozenset(TARGET_CHIPS)
    assert WIREABLE_PINS == {
        "esp32": frozenset({4, 13, 14, 16, 17, 18, 19, 21, 22, 23, 25, 26, 27, 32, 33, 34, 35, 36, 39}),
        "esp32-c3": frozenset({0, 1, 3, 4, 5, 6, 7, 10}),
        "esp32-c6": frozenset({0, 1, 2, 3, 6, 7, 10, 11, 18, 19, 20, 21, 22, 23}),
        "esp32-s3": frozenset({1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 21, 39, 40, 41, 42}),
        "esp8266": frozenset({4, 5, 12, 13, 14}),
    }
    assert INPUT_ONLY_PINS == {"esp32": frozenset({34, 35, 36, 39})}
    sys.path.insert(0, str(REPO / "runner"))
    from alteriom_hil import admin_cli

    assert admin_cli.NATIVE_USB_TARGETS == {"esp32-c3", "esp32-c5", "esp32-c6", "esp32-s3"}
    # And every family flashes through the flasher it always did.
    for target in TARGET_CHIPS:
        assert plugins.flasher_for(target) is flash_esptool, target


def test_every_family_says_why_a_pin_is_not_wireable():
    for name, family in load_families().items():
        assert family.gpios, name
        if family.wireable is None:
            assert name == "esp32-c5", "only a family nobody has checked has no table"
            continue
        reserved = frozenset().union(*family.reserved.values())
        assert not reserved & family.wireable, name
        assert family.input_only <= family.wireable, name
        # The strapping, console and flash pins are named, not just absent.
        assert {"strapping", "flash"} <= set(family.reserved), name


def test_the_generic_flasher_asks_the_family_for_its_flasher(tmp_path, monkeypatch):
    sys.path.insert(0, str(REPO / "runner"))
    from alteriom_hil import flash_artifacts

    source = (REPO / "rig" / "alteriom_hil" / "flash_artifacts.py").read_text(encoding="utf-8")
    assert "flash_esptool" not in source and "flasher_for(board.target)" in source
    asked, flashed = [], []
    monkeypatch.setattr(flash_artifacts, "flasher_for", lambda target: asked.append(target) or (lambda *a, **k: flashed.append((a, k))))
    monkeypatch.setattr(flash_artifacts, "load_artifacts", lambda path: {"targets": {"esp32-c6": {"path": "i.bin", "sha256": "0" * 64}}})
    board_map = tmp_path / "map.yaml"
    board_map.write_text(yaml.safe_dump({"boards": [{"id": "c6", "port": "/dev/a", "chip": "esp32c6", "target": "esp32-c6"}]}))
    assert flash_artifacts.main(["--artifacts", str(tmp_path), "--board-map", str(board_map)]) == 0
    assert asked == ["esp32-c6"] and flashed[0][1] == {"offset": "0x0"}


# ---- a descriptor that would mislead the farm is refused ---------------------------------


def _family(**overrides):
    document = yaml.safe_load((devices.FAMILY_DIR / "esp32-c6.yaml").read_text(encoding="utf-8"))
    for dotted, value in overrides.items():
        target = document
        *path, last = dotted.split(".")
        for key in path:
            target = target[key]
        target[last] = value
    return document


def test_a_family_descriptor_that_would_mislead_is_refused():
    parse_family(_family(), "esp32-c6.yaml")
    for overrides, message in (
        ({"pins.wireable": [0, 1, 99]}, "must be a list of GPIO numbers"),
        ({"pins.wireable": [0, 1, 1]}, "lists a GPIO twice"),
        ({"pins.wireable": [0, 1, 31]}, "names GPIOs the family does not have: \\[31\\]"),
        ({"pins.wireable": [0, 1, 9]}, "GPIO \\[9\\] is both wireable and reserved for strapping"),
        ({"console": "bluetooth"}, "console must be one of"),
        ({"drivers": {"flasher": "esptool"}}, "drivers must name a flasher, identity, console, power"),
        ({"schema": 2}, "not a schema 1 family descriptor"),
        ({"chip": ""}, "chip must name what esptool calls the part"),
    ):
        with pytest.raises(DescriptorError, match=message):
            parse_family(_family(**overrides), "esp32-c6.yaml")


def test_a_family_whose_file_names_another_is_refused(tmp_path):
    (tmp_path / "esp32-c7.yaml").write_text(yaml.safe_dump(_family()))
    with pytest.raises(DescriptorError, match="does not match its filename"):
        load_families(tmp_path)


def test_a_device_is_its_signals_on_its_familys_pins():
    found = load_devices(REPO)
    assert {"esp32-c6-devkitc-1", "esp32-devkit-v1", "example-sensor-node"} <= set(found)
    node = found["example-sensor-node"]
    assert node.signal("BUTTON").pin == 18 and node.signal("BUTTON").active == "low"
    assert [signal.name for signal in wireable_signals(node)] == ["BUTTON", "STATUS_LED", "VBAT_SENSE", "I2C_SDA", "I2C_SCL"]
    # A devkit's own button is a strapping pin: a signal, never a wire.
    assert wireable_signals(found["esp32-c6-devkitc-1"]) == []
    with pytest.raises(KeyError, match="has no signal 'RELAY'"):
        node.signal("RELAY")

    base = {"schema": 1, "model": "board", "family": "esp32", "signals": {"LED": {"pin": 2, "direction": "out"}}}
    parse_device(base, "board.yaml")
    for change, message in (
        ({"family": "rp2040"}, "family must be one of"),
        ({"signals": {"LED": {"pin": 24, "direction": "out"}}}, "GPIO24, which a esp32 does not have"),
        ({"signals": {"LED": {"pin": 2, "direction": "out"}, "OTHER": {"pin": 2, "direction": "in"}}}, "both on GPIO2"),
        ({"signals": {"led": {"pin": 2, "direction": "out"}}}, "UPPER_CASE"),
        ({"signals": {"LED": {"pin": 2, "direction": "sideways"}}}, "direction must be one of"),
        ({"signals": {"SENSE": {"pin": 34, "direction": "out"}}}, "input-only on a esp32"),
        ({"signals": {"LED": {"pin": 2, "direction": "out", "pull": "sideways"}}}, "pull must be up or down"),
        ({"signals": {}}, "at least one signal"),
    ):
        with pytest.raises(DescriptorError, match=message):
            parse_device({**base, **change}, "board.yaml")


# ---- a new chip brings its own drivers -----------------------------------------------------


class _EntryPoint:
    def __init__(self, name, provided):
        self.name, self._provided = name, provided

    def load(self):
        return self._provided


def test_a_plugin_brings_drivers_for_a_chip_the_farm_has_never_run(tmp_path, monkeypatch):
    def picotool(image, board, offset):
        return f"picotool load {image} on {board.port}"

    monkeypatch.setattr(plugins, "_entry_points", lambda: [_EntryPoint("alteriom-rp2040", {
        "flasher": {"picotool": picotool},
        "identity": {"rp2040-usb": "os.path:basename"},
    })])
    rp2040 = {
        "schema": 1, "family": "rp2040", "chip": "rp2040", "console": "usb-serial-jtag",
        "drivers": {"flasher": "picotool", "identity": "rp2040-usb", "console": "usb-serial", "power": "uhubctl"},
        "pins": {"gpios": list(range(30)), "wireable": [2, 3], "input_only": [], "reserved": {"flash": [29]}},
    }
    families = {"rp2040": parse_family(rp2040, "rp2040.yaml"), **load_families()}
    assert plugins.flasher_for("rp2040", families) is picotool
    import os.path

    assert plugins.driver_for("rp2040", "identity", families) is os.path.basename, "loaded from module:attribute"
    # The ESP families are untouched by it.
    assert plugins.flasher_for("esp32-c6") is flash_esptool

    # A plugin may not take over a built-in, nor invent a kind.
    monkeypatch.setattr(plugins, "_entry_points", lambda: [_EntryPoint("evil", {"flasher": {"esptool": picotool}})])
    with pytest.raises(plugins.PluginError, match="already provided"):
        plugins.registry()
    monkeypatch.setattr(plugins, "_entry_points", lambda: [_EntryPoint("odd", {"telepathy": {"x": picotool}})])
    with pytest.raises(plugins.PluginError, match="not a driver kind"):
        plugins.registry()
    monkeypatch.setattr(plugins, "_entry_points", lambda: [])
    with pytest.raises(plugins.PluginError, match="no flasher named 'picotool'"):
        plugins.flasher_for("rp2040", families)
    with pytest.raises(plugins.PluginError, match="no family named"):
        plugins.flasher_for("rp2350")


def test_the_family_descriptors_ship_with_the_package():
    pyproject = (REPO / "core" / "pyproject.toml").read_text(encoding="utf-8")
    assert '"alteriom_hil.devices" = ["families/*.yaml"]' in pyproject
    assert sorted(path.stem for path in devices.FAMILY_DIR.glob("*.yaml")) == sorted(TARGET_CHIPS)
