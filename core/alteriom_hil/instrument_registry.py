"""I/O instruments: test equipment the farm owns, wired to boards under test.

A board under test runs somebody's firmware. An instrument runs the farm's:
its pins are the test equipment -- they press the button, read the LED,
measure the pulse -- so a suite can check what firmware does with its I/O and
not only what it prints. The first instrument is an ESP32 on jumper wires
(`instruments/esp32-io/`, docs/io-instrument.md).

An instrument has its own identity. It is an ESP32, so discovery sees an ESP32
-- and without a registration of its own it would be offered as a board,
flashed with a suite's firmware and meshed with the rest. It is registered in
`instruments.yaml` beside the board registry, by its eFuse MAC like a board, and
the inventory claims it as an instrument before anything can mistake it for a
board.

Its wiring is registered with it: which instrument channel goes to which pin
of which board. That is the only place the farm learns what a jumper connects,
and it is checked where it is written (`load_instruments`): a channel the
instrument does not have, a board that is not registered, a pin that is the
flash bus, the console or a strapping pin, a pin wired twice, or a wire neither
end can drive.

This module is the description: what an instrument is, how one is registered
and what its wiring may be. It opens no port, so a portal reads the registry
with it as a rig does. Talking to one is `alteriom_hil.instrument`.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

import yaml

from .board import TARGET_CHIPS, Board
from .pins import can_drive, check_wireable

ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}\Z")
MAC_PATTERN = re.compile(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}\Z")
PROTOCOL_VERSION = 1


@dataclass(frozen=True)
class Channel:
    """One instrument pin and what it can do."""

    pin: int
    drive: bool = True  # can be an output
    pull: bool = True  # has internal pull-up/down
    adc: bool = False  # can measure a voltage


@dataclass(frozen=True)
class InstrumentKind:
    """A kind of instrument: the chip it is built on and its channels.

    The channel table is the firmware's (`instruments/<kind>/firmware`), and a
    test holds the two to agreement.
    """

    name: str
    target: str  # the chip family discovery reports it as
    channels: dict[int, Channel]


def _esp32_io_channels() -> dict[int, Channel]:
    # An esp32dev (WROOM-32) as test equipment. Left out, as on a board under
    # test: strapping 0 2 5 12 15, console 1 3, flash 6-11. 34-39 are ADC1,
    # input-only, and have no pulls; 32 and 33 are ADC1 too. ADC2 (the rest)
    # would work with the radio off, which this firmware never turns on, but
    # nothing needs it yet.
    channels = {pin: Channel(pin) for pin in (4, 13, 14, 16, 17, 18, 19, 21, 22, 23, 25, 26, 27)}
    channels.update({pin: Channel(pin, adc=True) for pin in (32, 33)})
    channels.update({pin: Channel(pin, drive=False, pull=False, adc=True) for pin in (34, 35, 36, 39)})
    return channels


KINDS: dict[str, InstrumentKind] = {
    "esp32-io": InstrumentKind("esp32-io", "esp32", _esp32_io_channels()),
}


@dataclass
class Wire:
    """One jumper: an instrument channel to a pin of a registered board."""

    channel: int
    board: str
    pin: int
    note: str | None = None


@dataclass
class Instrument:
    id: str
    kind: str
    mac: str
    port: str = ""  # last known serial device; the MAC is the identity
    usb_path: str | None = None
    wiring: list[Wire] = field(default_factory=list)

    def __post_init__(self):
        if not isinstance(self.id, str) or not ID_PATTERN.fullmatch(self.id):
            raise ValueError(f"instrument id {self.id!r} must be 1-32 lowercase letters, digits, dots, underscores or hyphens")
        if self.kind not in KINDS:
            raise ValueError(f"instrument {self.id}: unknown kind {self.kind!r}; known: {', '.join(sorted(KINDS))}")
        mac = str(self.mac or "").strip().lower().replace("-", ":")
        if not MAC_PATTERN.fullmatch(mac):
            raise ValueError(f"instrument {self.id}: invalid MAC address {self.mac!r}")
        self.mac = mac
        self.wiring = [wire if isinstance(wire, Wire) else Wire(**wire) for wire in self.wiring or []]

    @property
    def target(self) -> str:
        return KINDS[self.kind].target

    @property
    def chip(self) -> str:
        return TARGET_CHIPS[self.target]

    def as_dict(self) -> dict:
        found = asdict(self)
        found["wiring"] = [{k: v for k, v in wire.items() if v is not None} for wire in found["wiring"]]
        return {key: value for key, value in found.items() if value not in (None, "")}


def instruments_path_for(registry: str | Path) -> Path:
    """Where the instruments are registered: beside the board registry.

    A file of its own rather than a section of the board registry, because
    every writer of that file -- the admin CLI, the service's register and
    unregister routes -- rewrites it as a list of boards.
    """
    return Path(registry).with_name("instruments.yaml")


def validate_instruments(instruments: Iterable[Instrument], boards: Iterable[Board] | None = None) -> list[Instrument]:
    """Check identities and wiring. `boards` is the board registry; without it
    only what the instruments say about themselves is checked."""
    found = list(instruments)
    ids = [item.id for item in found]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate instrument ids: {sorted(ids)}")
    macs = [item.mac for item in found]
    if len(macs) != len(set(macs)):
        raise ValueError(f"duplicate instrument MAC addresses: {sorted(macs)}")
    registered = {board.id: board for board in boards} if boards is not None else None
    if registered is not None:
        board_macs = {str(board.mac).lower() for board in registered.values() if board.mac}
        for item in found:
            if item.mac in board_macs:
                raise ValueError(f"instrument {item.id}: MAC {item.mac} is registered as a board too")
            if item.id in registered:
                raise ValueError(f"instrument {item.id}: that id is a registered board's")
    landed: dict[tuple[str, int], str] = {}
    for item in found:
        kind = KINDS[item.kind]
        channels: set[int] = set()
        for wire in item.wiring:
            channel = kind.channels.get(wire.channel)
            if channel is None:
                raise ValueError(
                    f"instrument {item.id}: {item.kind} has no channel {wire.channel} "
                    f"(channels: {', '.join(str(c) for c in sorted(kind.channels))})"
                )
            if wire.channel in channels:
                raise ValueError(f"instrument {item.id}: channel {wire.channel} is wired twice")
            channels.add(wire.channel)
            if registered is None:
                continue
            board = registered.get(wire.board)
            if board is None:
                raise ValueError(f"instrument {item.id}: channel {wire.channel} is wired to {wire.board}, which is not a registered board")
            try:
                check_wireable(board.target, wire.pin)
            except ValueError as exc:
                raise ValueError(f"instrument {item.id}: channel {wire.channel}: {exc}") from None
            if not channel.drive and not can_drive(board.target, wire.pin):
                raise ValueError(
                    f"instrument {item.id}: channel {wire.channel} and {wire.board} GPIO{wire.pin} "
                    f"are both input-only, so nothing can put a level on that wire"
                )
            end = (wire.board, wire.pin)
            if end in landed:
                raise ValueError(f"{wire.board} GPIO{wire.pin} is wired to both {landed[end]} and {item.id} channel {wire.channel}")
            landed[end] = f"{item.id} channel {wire.channel}"
    return found


def load_instruments(path: str | Path, boards: Iterable[Board] | None = None) -> list[Instrument]:
    """The registered instruments; none when the file does not exist."""
    path = Path(path)
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = raw.get("instruments") or []
    try:
        return validate_instruments([Instrument(**entry) for entry in entries], boards)
    except TypeError as exc:
        raise ValueError(f"instrument registry {path}: {exc}") from exc


def instruments_document(instruments: Iterable[Instrument]) -> str:
    entries = []
    for item in instruments:
        entry = item.as_dict()
        entry.pop("usb_path", None)  # a transport detail, like a board's
        entries.append(entry)
    return yaml.safe_dump({"instruments": entries}, sort_keys=False)


def wired_to(instruments: Iterable[Instrument], board_id: str) -> list[str]:
    """Which instrument channels are wired to a board, as "<id> channel <n>"."""
    return [
        f"{item.id} channel {wire.channel}"
        for item in instruments
        for wire in item.wiring
        if wire.board == board_id
    ]
