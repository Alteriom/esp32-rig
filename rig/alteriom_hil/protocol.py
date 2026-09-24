"""Command/event protocol client for a HIL agent board.

The agent firmware speaks newline-delimited JSON in both directions:

    host -> board:  {"cmd": "send_single", "dest": 123, "msg": "x", "ack": true}
    board -> host:  {"evt": "ack", "node": 123, "delivered": true, "latencyMs": 42}

``BoardClient`` wraps a SerialCapture with a ``wait_for(predicate)`` primitive
that every suite assertion builds on, and the few things any agent can be
asked: a reset, whether it answers, what it is running. Events that arrive
while waiting for something else are retained (in order) for later waits, so
tests never lose events to races.

What one firmware in particular can be asked is that firmware's suite's to
say, as a subclass kept beside the suite -- the health check's is
`suites/canary/tests/canary_client.py`, the I/O instrument's is
`alteriom_hil.instrument`. A suite has its boards built as its own class with
the `board_client_class` fixture (alteriom_hil.pytest_plugin).
"""

from __future__ import annotations

import json
import time
from typing import Callable

from .serial_capture import SerialCapture


class ProtocolError(RuntimeError):
    pass


class TimeoutWaitingFor(AssertionError):
    """A wait_for() deadline passed. Carries recent raw log for diagnosis."""

    def __init__(self, description: str, board_id: str, raw_tail: list[str]):
        # Kept as an attribute, not only inside the message: the run hook that
        # recovers a wedged board needs to know which board without parsing
        # prose back out of an exception.
        self.board_id = board_id
        self.description = description
        tail = "\n".join(raw_tail[-25:])
        super().__init__(
            f"[{board_id}] timed out waiting for {description}\n"
            f"--- last serial lines ---\n{tail}"
        )


class BoardClient:
    def __init__(self, board_id: str, capture: SerialCapture):
        self.board_id = board_id
        self.capture = capture
        self._pending: list[dict] = []  # events seen but not yet consumed

    # ---- low level ----

    def send_cmd(self, cmd: str, **kwargs):
        payload = {"cmd": cmd}
        payload.update(kwargs)
        self.capture.write_line(json.dumps(payload))

    def wait_for(
        self,
        predicate: Callable[[dict], bool],
        description: str,
        timeout: float = 10.0,
        consume: bool = True,
    ) -> dict:
        """Return the first event matching ``predicate`` within ``timeout``.

        Non-matching events are retained for subsequent waits.
        """
        deadline = time.monotonic() + timeout
        for i, evt in enumerate(self._pending):
            if predicate(evt):
                return self._pending.pop(i) if consume else evt
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutWaitingFor(
                    description, self.board_id, self.capture.raw_log
                )
            evt = self.capture.next_event(timeout=min(remaining, 0.5))
            if evt is None:
                continue
            if predicate(evt):
                return evt
            self._pending.append(evt)

    def clear_pending(self):
        """Forget every event seen so far — held back or not yet read.

        A retry that only cleared the held-back list still met the previous
        attempt's late arrivals: a broadcast's delivered=false ack lands at
        its 5 s timeout, after the test has already given up on that attempt
        and slept, and the next attempt's wait_ack consumed it as its own.
        """
        self._pending.clear()
        self.capture.drain()

    def reattach(self, timeout: float = 30.0):
        """Reconnect after the board's device node went away.

        Use after cutting and restoring a board's power: the serial port is
        reopened once udev recreates it, and every event buffered from
        before the reboot is discarded.
        """
        self.capture.reopen(timeout=timeout)
        self.clear_pending()
        return self

    def send_cmd_awaiting(
        self,
        cmd: str,
        predicate: Callable[[dict], bool],
        description: str,
        timeout: float = 10.0,
        idempotent: bool = False,
        **kwargs,
    ) -> dict:
        """Send a command and wait for its reply, resending a corrupted one.

        The agent answers a frame it could not parse with
        ``{"evt":"error","error":"bad json"}``. That is a precise statement
        that the command did *not* run, which makes resending safe — and
        makes losing it needless. Seen on the ESP8266, which does a blocking
        all-channel scan and can overrun its receive buffer while a command
        is arriving; the lost command was a role change, so the whole
        module's fixture failed with the board perfectly healthy.

        Only a reported parse failure is retried. A command that ran and
        whose reply went missing is never resent — for a role change that
        reboots the board, that would be a different and worse guess.

        A command that is safe to repeat -- a scan, a read -- may say so
        with ``idempotent=True``, and is then also resent when what came
        back could not be read: the capture frames a line it received with
        undecodable bytes and no event as ``{"evt":"unreadable"}``. Seen on
        esp32-fde4 (farm run be46491e): its scan reply arrived with the
        first 32 characters as 64 undecodable bytes and the rest intact, the
        board neither reset nor slow, and the check timed out at 45 s with
        the answer in the serial log. Nothing else can tell a damaged reply
        from a silent board. An ``unreadable`` that was only noise costs a
        repeatable command one more run; a command that is not safe to
        repeat ignores it, as before.
        """
        def resend_on(e: dict) -> bool:
            if e["evt"] == "error":
                error = str(e.get("error", ""))
                return error == "bad json" or error.startswith("frame dropped")
            return idempotent and e["evt"] == "unreadable"

        deadline = time.monotonic() + timeout
        self.clear_pending()
        self.send_cmd(cmd, **kwargs)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutWaitingFor(
                    description, self.board_id, self.capture.raw_log
                )
            evt = self.wait_for(
                lambda e: predicate(e) or resend_on(e), description, remaining
            )
            if predicate(evt):
                return evt
            self.send_cmd(cmd, **kwargs)

    def hard_reset(self, timeout: float = 30.0) -> dict:
        """Reset the board over RTS/DTR and wait for it to announce itself.

        For a board that has hard-hung this is the only way back: it prints
        nothing, answers nothing, and no watchdog rescues it.
        """
        # Drop whatever the wedged board left behind *before* pulsing: the
        # pulse itself takes 200 ms and a board can boot inside that, so
        # clearing afterwards throws away the boot frame being waited for.
        self.clear_pending()
        if not self.capture.pulse_reset():
            raise ProtocolError(
                f"{self.board_id}: this capture has no reset lines to pulse"
            )
        return self.wait_for(
            lambda e: e["evt"] == "boot", "boot after reset", timeout
        )

    def ensure_responsive(
        self, timeout: float = 20.0, after_reset_timeout: float = 60.0
    ) -> dict:
        """Return the board's ``info``, resetting it once if it has wedged.

        A board that hangs mid-suite stays hung — the port stays open and
        silent — so without this every later run inherits it too, and fails
        somewhere new each time depending on which test needed that board.
        One reset costs a few seconds; not resetting costs every run until
        somebody notices.

        The budget after a reset is much larger than the one before it,
        because a board that has just booted is legitimately slow: measured
        on this rig, reset to `boot` is about a second on every family, but
        reset to a first `info` reply is 7 s on most and **15 s on the
        ESP32-C5**. Reusing the pre-check budget would reset a wedged C5 and
        then declare the recovered board dead.
        """
        try:
            return self.info(timeout=timeout)
        except TimeoutWaitingFor:
            pass
        self.hard_reset()
        return self.info(timeout=after_reset_timeout)

    # ---- typed helpers (mirror the agent firmware commands) ----

    def info(self, timeout: float = 10.0) -> dict:
        last_timeout = None
        for _ in range(2):
            self.send_cmd("info")
            try:
                return self.wait_for(
                    lambda e: e["evt"] == "info", "info reply", timeout / 2
                )
            except TimeoutWaitingFor as exc:
                last_timeout = exc
        assert last_timeout is not None
        raise last_timeout
