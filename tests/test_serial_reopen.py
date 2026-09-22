"""SerialCapture must survive the device node disappearing.

Cutting USB power (the rung-1 recovery in docs/runbook.md) destroys the
board's /dev node. Without an explicit reattach the reader thread keeps
polling a dead fd and the board stays mute for the rest of the session,
so power-cycle recovery is impossible from inside a run.
"""

from __future__ import annotations

import pytest

from alteriom_hil.serial_capture import SerialCapture


class FakeStream:
    """Minimal serial stand-in that can be 'unplugged'."""

    def __init__(self, lines=()):
        self._lines = list(lines)
        self.closed = False
        self.dead = False
        self.written = []

    def readline(self):
        if self.dead:
            raise OSError(5, "Input/output error")  # what a yanked USB gives
        if self._lines:
            return self._lines.pop(0).encode()
        return b""

    def write(self, data):
        if self.dead:
            raise OSError(5, "Input/output error")
        self.written.append(data)

    def close(self):
        self.closed = True


def test_reopen_replaces_a_dead_stream():
    streams = [
        FakeStream(['{"evt":"boot","n":1}\n']),
        FakeStream(['{"evt":"boot","n":2}\n']),
    ]
    opener = lambda: streams.pop(0)  # noqa: E731

    cap = SerialCapture(opener).start()
    assert cap.next_event(timeout=2)["n"] == 1

    # board is power-cut: stream goes bad
    cap._stream.dead = True

    cap.reopen(timeout=2)
    assert cap.next_event(timeout=2)["n"] == 2, "no events after reattach"
    cap.stop()


def test_reopen_retries_until_the_device_reappears():
    """udev takes a moment to recreate the node after power returns."""
    attempts = {"n": 0}

    def opener():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise OSError(2, "No such file or directory")
        return FakeStream(['{"evt":"ready"}\n'])

    cap = SerialCapture(opener)
    cap._stream = FakeStream()
    cap.reopen(timeout=5, retry_interval=0.01)
    assert attempts["n"] == 3
    assert cap.next_event(timeout=2)["evt"] == "ready"
    cap.stop()


def test_reopen_raises_if_device_never_returns():
    def opener():
        raise OSError(2, "No such file or directory")

    cap = SerialCapture(opener)
    cap._stream = FakeStream()
    with pytest.raises(OSError):
        cap.reopen(timeout=0.2, retry_interval=0.01)


def test_reopen_drops_stale_events_but_keeps_raw_log():
    """Events queued before the cut describe a board that no longer exists."""
    s1 = FakeStream(['{"evt":"old"}\n'])
    s2 = FakeStream(['{"evt":"new"}\n'])
    streams = [s1, s2]
    cap = SerialCapture(lambda: streams.pop(0)).start()

    # let the pre-cut event land in the queue, unconsumed
    for _ in range(100):
        if cap.raw_log:
            break
        import time

        time.sleep(0.01)

    cap.reopen(timeout=2)
    evt = cap.next_event(timeout=2)
    assert evt["evt"] == "new", f"stale event survived reattach: {evt}"
    assert any("old" in line for line in cap.raw_log), "raw log lost history"
    cap.stop()


def test_reopen_closes_the_old_stream():
    s1 = FakeStream()
    s2 = FakeStream()
    streams = [s1, s2]
    cap = SerialCapture(lambda: streams.pop(0)).start()
    cap.reopen(timeout=2)
    assert s1.closed, "old fd leaked"
    cap.stop()
