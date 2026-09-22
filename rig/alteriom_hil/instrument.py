"""Talking to an I/O instrument: the farm's own test equipment, on a port.

What an instrument *is* -- its kinds and channels, its registration, its
wiring and the rules a wire is held to -- is `alteriom_hil.instrument_registry`,
which needs no hardware and is what a portal reads. This is the half that
does: the client that drives one over its newline-JSON protocol
(`instruments/esp32-io/`, docs/io-instrument.md). Everything the registry
defines is importable from here as it always was.
"""

from __future__ import annotations

from .instrument_registry import (  # noqa: F401 -- re-exported
    ID_PATTERN,
    KINDS,
    MAC_PATTERN,
    PROTOCOL_VERSION,
    Channel,
    Instrument,
    InstrumentKind,
    Wire,
    instruments_document,
    instruments_path_for,
    load_instruments,
    validate_instruments,
    wired_to,
)
from .protocol import BoardClient, ProtocolError


# ---- talking to one ----------------------------------------------------------

MODES = ("input", "pullup", "pulldown", "output")
EDGES = ("rising", "falling", "any")


class InstrumentClient:
    """Drive an instrument over its newline-JSON protocol.

    The same framing as a HIL agent, so a command the instrument could not
    parse is resent and one that ran is never resent. A command the
    instrument refused -- a channel it does not have, a mode that channel
    cannot take -- raises ProtocolError with its reason.
    """

    def __init__(self, instrument_id: str, capture, kind: str = "esp32-io"):
        self.instrument_id = instrument_id
        self.kind = KINDS[kind]
        self._link = BoardClient(instrument_id, capture)

    def _call(self, cmd: str, reply: str, timeout: float = 5.0, **fields) -> dict:
        event = self._link.send_cmd_awaiting(
            cmd,
            lambda e: e.get("evt") == reply or (e.get("evt") == "error" and e.get("cmd") == cmd),
            f"{cmd} reply",
            timeout,
            **fields,
        )
        if event.get("evt") == "error":
            raise ProtocolError(f"{self.instrument_id}: {cmd} refused: {event.get('error')}")
        return event

    def _channel(self, channel: int, drive: bool = False, adc: bool = False) -> Channel:
        found = self.kind.channels.get(channel)
        if found is None:
            raise ValueError(f"{self.instrument_id}: {self.kind.name} has no channel {channel}")
        if drive and not found.drive:
            raise ValueError(f"{self.instrument_id}: channel {channel} is input-only")
        if adc and not found.adc:
            raise ValueError(f"{self.instrument_id}: channel {channel} cannot measure a voltage")
        return found

    def info(self, timeout: float = 10.0) -> dict:
        """What the instrument says it is. Refuses a device that is not one:
        a board still running a suite's firmware answers `info` too."""
        event = self._link.info(timeout=timeout)
        if event.get("role") != "instrument":
            raise ProtocolError(
                f"{self.instrument_id} answered as {event.get('role') or 'a board'}, not an "
                f"instrument: is the {self.kind.name} firmware flashed on it?"
            )
        if event.get("kind") != self.kind.name:
            raise ProtocolError(f"{self.instrument_id} is a {event.get('kind')}, registered as {self.kind.name}")
        if event.get("protocol") != PROTOCOL_VERSION:
            raise ProtocolError(
                f"{self.instrument_id} speaks instrument protocol {event.get('protocol')}; this farm speaks {PROTOCOL_VERSION}"
            )
        return event

    def mode(self, channel: int, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        found = self._channel(channel, drive=mode == "output")
        if mode in ("pullup", "pulldown") and not found.pull:
            raise ValueError(f"{self.instrument_id}: channel {channel} has no internal pulls")
        self._call("mode", "mode", ch=channel, mode=mode)

    def write(self, channel: int, level: int) -> None:
        """Drive a level. The channel becomes an output if it was not one."""
        self._channel(channel, drive=True)
        self._call("write", "write", ch=channel, level=1 if level else 0)

    def read(self, channel: int) -> int:
        self._channel(channel)
        return int(self._call("read", "read", ch=channel)["level"])

    def millivolts(self, channel: int, samples: int = 8) -> int:
        self._channel(channel, adc=True)
        return int(self._call("adc", "adc", ch=channel, samples=max(1, min(int(samples), 64)))["mv"])

    def pulse(self, channel: int, level: int, ms: int) -> None:
        """Drive `level` for `ms` milliseconds, then the opposite level."""
        self._channel(channel, drive=True)
        ms = max(1, min(int(ms), 10_000))
        self._call("pulse", "pulse", timeout=ms / 1000 + 5, ch=channel, level=1 if level else 0, ms=ms)

    def count(self, channel: int, ms: int, edge: str = "rising") -> int:
        """How many edges arrive in a window of `ms` milliseconds."""
        if edge not in EDGES:
            raise ValueError(f"edge must be one of {EDGES}")
        self._channel(channel)
        ms = max(1, min(int(ms), 10_000))
        return int(self._call("count", "count", timeout=ms / 1000 + 5, ch=channel, ms=ms, edge=edge)["edges"])

    def wait_edge(self, channel: int, edge: str = "any", timeout_ms: int = 2000) -> dict | None:
        """The first edge within `timeout_ms`, as `{"level", "afterMs"}`, or None."""
        if edge not in EDGES:
            raise ValueError(f"edge must be one of {EDGES}")
        self._channel(channel)
        timeout_ms = max(1, min(int(timeout_ms), 60_000))
        event = self._call("wait_edge", "edge", timeout=timeout_ms / 1000 + 5, ch=channel, edge=edge, timeoutMs=timeout_ms)
        return None if event.get("timeout") else {"level": int(event["level"]), "afterMs": int(event["afterMs"])}

    def release(self) -> None:
        """Every channel back to an input with no pull: the state nothing it is
        wired to can be harmed by. The instrument boots this way too."""
        self._call("release", "release")
