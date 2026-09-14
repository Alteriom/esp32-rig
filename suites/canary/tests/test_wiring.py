"""Does every wire between an instrument and a board carry a level.

The standard check for a board connected to test equipment: before any suite
presses a button through a jumper or reads an LED through one, each jumper is
driven from one end and read at the other, both levels, in each direction
either end can drive. A loose wire, a wire on the wrong pin, a missing ground,
a board pin the firmware cannot reach -- each shows up here as one named wire
failing, instead of as a product test that "did not see the button".

It lives in the canary because the canary is what the farm flashes to ask
whether a board is healthy, and it can drive and read the board's end of a
wire (its gpio commands) knowing nothing of any product. A board no
instrument is wired to has nothing to check, and says so.
"""

from __future__ import annotations

import time

import pytest

from alteriom_hil.pins import can_drive

pytestmark = [
    # A wire that fails is a jumper, a pin or a board -- never a library.
    pytest.mark.failure_class("flaky_hardware"),
]

# How long a level is given to settle before it is read. Each read is a serial
# round trip, which already takes longer than a jumper needs; this is margin
# for the series resistor and the input's capacitance, not a timing claim.
SETTLE_SECONDS = 0.02


@pytest.mark.hil_only(reason="peripheral")
@pytest.mark.capability("io.wiring")
def test_every_wire_carries_a_level_both_ways(board, canary, instruments, wiring):
    wires = [(instrument_id, wire) for instrument_id, wire in wiring if wire.board == canary]
    if not wires:
        pytest.skip(f"no instrument is wired to {canary}")
    info = board.ensure_responsive()
    family = info.get("family")
    # A canary whose build fell through to the wrong pin table would refuse
    # every wire; say that, rather than a wire at a time.
    assert info.get("pinTable") == family, (
        f"{canary} is a {family} running the {info.get('pinTable')!r} pin table"
    )
    failures = []
    for instrument_id, wire in wires:
        instrument = instruments.get(instrument_id)
        if instrument is None:
            failures.append(f"{instrument_id} is wired to {canary} but did not come up for this run")
            continue
        channel = instrument.kind.channels[wire.channel]
        label = f"{instrument_id} channel {wire.channel} -> {canary} GPIO{wire.pin}"
        try:
            if channel.drive:
                # The instrument drives, the board listens.
                board.gpio_mode(wire.pin, "input")
                for level in (1, 0):
                    instrument.write(wire.channel, level)
                    time.sleep(SETTLE_SECONDS)
                    read = board.gpio_read(wire.pin)
                    if read != level:
                        failures.append(f"{label}: the instrument drove {level}, the board read {read}")
                instrument.mode(wire.channel, "input")
            if can_drive(family, wire.pin):
                # The board drives, the instrument listens -- never both at
                # once, which on a wire is a short through the resistor.
                for level in (1, 0):
                    board.gpio_write(wire.pin, level)
                    time.sleep(SETTLE_SECONDS)
                    read = instrument.read(wire.channel)
                    if read != level:
                        failures.append(f"{label}: the board drove {level}, the instrument read {read}")
        finally:
            board.gpio_release(wire.pin)
            instrument.mode(wire.channel, "input")
    assert not failures, "\n".join(failures)
