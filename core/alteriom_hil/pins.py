"""Which pins of a board under test an instrument may be wired to.

A jumper from an instrument to a board is only safe on an ordinary GPIO. The
rest are load-bearing, and wiring one breaks the board in a way that looks
like anything but a wire:

- **the flash bus** -- the board stops booting;
- **the console UART or native USB** -- the farm loses the board's serial port,
  which is how it flashes and talks to it;
- **a strapping pin** -- the chip reads it at reset, so an instrument that
  happens to be driving it then boots the board into download mode or the
  wrong flash voltage.

So the farm refuses such wiring where it is written rather than finding out on
the rig. The tables are deliberately conservative: a pin missing from one can
be added once it is shown safe on a real board; one wrongly present costs a
board that will not boot. A family with no table cannot be wired yet.

The tables are read from the family descriptors
(alteriom_hil/devices/families), which say why each other pin is reserved.
"""

from __future__ import annotations

from .devices import load_families

# Per family: the GPIOs a jumper may land on, on the dev boards this rig uses.
WIREABLE_PINS: dict[str, frozenset[int]] = {
    name: family.wireable for name, family in load_families().items() if family.wireable is not None
}

# Pins that can read but never drive. Wiring one to an instrument channel that
# cannot drive either leaves a wire nothing can put a level on.
INPUT_ONLY_PINS: dict[str, frozenset[int]] = {
    name: family.input_only for name, family in load_families().items() if family.input_only
}


def check_wireable(target: str, pin: int) -> None:
    """Raise ValueError naming why `pin` on a `target` board cannot be wired."""
    table = WIREABLE_PINS.get(target)
    if table is None:
        raise ValueError(
            f"no pin table for {target} yet, so none of its pins can be wired; "
            f"add one to alteriom_hil.pins once its safe GPIOs are confirmed"
        )
    if pin not in table:
        raise ValueError(
            f"GPIO{pin} on {target} is not a wireable pin: it is the flash bus, "
            f"the console, native USB, a strapping pin, or not on the header "
            f"(wireable: {', '.join(str(p) for p in sorted(table))})"
        )


def can_drive(target: str, pin: int) -> bool:
    return pin not in INPUT_ONLY_PINS.get(target, frozenset())
