"""Drivers, as plugins: the farm calls an interface, a family names who answers.

What the farm does to a board -- flash it, find out what it is, talk to it,
cut its power -- was a direct call to esptool, to a MAC probe, to a serial
port, to uhubctl. Those are now **kinds** of driver, each family's descriptor
(alteriom_hil.devices) names which driver of each kind it uses, and the farm
asks the registry for it:

    flasher_for("esp32-c6")(image, board, offset="0x0")

The built-in drivers are the ones the farm has always used, so every family
the farm runs today resolves to exactly the call it made before. A new chip
-- an RP2040, an nRF52 -- brings its own: a package that declares an entry
point in the ``alteriom_hil.plugins`` group, whose object is a mapping of
kind to ``{name: implementation}`` (an implementation is an object, or a
``"module:attribute"`` string loaded when first asked for), and a family
descriptor that names those drivers. The farm's own code does not change.

The interfaces, as the built-ins implement them:

- ``flasher(image, board, offset) -> CompletedProcess``, raising on failure;
- ``identity(port) -> DetectedDevice``, raising when the part cannot be named;
- ``console(opener) -> SerialCapture``-like, with ``start``/``stop``/``write_line``/``next_event``;
- ``power(boards) -> controller`` with ``supports``/``on``/``off``/``cycle``.

Discovery and power are not routed through here yet: discovery must name a
part before it knows its family, and the registry is where a second identity
probe would go when one exists.
"""

from __future__ import annotations

import importlib
from importlib import metadata

from .devices import DRIVER_KINDS, load_families

ENTRY_POINT_GROUP = "alteriom_hil.plugins"

BUILTIN = {
    "flasher": {"esptool": "alteriom_hil.flash:flash_esptool"},
    "identity": {"esptool-mac": "alteriom_hil.inventory:probe_port"},
    "console": {"usb-serial": "alteriom_hil.serial_capture:SerialCapture"},
    "power": {"uhubctl": "alteriom_hil.power:power_for"},
}


class PluginError(LookupError):
    pass


def _entry_points() -> list:
    found = metadata.entry_points()
    if hasattr(found, "select"):  # Python 3.10+
        return list(found.select(group=ENTRY_POINT_GROUP))
    return list(found.get(ENTRY_POINT_GROUP, []))  # Python 3.9


def registry() -> dict[str, dict]:
    """Every driver the farm can use, by kind and name: its own, then each
    installed plugin's. A plugin may not replace a built-in -- a package that
    quietly swapped the esptool flasher would flash every board through it."""
    drivers = {kind: dict(names) for kind, names in BUILTIN.items()}
    for entry in _entry_points():
        provided = entry.load()
        if callable(provided) and not isinstance(provided, dict):
            provided = provided()
        if not isinstance(provided, dict):
            raise PluginError(f"plugin {entry.name}: must provide a mapping of driver kind to drivers")
        for kind, names in provided.items():
            if kind not in DRIVER_KINDS or not isinstance(names, dict):
                raise PluginError(f"plugin {entry.name}: {kind!r} is not a driver kind ({', '.join(DRIVER_KINDS)})")
            for name, implementation in names.items():
                if name in drivers[kind]:
                    raise PluginError(f"plugin {entry.name}: the {kind} {name!r} is already provided")
                drivers[kind][name] = implementation
    return drivers


def resolve(kind: str, name: str):
    if kind not in DRIVER_KINDS:
        raise PluginError(f"{kind!r} is not a driver kind ({', '.join(DRIVER_KINDS)})")
    implementation = registry()[kind].get(name)
    if implementation is None:
        raise PluginError(f"no {kind} named {name!r} is installed")
    if isinstance(implementation, str):
        module, _, attribute = implementation.partition(":")
        return getattr(importlib.import_module(module), attribute)
    return implementation


def driver_for(target: str, kind: str, families: dict | None = None):
    family = (families if families is not None else load_families()).get(target)
    if family is None:
        raise PluginError(f"no family named {target!r}")
    return resolve(kind, family.drivers[kind])


def flasher_for(target: str, families: dict | None = None):
    return driver_for(target, "flasher", families)
