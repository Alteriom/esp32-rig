"""Device descriptors: what the farm knows about hardware, as data.

Two kinds of document.

A **family** (``alteriom_hil/devices/families/<name>.yaml``) is a chip the
farm can run: what esptool calls it, how its board's console reaches the host,
which drivers the farm uses for it (``alteriom_hil.plugins``), and its pins --
every GPIO, the ones a jumper may land on, the input-only ones, and the
reserved ones with the reason each is reserved. Everything the HAL used to
hard-code about a family is read from here: ``board.TARGET_CHIPS``,
``pins.WIREABLE_PINS``, which families have native USB. Adding a family is
adding a document (and its drivers, when esptool is not one).

A **device** (``devices/<model>.yaml`` in a repository) is a board built on a
family, described by its **signals**: ``BUTTON`` on GPIO18, active low, pulled
up; ``LED`` on GPIO19; ``VBAT`` analog on GPIO3. It is what lets a suite say
``BUTTON`` instead of a channel number, and what lets the farm write the
standard check for a board it has never seen. A signal may sit on any GPIO of
its family -- a devkit's BOOT button is a strapping pin -- but only one on a
wireable pin can be reached by an instrument, and ``wireable_signals`` says
which.

Checked where they are written, like profiles: a descriptor that names a pin
its family does not have, or two signals on one pin, stops the loader, naming
the file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

FAMILY_DIR = Path(__file__).resolve().parent / "families"
SCHEMA = 1
CONSOLES = ("usb-serial-jtag", "uart-bridge")
DRIVER_KINDS = ("flasher", "identity", "console", "power")
DIRECTIONS = ("in", "out", "analog", "bidir")
NAME = re.compile(r"[a-z0-9][a-z0-9.-]{0,63}\Z")
SIGNAL = re.compile(r"[A-Z][A-Z0-9_]{0,31}\Z")


class DescriptorError(ValueError):
    pass


@dataclass(frozen=True)
class Family:
    name: str
    label: str
    chip: str
    console: str
    drivers: dict
    gpios: frozenset
    # None: no pin has been confirmed safe to wire on this family, so none is.
    wireable: frozenset | None
    input_only: frozenset
    reserved: dict = field(default_factory=dict)

    @property
    def native_usb(self) -> bool:
        return self.console == "usb-serial-jtag"


def _pins(value, source: str, what: str) -> frozenset:
    if not isinstance(value, list) or not all(isinstance(pin, int) and not isinstance(pin, bool) and 0 <= pin < 64 for pin in value):
        raise DescriptorError(f"{source}: {what} must be a list of GPIO numbers")
    if len(set(value)) != len(value):
        raise DescriptorError(f"{source}: {what} lists a GPIO twice")
    return frozenset(value)


def parse_family(document: object, source: str) -> Family:
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        raise DescriptorError(f"{source}: not a schema {SCHEMA} family descriptor")
    name = document.get("family")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise DescriptorError(f"{source}: family must be a lowercase name")
    chip = document.get("chip")
    if not isinstance(chip, str) or not chip:
        raise DescriptorError(f"{source}: chip must name what esptool calls the part")
    console = document.get("console")
    if console not in CONSOLES:
        raise DescriptorError(f"{source}: console must be one of {', '.join(CONSOLES)}")
    drivers = document.get("drivers")
    if not isinstance(drivers, dict) or set(drivers) != set(DRIVER_KINDS) or not all(isinstance(v, str) and v for v in drivers.values()):
        raise DescriptorError(f"{source}: drivers must name a {', '.join(DRIVER_KINDS)}")
    pins = document.get("pins")
    if not isinstance(pins, dict):
        raise DescriptorError(f"{source}: pins must be a mapping")
    gpios = _pins(pins.get("gpios"), source, "pins.gpios")
    wireable = None if pins.get("wireable") is None else _pins(pins.get("wireable"), source, "pins.wireable")
    input_only = _pins(pins.get("input_only") or [], source, "pins.input_only")
    reserved_raw = pins.get("reserved") or {}
    if not isinstance(reserved_raw, dict):
        raise DescriptorError(f"{source}: pins.reserved must map a reason to its GPIOs")
    reserved = {reason: _pins(value, source, f"pins.reserved.{reason}") for reason, value in reserved_raw.items()}
    for what, group in [("pins.wireable", wireable or frozenset()), ("pins.input_only", input_only),
                        *((f"pins.reserved.{reason}", group) for reason, group in reserved.items())]:
        stray = sorted(group - gpios)
        if stray:
            raise DescriptorError(f"{source}: {what} names GPIOs the family does not have: {stray}")
    for reason, group in reserved.items():
        clash = sorted(group & (wireable or frozenset()))
        if clash:
            raise DescriptorError(f"{source}: GPIO {clash} is both wireable and reserved for {reason}")
    return Family(
        name=name, label=str(document.get("label") or name), chip=chip, console=console,
        drivers=dict(drivers), gpios=gpios, wireable=wireable, input_only=input_only, reserved=reserved,
    )


def load_families(directory: str | Path = FAMILY_DIR) -> dict[str, Family]:
    return dict(_load_families(str(Path(directory).resolve())))


@lru_cache(maxsize=None)
def _load_families(directory: str) -> tuple:
    found = []
    for path in sorted(Path(directory).glob("*.yaml")):
        family = parse_family(yaml.safe_load(path.read_text(encoding="utf-8")), path.name)
        if family.name != path.stem:
            raise DescriptorError(f"{path.name}: family {family.name!r} does not match its filename")
        found.append((family.name, family))
    return tuple(found)


# ---- devices ----------------------------------------------------------------------


@dataclass(frozen=True)
class Signal:
    name: str
    pin: int
    direction: str
    pull: str | None = None
    active: str = "high"
    note: str | None = None


@dataclass(frozen=True)
class Device:
    model: str
    label: str
    family: str
    signals: dict

    def signal(self, name: str) -> Signal:
        try:
            return self.signals[name]
        except KeyError:
            raise KeyError(f"{self.model} has no signal {name!r}; it has {', '.join(sorted(self.signals))}") from None


def parse_device(document: object, source: str, families: dict[str, Family] | None = None) -> Device:
    families = families if families is not None else load_families()
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        raise DescriptorError(f"{source}: not a schema {SCHEMA} device descriptor")
    model = document.get("model")
    if not isinstance(model, str) or not NAME.fullmatch(model):
        raise DescriptorError(f"{source}: model must be a lowercase name")
    family = families.get(document.get("family"))
    if family is None:
        raise DescriptorError(f"{source}: family must be one of {', '.join(sorted(families))}")
    raw = document.get("signals") or {}
    if not isinstance(raw, dict) or not raw:
        raise DescriptorError(f"{source}: signals must name at least one signal")
    signals, used = {}, {}
    for name, spec in raw.items():
        if not isinstance(name, str) or not SIGNAL.fullmatch(name):
            raise DescriptorError(f"{source}: signal {name!r} must be an UPPER_CASE name")
        if not isinstance(spec, dict):
            raise DescriptorError(f"{source}: signal {name} must be a mapping")
        pin = spec.get("pin")
        if not isinstance(pin, int) or isinstance(pin, bool) or pin not in family.gpios:
            raise DescriptorError(f"{source}: {name} is on GPIO{pin}, which a {family.name} does not have")
        if pin in used:
            raise DescriptorError(f"{source}: {name} and {used[pin]} are both on GPIO{pin}")
        direction = spec.get("direction")
        if direction not in DIRECTIONS:
            raise DescriptorError(f"{source}: {name}.direction must be one of {', '.join(DIRECTIONS)}")
        if direction in ("out", "bidir") and pin in family.input_only:
            raise DescriptorError(f"{source}: {name} drives GPIO{pin}, which is input-only on a {family.name}")
        pull = spec.get("pull")
        if pull not in (None, "up", "down"):
            raise DescriptorError(f"{source}: {name}.pull must be up or down")
        active = spec.get("active", "high")
        if active not in ("high", "low"):
            raise DescriptorError(f"{source}: {name}.active must be high or low")
        used[pin] = name
        signals[name] = Signal(name, pin, direction, pull, active, spec.get("note"))
    return Device(model=model, label=str(document.get("label") or model), family=family.name, signals=signals)


def load_devices(root: str | Path, families: dict[str, Family] | None = None) -> dict[str, Device]:
    """Every device descriptor in `<root>/devices/`, by model."""
    found = {}
    for path in sorted((Path(root) / "devices").glob("*.yaml")):
        device = parse_device(yaml.safe_load(path.read_text(encoding="utf-8")), path.name, families)
        if device.model != path.stem:
            raise DescriptorError(f"{path.name}: model {device.model!r} does not match its filename")
        found[device.model] = device
    return found


def wireable_signals(device: Device, families: dict[str, Family] | None = None) -> list[Signal]:
    """The signals an instrument can be wired to: those on wireable pins."""
    family = (families if families is not None else load_families())[device.family]
    return [signal for signal in device.signals.values() if family.wireable and signal.pin in family.wireable]
