import os
import stat

from alteriom_hil.board import TARGET_CHIPS, Board
import pytest

from alteriom_hil.inventory import (
    CHIP_TARGETS,
    DetectedDevice,
    discover,
    load_inventory_snapshot,
    load_registry,
    parse_esptool_details,
    parse_esptool_identity,
    probe_details,
    probe_port,
    publish_inventory,
    reconcile,
    stable_serial_port,
    validate_registry,
    write_registry,
)


def device(port, target, mac, path=None):
    return DetectedDevice(port, TARGET_CHIPS[target], target, mac, path)


# --- esptool output, as printed by the versions the farm has met -------------

ESPTOOL_V4_ESP32 = """esptool.py v4.7.0
Serial port /dev/ttyUSB0
Connecting....
Detecting chip type... Unsupported detection protocol, switching and trying again...
Connecting....
Detecting chip type... ESP32
Chip is ESP32-D0WD-V3 (revision v3.1)
Features: WiFi, BT, Dual Core, 240MHz, VRef calibration in efuse, Coding Scheme None
Crystal is 40MHz
MAC: 24:6f:28:aa:bb:cc
Uploading stub...
Running stub...
Stub running...
Warning: ESP32 has no Chip ID. Reading MAC instead.
MAC: 24:6f:28:aa:bb:cc
Hard resetting via RTS pin...
"""

ESPTOOL_V4_C6 = """esptool.py v4.8.1
Serial port /dev/ttyACM2
Connecting...
Detecting chip type... ESP32-C6
Chip is ESP32-C6 (QFN40) (revision v0.1)
Features: WiFi 6, BT 5, IEEE802.15.4
Crystal is 40MHz
MAC: 40:4c:ca:ff:fe:41:0f:7c
BASE MAC: 40:4c:ca:41:0f:7c
MAC_EXT: ff:fe
Uploading stub...
Warning: ESP32-C6 has no Chip ID. Reading MAC instead.
MAC: 40:4c:ca:ff:fe:41:0f:7c
Hard resetting via RTS pin...
"""

ESPTOOL_V5_C5 = """esptool v5.0.2
Connected to ESP32-C5 on /dev/ttyUSB2:
Chip type:          ESP32-C5 (QFN40) (revision v1.0)
Features:           Wi-Fi 6 (dual-band), BT 5 (LE), IEEE802.15.4, Single Core + LP Core, 240MHz
Crystal frequency:  40MHz
USB mode:           USB-Serial/JTAG
MAC:                60:55:f9:ff:fe:12:34:56
BASE MAC:           60:55:f9:12:34:56
MAC_EXT:            ff:fe

Stub flasher running.
Chip ID:  Warning: ESP32-C5 has no chip ID. Reading MAC address instead.
MAC: 60:55:f9:ff:fe:12:34:56

Hard resetting via RTS pin...
"""

ESPTOOL_V4_ESP8266 = """esptool.py v4.7.0
Serial port /dev/ttyUSB3
Connecting....
Detecting chip type... Unsupported detection protocol, switching and trying again...
Connecting....
Detecting chip type... ESP8266
Chip is ESP8266EX
Features: WiFi
Crystal is 26MHz
MAC: 5c:cf:7f:11:22:33
Uploading stub...
Running stub...
Stub running...
Chip ID: 0x00112233
Hard resetting via RTS pin...
"""

ESPTOOL_V5_S3 = """esptool v5.0.2
Connected to ESP32-S3 on /dev/ttyACM1:
Chip type:          ESP32-S3 (QFN56) (revision v0.2)
Features:           Wi-Fi, BT 5 (LE), Dual Core + LP Core, 240MHz, Embedded PSRAM 8MB (AP_3v3)
Crystal frequency:  40MHz
USB mode:           USB-Serial/JTAG
MAC:                f4:12:fa:aa:bb:01

Stub flasher running.
Chip ID:  Warning: ESP32-S3 has no chip ID. Reading MAC address instead.
MAC: f4:12:fa:aa:bb:01
"""


def test_a_rig_with_nothing_registered_yet_can_still_be_read(tmp_path):
    """The state every rig starts in. Discovery and registering the first
    board both read the registry first, so refusing an absent or empty one
    made those the two commands a new rig could not run."""
    assert load_registry(tmp_path / "inventory.yaml") == []

    empty = tmp_path / "empty.yaml"
    empty.write_text("boards: []\n", encoding="utf-8")
    assert load_registry(empty) == []

    nothing = tmp_path / "nothing.yaml"
    nothing.write_text("", encoding="utf-8")
    assert load_registry(nothing) == []

    # A registry that is wrong is still refused: this is about one not
    # written yet, not about one that contradicts itself.
    duplicated = tmp_path / "duplicated.yaml"
    duplicated.write_text(
        "boards:\n"
        "  - {id: esp32-01, port: /dev/ttyUSB0, target: esp32, chip: esp32}\n"
        "  - {id: esp32-01, port: /dev/ttyUSB1, target: esp32, chip: esp32}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate board ids"):
        load_registry(duplicated)


@pytest.mark.parametrize(
    "output, family, mac",
    [
        (ESPTOOL_V4_ESP32, "esp32", "24:6f:28:aa:bb:cc"),
        (ESPTOOL_V4_C6, "esp32c6", "40:4c:ca:41:0f:7c"),
        (ESPTOOL_V5_C5, "esp32c5", "60:55:f9:12:34:56"),
        (ESPTOOL_V4_ESP8266, "esp8266", "5c:cf:7f:11:22:33"),
        (ESPTOOL_V5_S3, "esp32s3", "f4:12:fa:aa:bb:01"),
    ],
)
def test_esptool_identity_is_parsed_for_every_family_and_esptool_version(output, family, mac):
    identity = parse_esptool_identity(output)
    assert (identity.family, identity.mac) == (family, mac)
    assert family in CHIP_TARGETS, "every parsed family must be a farm target"


def test_esptool_identity_derives_the_base_mac_from_an_eui64_without_a_base_mac_line():
    output = ESPTOOL_V4_C6.replace("BASE MAC: 40:4c:ca:41:0f:7c\n", "")
    assert parse_esptool_identity(output).mac == "40:4c:ca:41:0f:7c"


def test_esptool_identity_names_what_is_missing():
    with pytest.raises(ValueError, match="no chip identification"):
        parse_esptool_identity("Serial port /dev/ttyUSB9\nConnecting........_____....._____\n")
    with pytest.raises(ValueError, match="no MAC address"):
        parse_esptool_identity("Detecting chip type... ESP32-C3\nChip is ESP32-C3 (QFN32)\n")
    with pytest.raises(ValueError, match="unrecognised chip name"):
        parse_esptool_identity("Chip is ESPRESSIF-UNKNOWN\nMAC: 00:11:22:33:44:55\n")


def _fake_esptool(tmp_path, output, exit_code=0):
    """A stand-in for the interpreter that runs `-m esptool`."""
    script = tmp_path / "python"
    script.write_text(f"#!/bin/sh\ncat <<'EOF'\n{output}EOF\nexit {exit_code}\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


def test_probe_port_identifies_a_c6_on_native_usb(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "alteriom_hil.inventory._udev_properties",
        lambda port: {"ID_VENDOR_ID": "303a", "ID_PATH": "platform-xhci-hcd.1-usb-0:1.1.3:1.0",
                      "ID_SERIAL": "Espressif_USB_JTAG_serial_debug_unit_40:4C:CA:41:0F:7C"},
    )
    found = probe_port("/dev/ttyACM2", python=_fake_esptool(tmp_path, ESPTOOL_V4_C6))
    assert (found.chip, found.target, found.mac) == ("esp32c6", "esp32-c6", "40:4c:ca:41:0f:7c")
    assert found.transport == "usb-serial-jtag"
    assert found.usb_path == "platform-xhci-hcd.1-usb-0:1.1.3:1.0"


def test_probe_port_identifies_an_esp8266_on_a_uart_bridge(tmp_path, monkeypatch):
    monkeypatch.setattr("alteriom_hil.inventory._udev_properties", lambda port: {"ID_VENDOR_ID": "1a86"})
    found = probe_port("/dev/ttyUSB3", python=_fake_esptool(tmp_path, ESPTOOL_V4_ESP8266))
    assert (found.target, found.mac, found.transport) == ("esp8266", "5c:cf:7f:11:22:33", "uart-bridge")


def test_probe_port_errors_carry_the_esptool_tail_and_name_unsupported_families(tmp_path, monkeypatch):
    monkeypatch.setattr("alteriom_hil.inventory._udev_properties", lambda port: {})
    silent = "esptool.py v4.7.0\nSerial port /dev/ttyUSB9\nConnecting........_____.....\n"
    with pytest.raises(RuntimeError, match=r"cannot identify /dev/ttyUSB9: no chip identification .*Connecting"):
        probe_port("/dev/ttyUSB9", python=_fake_esptool(tmp_path, silent))
    with pytest.raises(RuntimeError, match="esptool probe failed for /dev/ttyUSB9: .*Connecting"):
        probe_port("/dev/ttyUSB9", python=_fake_esptool(tmp_path, silent, exit_code=2))
    h2 = ESPTOOL_V4_C6.replace("ESP32-C6", "ESP32-H2")
    with pytest.raises(RuntimeError, match="esp32h2 on /dev/ttyACM5 .*not a supported farm target; supported: esp32, esp32c3"):
        probe_port("/dev/ttyACM5", python=_fake_esptool(tmp_path, h2))


def test_probe_port_rebinds_the_usb_device_once_when_the_serial_stream_stops(monkeypatch):
    # A CP2102 that stopped mid-transfer answered every later probe with
    # "serial data stream stopped" and the board was reported missing from
    # every run — until a driver re-bind from the shell brought it back in
    # five seconds. The probe does that itself, once.
    from alteriom_hil import inventory

    monkeypatch.setattr(inventory, "_udev_properties", lambda port: {"ID_VENDOR_ID": "10c4"})
    probes = []
    fault = "Uploading stub flasher...\nA fatal error occurred: Serial data stream stopped: Possible serial noise or corruption.\n"

    def flaky_probe(port, python):
        probes.append(port)
        return (2, fault) if len(probes) == 1 else (0, ESPTOOL_V4_ESP8266)

    rebinds = []
    monkeypatch.setattr(inventory, "_run_probe", flaky_probe)
    monkeypatch.setattr(inventory, "usb_rebind", lambda port: rebinds.append(port) or True)
    found = probe_port("/dev/ttyUSB0", python="python")
    assert found.mac == "5c:cf:7f:11:22:33"
    assert probes == ["/dev/ttyUSB0", "/dev/ttyUSB0"] and rebinds == ["/dev/ttyUSB0"]


def test_probe_port_does_not_rebind_for_a_part_that_simply_does_not_answer(monkeypatch):
    from alteriom_hil import inventory

    monkeypatch.setattr(inventory, "_udev_properties", lambda port: {})
    silent = "esptool.py v4.7.0\nSerial port /dev/ttyUSB9\nConnecting........_____.....\n"
    monkeypatch.setattr(inventory, "_run_probe", lambda port, python: (2, silent))
    rebinds = []
    monkeypatch.setattr(inventory, "usb_rebind", lambda port: rebinds.append(port) or True)
    with pytest.raises(RuntimeError, match="esptool probe failed"):
        probe_port("/dev/ttyUSB9", python="python")
    assert rebinds == []


def test_usb_rebind_is_a_no_op_for_a_port_that_is_not_on_usb(monkeypatch):
    from alteriom_hil import inventory

    monkeypatch.setattr(inventory, "usb_device_of", lambda port: None)
    assert inventory.usb_rebind("/dev/ttyS0") is False


def test_reconcile_follows_mac_when_port_changes():
    configured = Board(
        id="s3-a", port="/dev/ttyACM0", chip="esp32s3", target="esp32-s3",
        mac="aa:bb:cc:dd:ee:01"
    )
    found = device("/dev/ttyACM4", "esp32-s3", "aa:bb:cc:dd:ee:01", "hub-4")
    result = reconcile([configured], [found])
    assert result.boards[0].port == "/dev/ttyACM4"
    assert result.boards[0].usb_path == "hub-4"
    assert not result.missing


def test_stable_serial_port_prefers_matching_identity_symlink(tmp_path):
    tty = tmp_path / "ttyACM2"
    tty.touch()
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    alias = by_id / "usb-Espressif_debug_aa:bb-if00"
    alias.symlink_to(tty)
    assert stable_serial_port(
        str(tty),
        {"ID_SERIAL": "Espressif_debug_aa:bb", "ID_SERIAL_SHORT": "aa:bb"},
        by_id,
    ) == str(alias)


def test_stable_serial_port_keeps_tty_without_unique_identity(tmp_path):
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    assert stable_serial_port("/dev/ttyUSB0", {}, by_id) == "/dev/ttyUSB0"


def test_stable_serial_port_rejects_placeholder_usb_uart_serial(tmp_path):
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    alias = by_id / "usb-Silicon_Labs_CP2102_0001-if00-port0"
    alias.symlink_to(tmp_path / "ttyUSB0")
    properties = {"ID_SERIAL": "Silicon_Labs_CP2102_0001", "ID_SERIAL_SHORT": "0001"}
    assert stable_serial_port("/dev/ttyUSB1", properties, by_id) == "/dev/ttyUSB1"


def test_reconcile_reports_missing_and_unregistered():
    configured = Board(
        id="classic", port="/dev/old", mac="aa:bb:cc:dd:ee:01"
    )
    unknown = device("/dev/ttyACM2", "esp32-c3", "aa:bb:cc:dd:ee:02")
    result = reconcile([configured], [unknown])
    assert result.missing == ["classic"]
    assert result.unregistered == [unknown]


def test_discover_keeps_probe_errors_without_hiding_good_devices():
    good = device("/dev/good", "esp32", "aa:bb:cc:dd:ee:01")

    def probe(port):
        if port == "/dev/bad":
            raise RuntimeError("no response")
        return good

    devices, errors = discover(["/dev/good", "/dev/bad"], probe)
    assert devices == [good]
    assert errors[0]["port"] == "/dev/bad"


def test_registry_allows_stale_duplicate_ports_but_not_duplicate_identity():
    first = Board(
        id="c3",
        port="/dev/ttyACM0",
        chip="esp32c3",
        target="esp32-c3",
        mac="aa:bb:cc:dd:ee:01",
    )
    replacement = Board(
        id="s3",
        port="/dev/ttyACM0",
        chip="esp32s3",
        target="esp32-s3",
        mac="aa:bb:cc:dd:ee:02",
    )
    assert validate_registry([first, replacement]) == [first, replacement]

    duplicate = Board(
        id="other",
        port="/dev/ttyACM1",
        chip="esp32c3",
        target="esp32-c3",
        mac=first.mac,
    )
    with pytest.raises(ValueError, match="duplicate MAC"):
        validate_registry([first, duplicate])


def test_write_registry_is_atomic_and_drops_transient_usb_path(tmp_path):
    path = tmp_path / "inventory.yaml"
    board = Board(
        id="s3",
        port="/dev/ttyACM2",
        chip="esp32s3",
        target="esp32-s3",
        mac="aa:bb:cc:dd:ee:03",
        usb_path="temporary-hub-path",
        tags=["mesh"],
    )
    write_registry(path, [board])
    assert load_registry(path)[0].mac == board.mac
    assert "usb_path" not in path.read_text(encoding="utf-8")


def test_publish_inventory_writes_the_active_map_and_the_dashboard_snapshot(tmp_path, monkeypatch):
    import json

    registry = tmp_path / "inventory.yaml"
    registry.write_text("boards:\n- {id: c6, port: /dev/old, chip: esp32c6, target: esp32-c6, mac: '40:4c:ca:41:0f:7c'}\n")
    monkeypatch.setattr("alteriom_hil.inventory.serial_ports", lambda: ["/dev/ttyACM2", "/dev/ttyUSB9"])

    def probe(port):
        if port == "/dev/ttyUSB9":
            raise RuntimeError("cannot identify /dev/ttyUSB9: no chip identification")
        return device("/dev/ttyACM2", "esp32-c6", "40:4c:ca:41:0f:7c", "hub-3")

    snapshot = publish_inventory(registry, tmp_path / "active.yaml", tmp_path / "state", probe=probe)
    assert snapshot["boards"][0]["port"] == "/dev/ttyACM2"
    assert snapshot["probe_errors"][0]["port"] == "/dev/ttyUSB9"
    assert "updated_at" in snapshot
    on_disk = json.loads((tmp_path / "state" / "inventory.json").read_text())
    assert on_disk["boards"][0]["id"] == "c6"
    assert "c6" in (tmp_path / "active.yaml").read_text()


def test_snapshot_is_reconciled_with_a_registry_that_changed_since_discovery(tmp_path):
    import json

    state = tmp_path / "state"
    state.mkdir()
    (state / "inventory.json").write_text(json.dumps({
        "boards": [{"id": "esp32-01", "target": "esp32", "mac": "aa:bb:cc:dd:ee:01", "port": "/dev/ttyUSB0"}],
        "missing": [],
        "unregistered": [{"port": "/dev/ttyACM2", "chip": "esp32c6", "target": "esp32-c6", "mac": "40:4c:ca:41:0f:7c"}],
        "probe_errors": [],
    }))
    registry = tmp_path / "inventory.yaml"
    registry.write_text(
        "boards:\n"
        "- {id: esp32-01, port: /dev/ttyUSB0, chip: esp32, target: esp32, mac: 'aa:bb:cc:dd:ee:01'}\n"
        "- {id: esp32-c6-01, port: /dev/ttyACM2, chip: esp32c6, target: esp32-c6, mac: '40:4c:ca:41:0f:7c'}\n"
        "- {id: esp8266-01, port: /dev/ttyUSB3, chip: esp8266, target: esp8266, mac: '5c:cf:7f:11:22:33'}\n"
    )
    merged = load_inventory_snapshot(state, registry)
    assert [b["id"] for b in merged["boards"]] == ["esp32-01"]
    assert merged["missing"] == ["esp32-c6-01", "esp8266-01"], "registered since the last discovery: known, not yet seen"
    assert merged["unregistered"] == [], "its MAC is registered now"
    assert merged["registered"] == 3
    assert load_inventory_snapshot(tmp_path / "nowhere")["boards"] == []


# --- advanced per-device details --------------------------------------------

# `esptool flash-id` prints the same identity banner as `chip-id` and adds the
# flash lines. Label wording is checked against esptool 4.x and 5.4.0 sources:
# v4 "Crystal is 40MHz", v5 "Crystal frequency:  40MHz" (padded to 20 columns),
# and v5 names the eFuse flash type "Flash type set in eFuse:".
FLASH_ID_V4_ESP32 = ESPTOOL_V4_ESP32.replace(
    "Warning: ESP32 has no Chip ID. Reading MAC instead.\nMAC: 24:6f:28:aa:bb:cc\n",
    "Manufacturer: c8\nDevice: 4016\nDetected flash size: 4MB\n",
)

FLASH_ID_V5_C5 = ESPTOOL_V5_C5.replace(
    "Chip ID:  Warning: ESP32-C5 has no chip ID. Reading MAC address instead.\nMAC: 60:55:f9:ff:fe:12:34:56\n",
    "Manufacturer: 5e\nDevice: 6017\nDetected flash size: 8MB\nFlash type set in eFuse: quad (4 data lines)\n",
)


def test_esptool_details_are_parsed_from_a_v4_banner():
    found = parse_esptool_details(FLASH_ID_V4_ESP32)
    assert found["description"] == "ESP32-D0WD-V3"
    assert found["revision"] == "v3.1"
    assert found["crystal"] == "40MHz"
    assert found["esptool_version"] == "4.7.0"
    assert (found["flash_manufacturer"], found["flash_device"], found["flash_size"]) == ("c8", "4016", "4MB")
    assert "Dual Core" in found["features"] and "240MHz" in found["features"]
    assert "usb_mode" not in found, "esptool 4 does not report it; absence must stay absent"


def test_esptool_details_are_parsed_from_a_v5_banner():
    found = parse_esptool_details(FLASH_ID_V5_C5)
    assert found["description"] == "ESP32-C5 (QFN40)", "the package stays, the revision does not"
    assert found["revision"] == "v1.0"
    assert found["crystal"] == "40MHz", "v5 labels it 'Crystal frequency:' with column padding"
    assert found["usb_mode"] == "USB-Serial/JTAG"
    assert found["flash_type"] == "quad (4 data lines)"
    assert (found["flash_manufacturer"], found["flash_device"], found["flash_size"]) == ("5e", "6017", "8MB")
    assert found["features"][0] == "Wi-Fi 6 (dual-band)"


def test_esptool_details_of_a_part_that_reports_almost_nothing():
    # An ESP8266 has no revision, no USB mode, and chip-id prints no flash lines.
    found = parse_esptool_details(ESPTOOL_V4_ESP8266)
    assert found["description"] == "ESP8266EX"
    assert found["features"] == ["WiFi"]
    assert found["crystal"] == "26MHz"
    assert not {"revision", "usb_mode", "flash_size", "flash_manufacturer"} & set(found)


def test_probe_details_reports_identity_and_flash_together(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "alteriom_hil.inventory._udev_properties",
        lambda port: {"ID_VENDOR_ID": "303a", "ID_PATH": "hub-4", "ID_SERIAL": "Espressif_USB_JTAG"},
    )
    found = probe_details("/dev/ttyACM2", python=_fake_esptool(tmp_path, FLASH_ID_V5_C5))
    assert (found.target, found.mac) == ("esp32-c5", "60:55:f9:12:34:56")
    assert (found.description, found.revision, found.flash_size) == ("ESP32-C5 (QFN40)", "v1.0", "8MB")
    assert found.transport == "usb-serial-jtag" and found.usb_path == "hub-4"
    assert found.probed_at, "a details record without a timestamp cannot be read as stale"


def test_probe_details_refuses_a_part_the_farm_cannot_run(tmp_path, monkeypatch):
    monkeypatch.setattr("alteriom_hil.inventory._udev_properties", lambda port: {})
    h2 = ESPTOOL_V4_C6.replace("ESP32-C6", "ESP32-H2")
    with pytest.raises(RuntimeError, match="esp32h2 on /dev/ttyACM5 .*not a supported farm target"):
        probe_details("/dev/ttyACM5", python=_fake_esptool(tmp_path, h2))
    with pytest.raises(RuntimeError, match="esptool flash-id failed for /dev/ttyUSB9"):
        probe_details("/dev/ttyUSB9", python=_fake_esptool(tmp_path, "Connecting......\n", exit_code=2))


def test_a_board_plugged_in_registers_itself(tmp_path, monkeypatch):
    """The step between "the hub is connected" and a farm that still reports
    no boards. A board on a rig is a board the rig should have, and naming it
    by hand taught nobody anything."""
    import alteriom_hil.inventory as inventory_module
    from alteriom_hil.inventory import publish_inventory, load_registry, suggested_id

    found = [device("/dev/ttyUSB0", "esp32", "6c:c8:40:34:1e:cc"),
             device("/dev/ttyUSB1", "esp32-c6", "54:32:04:aa:14:b4")]
    monkeypatch.setattr(inventory_module, "serial_ports", lambda: [item.port for item in found])
    registry = tmp_path / "inventory.yaml"
    snapshot = publish_inventory(
        registry, tmp_path / "board-map.yaml", tmp_path,
        probe=lambda port: next(item for item in found if item.port == port),
        auto_register=True,
    )
    assert snapshot["registered"] == ["esp32-1ecc", "esp32-c6-14b4"], snapshot["registered"]
    assert suggested_id(found[1]) == "esp32-c6-14b4"
    assert [board.id for board in load_registry(registry)] == ["esp32-1ecc", "esp32-c6-14b4"]
    assert not snapshot["unregistered"], "nothing is left for a person to do"

    # Run it again: the same boards, registered once.
    again = publish_inventory(
        registry, tmp_path / "board-map.yaml", tmp_path,
        probe=lambda port: next(item for item in found if item.port == port),
        auto_register=True,
    )
    assert again["registered"] == []
    assert len(load_registry(registry)) == 2


def test_nothing_registers_itself_when_the_rig_says_not_to(tmp_path, monkeypatch):
    """A rig whose bank is deliberate -- a board on loan, a bench experiment --
    keeps the choice."""
    import alteriom_hil.inventory as inventory_module
    from alteriom_hil.inventory import publish_inventory, load_registry

    found = [device("/dev/ttyUSB0", "esp32", "6c:c8:40:34:1e:cc")]
    monkeypatch.setattr(inventory_module, "serial_ports", lambda: [found[0].port])
    registry = tmp_path / "inventory.yaml"
    snapshot = publish_inventory(
        registry, tmp_path / "board-map.yaml", tmp_path,
        probe=lambda port: found[0], auto_register=False,
    )
    assert snapshot["registered"] == []
    assert len(snapshot["unregistered"]) == 1
    assert load_registry(registry) == []


def test_an_instrument_is_never_registered_as_a_board(tmp_path, monkeypatch):
    """An instrument is an ESP like any board and is registered as one
    deliberately. Left to itself, discovery would take it for a board, flash a
    suite onto it and mesh it with the rig."""
    import alteriom_hil.inventory as inventory_module
    from alteriom_hil.instrument import Instrument, instruments_document, instruments_path_for
    from alteriom_hil.inventory import publish_inventory, load_registry

    registry = tmp_path / "inventory.yaml"
    registry.write_text("boards: []\n", encoding="utf-8")
    instruments_path_for(registry).write_text(
        instruments_document([Instrument(id="io-01", port="/dev/ttyUSB1", kind="esp32-io",
                                         mac="aa:bb:cc:dd:ee:ff")]),
        encoding="utf-8",
    )
    found = [device("/dev/ttyUSB0", "esp32", "6c:c8:40:34:1e:cc"),
             device("/dev/ttyUSB1", "esp32", "aa:bb:cc:dd:ee:ff")]
    monkeypatch.setattr(inventory_module, "serial_ports", lambda: [item.port for item in found])
    snapshot = publish_inventory(
        registry, tmp_path / "board-map.yaml", tmp_path,
        probe=lambda port: next(item for item in found if item.port == port),
        auto_register=True,
    )
    assert snapshot["registered"] == ["esp32-1ecc"]
    assert [board.id for board in load_registry(registry)] == ["esp32-1ecc"]
