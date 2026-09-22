"""The board registry and the inventory a rig publishes, as descriptions.

What a rig's boards are registered as, what a discovery found, how the two
are reconciled, and the files that carries to everything that reads it: the
registry, the active board map, the inventory snapshot. None of it opens a
port -- a discovery is handed in, as `DetectedDevice`s -- so a portal reads a
registry and a snapshot with this as a rig does.

Finding what is on the ports is `alteriom_hil.inventory`: udev, esptool, and
`publish_inventory`, which probes and then calls what is here.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import yaml

from .board import Board
from .instrument_registry import Instrument, instruments_path_for, load_instruments


@dataclass(frozen=True)
class DetectedDevice:
    port: str
    chip: str
    target: str
    mac: str
    usb_path: str | None = None
    usb_serial: str | None = None
    transport: str | None = None  # usb-serial-jtag | uart-bridge


@dataclass(frozen=True)
class InventoryResult:
    boards: list[Board]
    missing: list[str]
    unregistered: list[DetectedDevice]
    instruments: list[Instrument] = field(default_factory=list)
    missing_instruments: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "boards": [asdict(board) for board in self.boards],
            "missing": self.missing,
            "unregistered": [asdict(device) for device in self.unregistered],
            "instruments": [_instrument_entry(item) for item in self.instruments],
            "missing_instruments": self.missing_instruments,
        }


def _instrument_entry(item: Instrument) -> dict:
    return {**item.as_dict(), "target": item.target, "chip": item.chip}


def normalize_mac(value: str) -> str:
    return value.strip().lower().replace("-", ":")


def reconcile(
    registrations: Iterable[Board],
    devices: Iterable[DetectedDevice],
    instruments: Iterable[Instrument] = (),
) -> InventoryResult:
    registered = list(registrations)
    detected = list(devices)
    by_mac = {device.mac: device for device in detected}
    claimed: set[str] = set()
    active, missing = [], []
    # Instruments first: an instrument is an ESP like any board, and nothing
    # else may claim its MAC -- not a board registered by port, and not the
    # unregistered list that invites registering it as one.
    active_instruments, missing_instruments = [], []
    for item in instruments:
        device = by_mac.get(item.mac)
        if device is None:
            missing_instruments.append(item.id)
            continue
        if device.target != item.target:
            raise ValueError(
                f"registered instrument {item.id} is a {item.kind} ({item.target}), "
                f"but MAC {device.mac} identifies as {device.target}"
            )
        claimed.add(device.mac)
        active_instruments.append(replace(item, port=device.port, usb_path=device.usb_path))
    for board in registered:
        device = by_mac.get(normalize_mac(board.mac)) if board.mac else None
        if device is None and not board.mac:
            device = next(
                (item for item in detected if item.port == board.port and item.mac not in claimed),
                None,
            )
        if device is None:
            missing.append(board.id)
            continue
        if device.target != board.target:
            raise ValueError(
                f"registered board {board.id} expects {board.target}, "
                f"but MAC {device.mac} identifies as {device.target}"
            )
        claimed.add(device.mac)
        active.append(
            replace(
                board,
                port=device.port,
                chip=device.chip,
                mac=device.mac,
                usb_path=device.usb_path,
            )
        )
    return InventoryResult(
        boards=active,
        missing=missing,
        unregistered=[device for device in detected if device.mac not in claimed],
        instruments=active_instruments,
        missing_instruments=missing_instruments,
    )


def suggested_id(device: DetectedDevice) -> str:
    """The name a board gets when it names itself: its family and the last two
    bytes of its MAC -- `esp32-c6-14b4`. The convention the dashboard has
    suggested to operators all along, so a rig that registers its own boards
    produces the names people were already choosing."""
    return f"{device.target}-{normalize_mac(device.mac).replace(':', '')[-4:]}"


def register_detected(
    registry_path: str | Path, registered: list[Board], devices: Iterable[DetectedDevice],
    claimed: Iterable[str] = (),
) -> tuple[list[Board], list[str]]:
    """Add every device nobody has registered, and say which were added.

    A board plugged into a rig is a board the rig should have: making an
    operator name it by hand was a step that taught nobody anything, and on a
    new rig it was the step between "the hub is connected" and a farm that
    still says it has no boards. Ids and MACs already registered are left
    alone, and an id that somehow collides gets the port appended rather than
    replacing a board that exists.
    """
    taken_ids = {board.id for board in registered}
    taken_macs = {normalize_mac(board.mac) for board in registered if board.mac}
    spoken_for = {normalize_mac(mac) for mac in claimed}
    added: list[str] = []
    for device in devices:
        mac = normalize_mac(device.mac)
        if mac in taken_macs or mac in spoken_for:
            continue
        board_id = suggested_id(device)
        if board_id in taken_ids:
            board_id = f"{board_id}-{Path(device.port).name}"[:32]
        if board_id in taken_ids:
            continue
        registered.append(Board(id=board_id, port=device.port, chip=device.chip,
                                target=device.target, mac=mac))
        taken_ids.add(board_id)
        taken_macs.add(mac)
        added.append(board_id)
    if added:
        write_registry(registry_path, registered)
    return registered, added


def load_registry(path: str | Path) -> list[Board]:
    """The rig's registered boards; none on a rig where nobody has registered
    one yet.

    A registry that is absent or empty is the state every rig starts in, and
    the two things you do there -- discover what is on the ports, and register
    the first board -- both read it first. Refusing to read it made those the
    two commands a new rig could not run: on rig02 (2026-09-16, a fresh
    install) `boards discover` answered "No such file or directory" about a
    file it exists to help write. Duplicate ids and MACs are still refused,
    since those are a registry that is wrong rather than one not written yet.
    """
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return []
    return validate_registry([Board(**entry) for entry in raw.get("boards") or []])


def validate_registry(boards: Iterable[Board]) -> list[Board]:
    """Validate stable identities while allowing stale transport hints to overlap."""
    registered = list(boards)
    ids = [board.id for board in registered]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate board ids in inventory registry: {ids}")
    macs = [normalize_mac(board.mac) for board in registered if board.mac]
    if len(macs) != len(set(macs)):
        raise ValueError(f"duplicate MAC addresses in inventory registry: {macs}")
    return registered


def _replace_atomic(target: str | Path, text: str) -> None:
    """Write via a temporary file and rename, then hand the file to the
    directory's owner when running as root: the admin CLI runs under sudo but
    the farm service (the runner user) must be able to overwrite the same
    file on its next discovery."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(target)
    if os.geteuid() == 0:
        owner = target.parent.stat()
        if owner.st_uid != 0:
            try:
                os.chown(target, owner.st_uid, owner.st_gid)
            except OSError:
                pass


def write_registry(path: str | Path, boards: Iterable[Board]) -> None:
    registered = validate_registry(boards)
    payload = {
        "boards": [
            {
                key: value
                for key, value in asdict(board).items()
                if value is not None and key != "usb_path"
            }
            for board in registered
        ]
    }
    _replace_atomic(path, yaml.safe_dump(payload, sort_keys=False))


def write_active_map(path: str | Path, result: InventoryResult) -> None:
    payload = {
        "boards": [
            {key: value for key, value in asdict(board).items() if value is not None}
            for board in result.boards
        ]
    }
    # Beside the boards, not among them: BoardMap reads `boards` only, so a
    # suite that knows nothing of instruments never sees one, and one that
    # does finds each connected instrument with its port and its wiring.
    if result.instruments:
        payload["instruments"] = [_instrument_entry(item) for item in result.instruments]
    _replace_atomic(path, yaml.safe_dump(payload, sort_keys=False))


def write_inventory_snapshot(state_dir: str | Path, result: InventoryResult, errors: list[dict]) -> dict:
    """Persist what the dashboard and API report as the fleet."""
    snapshot = {
        **result.as_dict(),
        "probe_errors": errors,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _replace_atomic(Path(state_dir) / "inventory.json", json.dumps(snapshot, indent=2) + "\n")
    return snapshot


def load_inventory_snapshot(state_dir: str | Path, registry_path: str | Path | None = None) -> dict:
    """The last published snapshot, reconciled with the registry as it is now.

    A board registered after the last discovery is reported as missing (known,
    not yet seen) rather than absent, and a device whose MAC has since been
    registered is no longer listed as unregistered; ``registered`` counts the
    registry. Nothing here touches hardware."""
    try:
        snapshot = json.loads((Path(state_dir) / "inventory.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        snapshot = {"boards": [], "missing": [], "unregistered": [], "probe_errors": []}
    snapshot.setdefault("boards", [])
    snapshot.setdefault("missing", [])
    snapshot.setdefault("unregistered", [])
    snapshot.setdefault("probe_errors", [])
    snapshot.setdefault("instruments", [])
    snapshot.setdefault("missing_instruments", [])
    if registry_path is None:
        return snapshot
    try:
        registered = load_registry(registry_path)
    except (OSError, ValueError, TypeError):
        return snapshot
    try:
        instruments = load_instruments(instruments_path_for(registry_path), registered)
    except (OSError, ValueError, TypeError):
        instruments = []
    known_ids = {board["id"] for board in snapshot["boards"]} | set(snapshot["missing"])
    macs = {normalize_mac(board.mac) for board in registered if board.mac}
    macs |= {item.mac for item in instruments}
    snapshot["missing"] = snapshot["missing"] + [board.id for board in registered if board.id not in known_ids]
    known_instruments = {item["id"] for item in snapshot["instruments"]} | set(snapshot["missing_instruments"])
    snapshot["missing_instruments"] = snapshot["missing_instruments"] + [
        item.id for item in instruments if item.id not in known_instruments
    ]
    snapshot["unregistered"] = [
        device for device in snapshot["unregistered"] if normalize_mac(device.get("mac", "")) not in macs
    ]
    snapshot["registered"] = len(registered)
    snapshot["registered_instruments"] = len(instruments)
    return snapshot


def snapshot_json(result: InventoryResult, errors: list[dict]) -> str:
    return json.dumps({**result.as_dict(), "probe_errors": errors}, sort_keys=True)
