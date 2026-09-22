"""Simulated board backend.

A SimHub hosts N virtual boards that speak the JSON serial protocol a HIL
agent speaks: a boot frame, `info`, the health check's commands, and
whatever the firmware a suite is written for answers besides. This exists
to test the HAL and suite orchestration logic on any machine -- it does NOT
exercise a library under test (that is what the hardware rig is for).

What a board answers beyond the common part is a *firmware* the hub is
given (`SimFirmware`): a suite whose agent has verbs of its own keeps a
simulation of them beside the suite and hands it to the hub through the
`sim_firmware` fixture (alteriom_hil.pytest_plugin). A hub given none is a
board that runs the health check's firmware and nothing else.

It simulates an I/O instrument too (instruments/esp32-io/firmware), wired to
the boards, so the canary's wiring check runs in CI: each wire is a net whose
level is whatever drives it, or its pull. That proves the check's
orchestration -- which end drives, which reads, what a failure names -- and
never a jumper.

It answers the farm canary's commands too (canary/firmware/src/main.cpp),
for the same reason and with the same limits: a green canary here says the
suite's orchestration is sound, never that an ESP is. What the canary asks
about — a cable that drops bytes, a radio that cannot see the rig, a flash
that has worn out — has no simulation worth writing, so the checks that
answer those carry `hil_only` and a sim pass is not evidence about
hardware. What this does catch is every bug in the suite before it costs
rig time.
"""

from __future__ import annotations

import json
import os
import queue
import secrets
import threading
import time


class _Pipe:
    """One direction of an in-memory line stream (thread-safe)."""

    def __init__(self):
        self._q: queue.Queue = queue.Queue()
        self._closed = False

    def write_line(self, line: str):
        self._q.put(line.rstrip("\n") + "\n")

    # file-like API used by SerialCapture ------------------------------
    def readline(self):
        try:
            return self._q.get(timeout=0.1).encode()
        except queue.Empty:
            return b""

    def write(self, data: bytes):
        raise NotImplementedError  # host writes go to the paired pipe

    def close(self):
        self._closed = True


class _HostStream:
    """What SerialCapture opens: reads board->host, writes host->board."""

    def __init__(self, board: "SimBoard"):
        self._board = board
        self._rx_buffer = ""

    def readline(self):
        return self._board.to_host.readline()

    def write(self, data: bytes):
        # Real serial writes may split a newline-delimited command across
        # multiple USB packets. Buffer until the protocol frame is complete.
        self._rx_buffer += data.decode()
        while "\n" in self._rx_buffer:
            line, self._rx_buffer = self._rx_buffer.split("\n", 1)
            if line.strip():
                self._board.handle_command(line)

    def flush(self):
        pass

    def close(self):
        pass


# What a simulated part says it is. Plausible rather than invented: a
# suite assertion about a field's shape ("a positive heap", "an RSSI a radio
# could have measured") must pass here for the same reason it passes on the
# bench, or the simulator teaches the suite to accept nonsense.
SIM_FAMILY = "esp32"
SIM_FLASH_BYTES = 4 * 1024 * 1024
SIM_RSSI = -58
SIM_INSTRUMENT_ID = "sim-io"


class _Pins:
    """What a simulated part's pins are set to, and what it reads through
    the hub's wires."""

    def __init__(self):
        self.state: dict[int, dict] = {}

    def set(self, pin: int, mode: str, level: int | None = None):
        entry = self.state.setdefault(pin, {"mode": "input", "level": 0})
        entry["mode"] = mode
        if level is not None:
            entry["level"] = level

    def get(self, pin: int) -> dict:
        return self.state.get(pin, {"mode": "input", "level": 0})

    def release_all(self):
        self.state.clear()


class SimFirmware:
    """What one firmware answers that every agent does not.

    The hub calls `handle` with each command the common part did not take
    (`info` and the health check's), and `info_fields` for what the firmware
    adds to an `info` frame. A firmware keeps whatever it needs across the
    hub's boards on itself; it is made once per hub, with the hub.
    """

    def __init__(self, hub: "SimHub"):
        self.hub = hub

    def info_fields(self, board: "SimBoard") -> dict:
        return {}

    def handle(self, board: "SimBoard", name: str, cmd: dict) -> bool:
        return False


class SimBoard:
    def __init__(self, hub: "SimHub", node_id: int):
        self.hub = hub
        self.node_id = node_id
        self.to_host = _Pipe()
        self.stalled_until = 0.0
        self.boot_id = secrets.token_hex(4)
        # The canary's key/value store, per board and lost on a restart in
        # the same way a part's NVS is not -- so the store check's write,
        # read and erase are the only things that put anything in it.
        self.store: dict = {}
        self.joined_to: str | None = None
        self.pins = _Pins()
        self.emit(self.boot_frame())

    # ---- host side ----
    def open_host_stream(self) -> _HostStream:
        return _HostStream(self)

    def emit(self, evt: dict):
        self.to_host.write_line(json.dumps(evt))

    @property
    def stalled(self) -> bool:
        return time.monotonic() < self.stalled_until

    def boot_frame(self) -> dict:
        return {
            "evt": "boot",
            "nodeId": self.node_id,
            "version": "sim",
            "family": SIM_FAMILY,
            "canarySha": "sim",
            "bootId": self.boot_id,
            "resetReason": "poweron",
            "store": "sim",
            "storeReady": True,
        }

    def info_frame(self) -> dict:
        return {
            "evt": "info",
            "nodeId": self.node_id,
            "version": "sim",
            "family": SIM_FAMILY,
            "canarySha": "sim",
            "bootId": self.boot_id,
            "resetReason": "poweron",
            "pinTable": SIM_FAMILY,
            "uptimeMs": int(time.monotonic() * 1000),
            "freeHeap": 180000,
            "mac": f"02:00:00:{self.node_id >> 16 & 0xFF:02x}:{self.node_id >> 8 & 0xFF:02x}:{self.node_id & 0xFF:02x}",
            "store": "sim",
            "silicon": {
                "chip": "ESP32-SIM",
                "revision": 1,
                "cores": 2,
                "cpuMhz": 240,
                "flashBytes": SIM_FLASH_BYTES,
                "sdk": "sim",
            },
            **self.hub.firmware.info_fields(self),
        }

    # ---- the canary's commands ----
    # The rig a simulated board is on is the one the environment describes,
    # so a canary run in sim exercises the same settings path a rig run
    # does: no access point configured means the radio checks skip here
    # exactly as they would on a rig whose gateway is off.

    def handle_canary_command(self, name: str, cmd: dict) -> bool:
        if name == "echo":
            text = str(cmd.get("text", ""))
            self.emit({"evt": "echo", "text": text, "len": len(text)})
        elif name in ("store_write", "store_read", "store_erase"):
            key = str(cmd.get("key", "canary"))
            frame = {"evt": "store", "key": key, "backing": "sim"}
            if name == "store_write":
                value = str(cmd.get("value", ""))
                self.store[key] = value
                frame.update(op="write", ok=True, bytes=len(value))
            elif name == "store_read":
                found = key in self.store
                frame.update(op="read", found=found, ok=found)
                if found:
                    frame["value"] = self.store[key]
            else:
                self.store.pop(key, None)
                frame.update(op="erase", ok=True)
            self.emit(frame)
        elif name == "wifi_scan":
            wanted = cmd.get("ssid")
            rig = os.environ.get("ALTERIOM_HIL_WIFI_SSID")
            networks = (
                [{"ssid": rig, "rssi": SIM_RSSI, "channel": 6}]
                if rig and (not wanted or wanted == rig)
                else []
            )
            frame = {"evt": "wifi_scan", "ok": True, "ms": 1200,
                     "count": 1 if rig else 0, "networks": networks}
            if wanted:
                frame["seen"] = bool(networks)
            self.emit(frame)
        elif name == "wifi_join":
            ssid = str(cmd.get("ssid", ""))
            rig = os.environ.get("ALTERIOM_HIL_WIFI_SSID")
            joined = bool(ssid) and ssid == rig
            frame = {"evt": "wifi_join", "ssid": ssid, "joined": joined,
                     "ok": joined, "ms": 900, "status": 3 if joined else 6}
            if joined:
                self.joined_to = ssid
                frame.update(
                    ip=f"10.42.0.{self.node_id % 200 + 20}",
                    rssi=SIM_RSSI,
                    channel=6,
                    gateway="10.42.0.1",
                )
            self.emit(frame)
        elif name == "wifi_leave":
            self.joined_to = None
            self.emit({"evt": "wifi_leave", "ok": True})
        elif name == "http_get":
            url = str(cmd.get("url", ""))
            reachable = self.joined_to is not None and url.startswith("http://")
            self.emit({
                "evt": "http_get", "url": url, "ok": reachable,
                "status": 200 if reachable else 0,
                "statusLine": "HTTP/1.1 200 OK" if reachable else "",
                "bytes": 2 if reachable else 0, "ms": 40,
                **({} if reachable else {"error": "not joined to any network"}),
            })
        elif name == "mqtt_publish":
            payload = str(cmd.get("payload", ""))
            reachable = self.joined_to is not None
            self.emit({
                "evt": "mqtt_publish", "topic": cmd.get("topic"),
                "clientId": f"canary-{self.boot_id}", "ok": reachable,
                "bytes": len(payload), "ms": 30,
                **({} if reachable else {"error": "not joined to any network"}),
            })
        elif name in ("gpio_mode", "gpio_write", "gpio_read", "gpio_release"):
            self.emit(self._gpio(name[len("gpio_"):], cmd))
        elif name == "reset":
            self.emit({"evt": "resetting", "bootId": self.boot_id})
            self.restart()
        else:
            return False
        return True

    def _gpio(self, op: str, cmd: dict) -> dict:
        from .pins import WIREABLE_PINS, can_drive

        pin = cmd.get("pin")
        frame = {"evt": "gpio", "op": op, "pin": pin}
        error = None
        if not isinstance(pin, int) or pin not in WIREABLE_PINS[SIM_FAMILY]:
            error = "not a wireable pin on this family"
        elif op == "mode":
            mode = cmd.get("mode")
            if mode not in ("input", "pullup", "pulldown", "output"):
                error = "mode must be input, pullup, pulldown or output"
            elif mode == "output" and not can_drive(SIM_FAMILY, pin):
                error = "an input-only pin"
            else:
                self.pins.set(pin, mode)
                frame["mode"] = mode
        elif op == "write":
            if not can_drive(SIM_FAMILY, pin):
                error = "an input-only pin"
            else:
                level = 1 if cmd.get("level") else 0
                self.pins.set(pin, "output", level)
                frame["level"] = level
        elif op == "read":
            frame["level"] = self.hub.read(self, pin)
        else:
            self.pins.set(pin, "input")
        frame["ok"] = error is None
        if error:
            frame["error"] = error
        return frame

    def restart(self):
        """What a reset does: a new session, and nothing kept in RAM."""
        self.boot_id = secrets.token_hex(4)
        self.joined_to = None
        self.pins.release_all()
        self.stalled_until = 0.0
        frame = self.boot_frame()
        frame["resetReason"] = "software"
        self.emit(frame)

    def handle_command(self, line: str):
        # Mirror the firmware: during a stall (a firmware's to cause -- see
        # `stalled_until`) the agent does not service serial either; commands
        # are buffered and processed on recovery.
        if self.stalled:
            delay = self.stalled_until - time.monotonic() + 0.01
            t = threading.Timer(delay, lambda: self.handle_command(line))
            t.daemon = True
            t.start()
            return
        cmd = json.loads(line)
        name = cmd["cmd"]
        if name == "info":
            self.emit(self.info_frame())
        elif self.handle_canary_command(name, cmd):
            return
        elif self.hub.firmware.handle(self, name, cmd):
            return
        else:
            self.emit({"evt": "error", "error": f"unknown cmd {name}"})


class SimInstrument:
    """An esp32-io instrument that exists only in memory, speaking the
    instrument firmware's protocol, wired to the hub's boards."""

    def __init__(self, hub: "SimHub", instrument_id: str = SIM_INSTRUMENT_ID):
        from .instrument import KINDS

        self.hub = hub
        self.id = instrument_id
        self.kind = KINDS["esp32-io"]
        self.to_host = _Pipe()
        self.pins = _Pins()
        self.stalled_until = 0.0
        self.mac = "02:00:00:00:10:00"
        self.emit({"evt": "boot", **self._describe()})

    def _describe(self) -> dict:
        return {"role": "instrument", "kind": self.kind.name, "protocol": 1, "fw": "sim",
                "target": self.kind.target, "mac": self.mac}

    def open_host_stream(self) -> _HostStream:
        return _HostStream(self)

    def emit(self, evt: dict):
        self.to_host.write_line(json.dumps(evt))

    def handle_command(self, line: str):
        cmd = json.loads(line)
        name = cmd.get("cmd")
        if name == "info":
            channels = [{"ch": ch.pin, "drive": ch.drive, "pull": ch.pull, "adc": ch.adc}
                        for ch in self.kind.channels.values()]
            return self.emit({"evt": "info", **self._describe(), "channels": channels})
        if name == "release":
            self.pins.release_all()
            return self.emit({"evt": "release", "ok": True})
        channel = self.kind.channels.get(cmd.get("ch"))
        if channel is None:
            return self.emit({"evt": "error", "cmd": name, "error": "no such channel"})
        pin = channel.pin
        if name == "mode":
            mode = cmd.get("mode")
            if mode not in ("input", "pullup", "pulldown", "output"):
                return self.emit({"evt": "error", "cmd": name, "error": "mode must be input, pullup, pulldown or output"})
            if mode == "output" and not channel.drive:
                return self.emit({"evt": "error", "cmd": name, "error": "this channel is input-only"})
            if mode in ("pullup", "pulldown") and not channel.pull:
                return self.emit({"evt": "error", "cmd": name, "error": "this channel has no internal pulls"})
            self.pins.set(pin, mode)
            return self.emit({"evt": "mode", "ch": pin, "mode": mode})
        if name in ("write", "pulse"):
            if not channel.drive:
                return self.emit({"evt": "error", "cmd": name, "error": "this channel is input-only"})
            level = 1 if cmd.get("level") else 0
            self.pins.set(pin, "output", level)
            frame = {"evt": name, "ch": pin, "level": level}
            if name == "pulse":
                self.pins.set(pin, "output", 1 - level)
                frame["ms"] = cmd.get("ms", 100)
            return self.emit(frame)
        if name == "read":
            return self.emit({"evt": "read", "ch": pin, "level": self.hub.read(self, pin)})
        if name == "adc":
            if not channel.adc:
                return self.emit({"evt": "error", "cmd": name, "error": "this channel cannot measure a voltage"})
            return self.emit({"evt": "adc", "ch": pin, "mv": 3300 if self.hub.read(self, pin) else 0,
                              "samples": cmd.get("samples", 8)})
        if name == "count":
            # Nothing in the simulator toggles a line on its own.
            return self.emit({"evt": "count", "ch": pin, "edges": 0, "ms": cmd.get("ms", 1000)})
        if name == "wait_edge":
            return self.emit({"evt": "edge", "ch": pin, "timeout": True})
        return self.emit({"evt": "error", "cmd": name, "error": "unknown command"})


class SimHub:
    """An in-process bank of SimBoards, an instrument wired to them, and the
    firmware they run beyond the common part (`SimFirmware`)."""

    # Which instrument channels a simulated board is wired to, and to which of
    # its pins: two lines either end can drive, and one only the board can --
    # the three shapes a real wiring takes. Enough for four boards; a fifth
    # board and beyond get the two-way wires only, while channels last.
    _TWO_WAY = (4, 13, 14, 16, 17, 18, 19, 21, 22, 23, 25, 26, 27)
    _READ_ONLY = (34, 35, 36, 39)

    def __init__(self, n_boards: int, base_node_id: int = 1000, instruments: bool = False,
                 firmware: type | None = None):
        # Before the boards: a board's boot frame may ask the firmware.
        self.firmware = (firmware or SimFirmware)(self)
        self.boards = [SimBoard(self, base_node_id + i) for i in range(n_boards)]
        self.instruments: list[SimInstrument] = []
        # (instrument, channel, board, pin): one net per wire.
        self.wires: list[tuple[SimInstrument, int, SimBoard, int]] = []
        if instruments and self.boards:
            instrument = SimInstrument(self)
            self.instruments.append(instrument)
            two_way = list(self._TWO_WAY)
            read_only = list(self._READ_ONLY)
            for board in self.boards:
                for pin in (25, 26):
                    if two_way:
                        self.wires.append((instrument, two_way.pop(0), board, pin))
                if read_only:
                    self.wires.append((instrument, read_only.pop(0), board, 27))
        # Wires to open, so a suite's failure path can be exercised in CI:
        # "sim-1000:25,sim-1001:27" -- the board end of each broken wire.
        self.broken = {
            item.strip() for item in os.environ.get("ALTERIOM_HIL_SIM_BROKEN_WIRES", "").split(",") if item.strip()
        }

    def board_id(self, board: SimBoard) -> str:
        return f"sim-{board.node_id}"

    def read(self, part, pin: int) -> int:
        """The level a part reads on a pin: the net's, when a wire joins it to
        something; its own otherwise."""
        net = [(part, pin)]
        for instrument, channel, board, board_pin in self.wires:
            if f"{self.board_id(board)}:{board_pin}" in self.broken:
                continue
            if (instrument, channel) == (part, pin):
                net.append((board, board_pin))
            elif (board, board_pin) == (part, pin):
                net.append((instrument, channel))
        states = [end.pins.get(end_pin) for end, end_pin in net]
        drivers = [state["level"] for state in states if state["mode"] == "output"]
        if drivers:
            # Two outputs fighting over one wire: the series resistors of the
            # real rig make that a divider, and a reading nobody should trust.
            # The simulator says low, and a check that drives both ends is the
            # bug to find.
            return drivers[0] if len(set(drivers)) == 1 else 0
        if any(state["mode"] == "pullup" for state in states):
            return 1
        return 0

    def by_id(self, node_id: int):
        for b in self.boards:
            if b.node_id == node_id:
                return b
        return None
