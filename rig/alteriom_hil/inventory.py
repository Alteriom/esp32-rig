"""Discover ESP serial devices and reconcile them with a stable registry.

The USB tty name and hub path are transport details.  The ESP eFuse MAC is
the farm identity, so a board can move to another hub port without becoming a
different farm member.

Two kinds of member share the USB tree: boards under test, and instruments --
ESPs the farm owns as test equipment (alteriom_hil.instrument). Both answer
esptool the same way, so the registry is what tells them apart, and an
instrument's MAC is claimed as an instrument before a board or "unregistered"
can take it.
"""

from __future__ import annotations

import glob
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from .board import Board, BoardMap, TARGET_CHIPS
from .board_registry import (  # noqa: F401 -- re-exported: the descriptions live there
    DetectedDevice,
    InventoryResult,
    load_inventory_snapshot,
    load_registry,
    normalize_mac,
    reconcile,
    register_detected,
    snapshot_json,
    suggested_id,
    validate_registry,
    write_active_map,
    write_inventory_snapshot,
    write_registry,
)
from .instrument_registry import Instrument, instruments_path_for, load_instruments  # noqa: F401

CHIP_TARGETS = {chip: target for target, chip in TARGET_CHIPS.items()}


def serial_ports() -> list[str]:
    return sorted(set(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*")))


def _udev_properties(port: str) -> dict[str, str]:
    try:
        result = subprocess.run(
            ["udevadm", "info", "--query=property", "--name", port],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode:
        return {}
    return dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )


def stable_serial_port(
    port: str,
    properties: dict[str, str],
    by_id_dir: str | Path = "/dev/serial/by-id",
) -> str:
    """Prefer udev's identity symlink over a volatile ttyACM number.

    Native-USB ESP32-C3/S3 boards disconnect while resetting. Linux may then
    assign a different ``ttyACM`` number, while the ``by-id`` symlink follows
    the USB serial identity. USB-UART bridges without a useful serial retain
    their discovered tty path.
    """
    root = Path(by_id_dir)
    if not root.is_dir():
        return port
    serial = properties.get("ID_SERIAL")
    serial_short = properties.get("ID_SERIAL_SHORT", "").strip()
    # Cheap CP2102 boards commonly all report the factory placeholder 0001.
    # A by-id link made from that value is not an identity and may point at a
    # different board when several identical bridges are attached.
    if not serial_short or serial_short.lower() in {"0000", "0001", "none"}:
        return port
    candidates = sorted(item for item in root.iterdir() if item.is_symlink())
    try:
        resolved_port = Path(port).resolve(strict=True)
    except OSError:
        resolved_port = None
    for candidate in candidates:
        try:
            if resolved_port is not None and candidate.resolve(strict=True) == resolved_port:
                return str(candidate)
        except OSError:
            continue
    if serial:
        matches = [item for item in candidates if serial in item.name]
        if len(matches) == 1:
            return str(matches[0])
    return port


# esptool identifies the part in one of these lines, depending on its version
# (v4: "Detecting chip type... ESP32-C6" / "Chip is ESP32-C6 (QFN40)";
# v5: "Connected to ESP32-C6 on /dev/ttyACM0:" / "Chip type: ESP32-C6 ...").
# Auto-detection may print "Detecting chip type... Unsupported detection
# protocol, switching and trying again..." first; the family is whatever the
# last identification line names, and the marketing suffix (D0WD, PICO-D4,
# QFN32, EX) is not part of the family.
_CHIP_LINES = (
    r"^Chip (?:is|type:)\s*(ESP\S+)",
    r"^Connected to (ESP\S+) on ",
    r"Detecting chip type\.\.\.\s*(ESP\S+)",
)
# Variant letters Espressif uses for distinct silicon families (C3/C5/C6, S2/S3,
# H2, P4). Package/marketing suffixes (D0WD, PICO-D4, WROVER) belong to the
# original ESP32 and must not be mistaken for a family.
_FAMILY = re.compile(r"^ESP(?:32(?:-[CSHP]\d+)?|8266)", re.I)
# ESP32-C5/C6/H2 carry an 8-byte EUI-64 ("MAC: 40:4c:ca:ff:fe:41:0f:7c"); the
# 6-byte base MAC follows on its own line in esptool >= 4.7 and is otherwise
# the EUI-64 with the ff:fe insert removed. The farm identity is always the
# 6-byte base MAC.
_MAC_LINE = re.compile(r"^\s*(BASE MAC|MAC):\s*((?:[0-9a-f]{2}:){5,7}[0-9a-f]{2})\s*$", re.I | re.M)


@dataclass(frozen=True)
class EsptoolIdentity:
    family: str  # esptool chip name: esp32, esp32c6, esp8266, ...
    mac: str  # 6-byte base MAC, normalized


def parse_esptool_identity(output: str) -> EsptoolIdentity:
    """Extract the chip family and base MAC from ``esptool chip-id`` output.

    Raises ``ValueError`` naming what is missing, so an operator reading a
    probe error knows whether esptool is too old for the part, the part is
    not an ESP at all, or the port simply did not answer.
    """
    chip_name = None
    for pattern in _CHIP_LINES:
        matches = re.findall(pattern, output, re.I | re.M)
        if matches:
            chip_name = matches[-1]
            break
    if chip_name is None:
        raise ValueError("no chip identification in esptool output")
    family_match = _FAMILY.match(chip_name)
    if family_match is None:
        raise ValueError(f"unrecognised chip name {chip_name!r} in esptool output")
    family = family_match.group(0).lower().replace("-", "")

    macs = {kind.upper(): normalize_mac(value) for kind, value in _MAC_LINE.findall(output)}
    mac = macs.get("BASE MAC") or macs.get("MAC")
    if mac is None:
        raise ValueError("no MAC address in esptool output")
    octets = mac.split(":")
    if len(octets) == 8:
        if octets[3:5] != ["ff", "fe"]:
            raise ValueError(f"cannot derive a base MAC from EUI-64 {mac}")
        mac = ":".join(octets[:3] + octets[5:])
    elif len(octets) != 6:
        raise ValueError(f"unexpected MAC length in esptool output: {mac}")
    return EsptoolIdentity(family=family, mac=mac)


def _transport(port: str, properties: dict[str, str]) -> str:
    """How the tty reaches the silicon: the chip's own USB or a UART bridge.

    Espressif's USB-Serial/JTAG peripheral enumerates with vendor 303a; a
    CP2102/CH340/FT232 bridge does not. The distinction matters for the
    C3/C5/C6/S3 artifacts, whose console is on the native USB port.
    """
    vendor = properties.get("ID_VENDOR_ID", "").lower()
    if vendor == "303a" or (not vendor and "ttyACM" in port):
        return "usb-serial-jtag"
    return "uart-bridge"


_SERIAL_FAULT_MARKERS = (
    "Serial data stream stopped",
    "serial noise or corruption",
    "Failed to connect",
    "Timed out waiting for packet",
)


def usb_device_of(port: str) -> Path | None:
    """The sysfs USB device a tty hangs off, e.g. ``/sys/bus/usb/devices/1-1.1.2``.

    Walks up from the tty's device until the directory that carries
    ``idVendor`` — the USB device itself rather than one of its interfaces.
    None for a port that is not USB, or not present.
    """
    try:
        node = (Path("/sys/class/tty") / Path(port).name / "device").resolve()
    except OSError:
        return None
    for candidate in [node, *node.parents]:
        if (candidate / "idVendor").is_file():
            return candidate
    return None


def usb_rebind(port: str) -> bool:
    """Unbind and re-bind the USB device behind ``port``; True if it was done.

    A CP2102 that has stopped mid-transfer stays broken until the USB
    device is re-enumerated: every later esptool call ends in "serial data
    stream stopped". A driver re-bind is what brought such a board back on
    the rig, from the shell, in five seconds. Direct writes need root; the
    service user has passwordless sudo, so the fallback is ``sudo -n tee``.
    """
    device = usb_device_of(port)
    if device is None:
        return False
    name = device.name
    for action in ("unbind", "bind"):
        target = Path("/sys/bus/usb/drivers/usb") / action
        try:
            target.write_text(name)
        except OSError:
            done = subprocess.run(
                ["sudo", "-n", "tee", str(target)],
                input=name, capture_output=True, text=True, check=False, timeout=10,
            )
            if done.returncode:
                return False
        time.sleep(2 if action == "unbind" else 3)
    return True


def _run_probe(port: str, python: str) -> tuple[int, str]:
    result = subprocess.run(
        [python, "-m", "esptool", "--port", port, "--connect-attempts", "3", "chip-id"],
        capture_output=True,
        text=True,
        check=False,
        timeout=45,
    )
    return result.returncode, result.stdout + "\n" + result.stderr


def probe_port(port: str, python: str = sys.executable) -> DetectedDevice:
    # Capture identity before esptool resets a native-USB device. The tty name
    # can disappear or be renumbered during the probe itself.
    properties = _udev_properties(port)
    returncode, output = _run_probe(port, python)
    if returncode and any(marker in output for marker in _SERIAL_FAULT_MARKERS):
        # The bridge, not the part: a board that answered every probe for
        # weeks and stops mid-transfer is a USB device that needs
        # re-enumerating, and reporting it missing loses it from every run
        # until someone reseats the cable. One re-bind, one more probe.
        if usb_rebind(port):
            returncode, output = _run_probe(port, python)
    if returncode:
        raise RuntimeError(f"esptool probe failed for {port}: {_tail(output)}")
    try:
        identity = parse_esptool_identity(output)
    except ValueError as exc:
        raise RuntimeError(f"cannot identify {port}: {exc} ({_tail(output)})") from exc
    if identity.family not in CHIP_TARGETS:
        raise RuntimeError(
            f"{identity.family} on {port} (MAC {identity.mac}) is not a supported farm target; "
            f"supported: {', '.join(sorted(CHIP_TARGETS))}"
        )
    return DetectedDevice(
        port=stable_serial_port(port, properties),
        chip=identity.family,
        target=CHIP_TARGETS[identity.family],
        mac=identity.mac,
        usb_path=properties.get("ID_PATH"),
        usb_serial=properties.get("ID_SERIAL"),
        transport=_transport(port, properties),
    )


def _tail(output: str, lines: int = 3) -> str:
    kept = [line.strip() for line in output.splitlines() if line.strip()]
    return " | ".join(kept[-lines:]) if kept else "no output"


# The facts esptool prints around the identity. Both generations are covered
# by one pattern each: v4 writes "Crystal is 40MHz", v5 writes
# "Crystal frequency:  40MHz". Every one of these is optional — an ESP8266
# reports no revision and no USB mode, and only `flash-id` prints the flash
# lines at all — so a missing field is silence, never an error.
_DETAIL_PATTERNS = {
    "revision": r"\(revision\s+([^)]+)\)",
    "features": r"^Features:\s*(.+?)\s*$",
    "crystal": r"^Crystal(?:\s+frequency)?\s*(?:is\s+|:\s*)(\S+)",
    "usb_mode": r"^USB mode:\s*(.+?)\s*$",
    "flash_size": r"^Detected flash size:\s*(\S+)",
    "flash_manufacturer": r"^Manufacturer:\s*(\S+)",
    "flash_device": r"^Device:\s*(\S+)",
    "flash_type": r"^Flash type(?:\s+set in eFuse)?:\s*(.+?)\s*$",
    "esptool_version": r"^esptool(?:\.py)?\s+v(\S+)",
}
# "Chip is ESP32-C6 (QFN40) (revision v0.1)" / "Chip type:  ESP32-C5 (QFN40)".
# The package suffix is part of the description an operator wants to read; the
# revision is pulled out separately by _DETAIL_PATTERNS.
_CHIP_DESCRIPTION = re.compile(r"^Chip (?:is|type:)\s*(.+?)\s*$", re.I | re.M)


@dataclass(frozen=True)
class DeviceDetails:
    """Everything esptool and udev can say about one attached part.

    The identity fields repeat ``DetectedDevice`` so a details record stands
    on its own. Everything after them is diagnostic and optional: what esptool
    prints varies by chip family and by esptool generation, and the dashboard
    renders only the fields that came back.
    """

    port: str
    chip: str
    target: str
    mac: str
    description: str | None = None
    revision: str | None = None
    features: list = field(default_factory=list)
    crystal: str | None = None
    usb_mode: str | None = None
    flash_size: str | None = None
    flash_manufacturer: str | None = None
    flash_device: str | None = None
    flash_type: str | None = None
    esptool_version: str | None = None
    usb_path: str | None = None
    usb_serial: str | None = None
    transport: str | None = None
    probed_at: str | None = None


def parse_esptool_details(output: str) -> dict:
    """Pull the optional diagnostic fields out of esptool output.

    Returns only the keys that were actually present, so a caller can splat
    it over ``DeviceDetails`` defaults without inventing values.
    """
    found: dict = {}
    description = _CHIP_DESCRIPTION.findall(output)
    if description:
        # Take the most specific line: v4 prints "Detecting chip type... ESP32"
        # before "Chip is ESP32-D0WD-V3 (revision v3.1)".
        found["description"] = re.sub(r"\s*\(revision[^)]*\)", "", description[-1]).strip()
    for name, pattern in _DETAIL_PATTERNS.items():
        match = re.search(pattern, output, re.I | re.M)
        if not match:
            continue
        value = match.group(1).strip()
        found[name] = [item.strip() for item in value.split(",") if item.strip()] if name == "features" else value
    return found


def probe_details(port: str, python: str = sys.executable) -> DeviceDetails:
    """Read the full chip and flash description of one attached board.

    ``flash-id`` prints the same identity banner as ``chip-id`` and then the
    flash manufacturer, device, and size, so one connect answers everything.
    The identity is re-parsed and re-validated here rather than trusted from
    the registry: this is the one call an operator makes to ask the silicon
    what it actually is.
    """
    properties = _udev_properties(port)
    result = subprocess.run(
        [python, "-m", "esptool", "--port", port, "--connect-attempts", "3", "flash-id"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    output = result.stdout + "\n" + result.stderr
    if result.returncode:
        raise RuntimeError(f"esptool flash-id failed for {port}: {_tail(output)}")
    try:
        identity = parse_esptool_identity(output)
    except ValueError as exc:
        raise RuntimeError(f"cannot identify {port}: {exc} ({_tail(output)})") from exc
    if identity.family not in CHIP_TARGETS:
        raise RuntimeError(
            f"{identity.family} on {port} (MAC {identity.mac}) is not a supported farm target; "
            f"supported: {', '.join(sorted(CHIP_TARGETS))}"
        )
    return DeviceDetails(
        port=stable_serial_port(port, properties),
        chip=identity.family,
        target=CHIP_TARGETS[identity.family],
        mac=identity.mac,
        usb_path=properties.get("ID_PATH"),
        usb_serial=properties.get("ID_SERIAL"),
        transport=_transport(port, properties),
        probed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **parse_esptool_details(output),
    )


def discover(
    ports: Iterable[str] | None = None,
    probe: Callable[[str], DetectedDevice] = probe_port,
) -> tuple[list[DetectedDevice], list[dict]]:
    devices, errors = [], []
    for port in ports if ports is not None else serial_ports():
        try:
            devices.append(probe(port))
        except Exception as exc:
            errors.append({"port": port, "error": str(exc)})
    return devices, errors


def publish_inventory(
    registry_path: str | Path,
    board_map_path: str | Path,
    state_dir: str | Path,
    probe: Callable[[str], DetectedDevice] | None = None,
    auto_register: bool = False,
) -> dict:
    """Probe the USB tree, reconcile it with the registry, and publish the
    result everywhere the rig reads it: the active board map (suites, health)
    and the inventory snapshot (dashboard, API). The farm service and the
    admin CLI share this one path so a board registered from either side is
    visible from both immediately."""
    registrations = load_registry(registry_path)
    instruments = load_instruments(instruments_path_for(registry_path), registrations)
    devices, errors = discover(probe=probe or probe_port)
    added: list[str] = []
    if auto_register:
        # An instrument is an ESP too, and is registered as one deliberately:
        # never as a board, whatever it looks like on the port.
        registrations, added = register_detected(
            registry_path, registrations, devices, claimed=[item.mac for item in instruments]
        )
    result = reconcile(registrations, devices, instruments)
    write_active_map(board_map_path, result)
    snapshot = write_inventory_snapshot(state_dir, result, errors)
    # What this discovery registered by itself, for the console and the log:
    # a board appearing in the farm is worth a line saying where it came from.
    snapshot["registered"] = added
    return snapshot

