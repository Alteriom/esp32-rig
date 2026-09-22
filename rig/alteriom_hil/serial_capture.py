"""Threaded serial capture: newline-delimited JSON events + raw log ring.

The HIL firmware protocol is one JSON object per line on the board's serial
port. Anything that does not parse as JSON (boot ROM chatter, library debug
output) is preserved in the raw log for post-mortem but not treated as an
event.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from collections import deque
import unicodedata
from typing import Callable, Optional

# Events the agent frames as JSON so they cannot splice into a protocol line,
# but which no test waits for. They stay in the raw log and out of the queue.
DIAGNOSTIC_EVENTS = frozenset({"mesh_log"})


class SerialCapture:
    """Reads a byte stream on a background thread and queues JSON events.

    ``opener`` is a zero-arg callable returning a file-like object with
    ``readline()`` and ``write()`` — normally a ``serial.Serial``, or an
    in-memory pipe in tests/sim. Keeping the opener injectable is what lets
    the whole HAL run without hardware.
    """

    def __init__(self, opener: Callable, raw_log_lines: int = 5000):
        self._opener = opener
        self._stream = None
        self._events: queue.Queue = queue.Queue()
        self._raw_log: deque = deque(maxlen=raw_log_lines)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._stream = self._opener()
        self._stop.clear()
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._close_stream()

    def _close_stream(self):
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def pulse_reset(self) -> bool:
        """Reset the attached board by driving its RTS/DTR lines.

        Every ESP dev board in the rig — CP210x, CH340, and the native
        USB-Serial/JTAG parts alike — wires those lines to EN/BOOT, which is
        the same sequence esptool uses. It is the only way back from a board
        that has hard-hung: no watchdog fires, nothing is printed, and the
        port stays open and silent until the chip is reset.

        Returns False when the stream has no modem lines (the in-memory pipe
        used by tests and sim mode), so callers can say so rather than
        believing they reset something.
        """
        stream = self._stream
        if stream is None or not hasattr(stream, "rts"):
            return False
        try:
            # EN low with BOOT released, so the board comes back running the
            # application rather than in the bootloader.
            stream.dtr = False
            stream.rts = True
            time.sleep(0.15)
            stream.rts = False
            time.sleep(0.05)
        except Exception:
            return False
        return True

    def reopen(self, timeout: float = 30.0, retry_interval: float = 0.5):
        """Reattach to the board after its device node went away.

        Power-cycling a port (see :mod:`alteriom_hil.power`) destroys the
        board's ``/dev`` node; the old fd never recovers, so a capture must
        be explicitly reattached once udev recreates the node. Retries until
        ``timeout`` because the node takes a second or two to reappear.

        Events queued before the cut describe a board that has since
        rebooted, so they are dropped. The raw log is preserved — it is the
        post-mortem trail for whatever wedged the board.

        Raises the last opener error if the device never comes back.
        """
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        self._close_stream()

        deadline = time.monotonic() + timeout
        last_exc = None
        while True:
            try:
                self._stream = self._opener()
                break
            except Exception as exc:  # device not back yet
                last_exc = exc
                if time.monotonic() >= deadline:
                    raise last_exc
                time.sleep(retry_interval)

        # Stale events refer to the pre-reboot board; the raw log stays.
        self.drain()
        self._stop.clear()
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()
        return self

    # A frame is one line. pyserial's readline() returns whatever it has when
    # the read timeout (100 ms on the rig) expires, so a frame the board
    # emits in two bursts a little apart arrives as two "lines", neither of
    # them JSON: the rig recorded `{"evt":"internet_queued","t` and
    # `ag":"recovery-…"}` as consecutive lines, and the test waiting for that
    # event timed out with the event on the wire. A read that does not end
    # in a newline is held and joined to the next; a fragment that stays
    # alone longer than this is passed on as it is, so a board that prints
    # without newlines (a boot ROM banner) cannot stall the reader.
    # A single "line" is capped before it reaches the raw log. An ESP32's boot
    # ROM prints at 74880 baud; read at 115200 that is binary noise containing
    # no newline, so readline() accumulates it until the firmware's first real
    # output. One reset produced a 74 KB line -- a serial log of 318 lines and
    # 91 KB, unreadable in an editor or a CI artifact preview, and occupying a
    # single slot of a ring sized in lines rather than bytes.
    #
    # Truncating keeps the signal that something unreadable arrived, which is
    # itself diagnostic (it means the board reset), without the bulk.
    MAX_LOG_LINE = 2000
    PARTIAL_LINE_MAX_WAIT = 1.0
    # A fragment that begins a JSON frame is worth more patience: the boards
    # stall mid-frame for longer than a second — the S3 recorded
    # `{"evt":"node_list","nodes":[319` and `8819345,…]}` as consecutive
    # lines, over a second apart, two to five times a run — and a frame
    # passed on in two halves is an event lost and a test timed out on it.
    PARTIAL_FRAME_MAX_WAIT = 5.0

    def _reader(self):
        partial = b""
        partial_since = 0.0
        # The head of a frame that had to be passed on before its tail came.
        # It is kept out of the queue and joined to the next line for
        # parsing only, so a tail that arrives after the wait still makes
        # the event; the raw log keeps both halves as they were read.
        carry = ""
        while not self._stop.is_set():
            try:
                line = self._stream.readline()
            except Exception:
                if self._stop.is_set():
                    return
                time.sleep(0.05)
                continue
            if isinstance(line, str):
                line = line.encode("utf-8")
            if line:
                if not partial:
                    partial_since = time.monotonic()
                partial += line
            if not partial:
                continue
            complete = partial.endswith(b"\n")
            wait = (
                self.PARTIAL_FRAME_MAX_WAIT
                if partial.lstrip().startswith(b"{")
                else self.PARTIAL_LINE_MAX_WAIT
            )
            if not complete and (time.monotonic() - partial_since < wait):
                continue
            line = partial.decode("utf-8", errors="replace")
            partial = b""
            line = line.strip()
            if not line:
                continue
            self._raw_log.append(self._bounded(line))
            events = self._extract_events(line)
            if not events and carry:
                events = self._extract_events(carry + line)
            carry = ""
            if not events and not complete and line.startswith("{"):
                carry = line
            for evt in events:
                if evt["evt"] in DIAGNOSTIC_EVENTS:
                    # Framed for the serial log, not for a waiting test: a
                    # regular node logs its radio state several times a
                    # minute, and queueing that would leave every wait_for
                    # walking past hundreds of lines nothing consumes.
                    continue
                self._events.put(evt)

    @classmethod
    def _bounded(cls, line: str) -> str:
        """The line as the raw log should keep it: printable, and bounded.

        Only the log is treated this way -- event extraction still sees the
        whole original line, because a long line is not necessarily a damaged
        one and a control byte in the middle of a frame is still part of it.

        Printable matters as much as bounded. The ESP32 boot ROM header is
        binary at this baud rate, and a single control byte anywhere in a file
        makes grep call the whole thing binary: it then prints "binary file
        matches" and none of the matching lines. Someone reading a failed run
        greps for the value they care about, gets that one line back, and
        concludes the board never printed it.

        Emoji survive -- the firmware logs them on purpose -- because only
        control characters are replaced, not everything non-ASCII.
        """
        line = cls._printable(line)
        if len(line) <= cls.MAX_LOG_LINE:
            return line
        dropped = len(line) - cls.MAX_LOG_LINE
        return f"{line[:cls.MAX_LOG_LINE]}… [{dropped} more characters not logged]"

    @staticmethod
    def _printable(line: str) -> str:
        """Replace control characters with a dot, keeping tabs.

        U+FFFD is left as it is: it means a byte that was not valid UTF-8,
        which is worth seeing rather than smoothing over.
        """
        return "".join(
            c if c == "	" or unicodedata.category(c)[0] != "C" else "."
            for c in line
        )

    @staticmethod
    def _extract_events(line: str) -> list[dict]:
        """Recover valid event objects from a possibly damaged serial line.

        USB serial drivers can occasionally lose a newline or the tail of a
        frame during device re-enumeration. A later complete JSON event must
        still be usable instead of poisoning the entire ``readline`` result.
        Trying each object boundary also handles multiple valid events joined
        by a missing newline while retaining the original bytes in raw_log.
        """
        decoder = json.JSONDecoder()
        events = []
        offset = 0
        while True:
            start = line.find("{", offset)
            if start < 0:
                return events
            try:
                evt, end = decoder.raw_decode(line, start)
            except json.JSONDecodeError:
                offset = start + 1
                continue
            if isinstance(evt, dict) and "evt" in evt:
                events.append(evt)
            offset = max(end, start + 1)

    def write_line(self, text: str):
        data = (text.rstrip("\n") + "\n").encode("utf-8")
        try:
            self._write_framed(data)
        except (OSError, IOError):
            # Native USB ESPs can re-enumerate between commands. The stable
            # by-id opener will follow the board, but the old file descriptor
            # still returns EIO and must be replaced before retrying.
            self.reopen()
            self._write_framed(data)

    def _write_framed(self, data: bytes, chunk_size: int = 128):
        """Pace long JSON frames for native-USB ESP receive buffers."""
        for offset in range(0, len(data), chunk_size):
            self._write(data[offset : offset + chunk_size])
            if len(data) > chunk_size:
                time.sleep(0.003)

    def _write(self, data: bytes):
        self._stream.write(data)
        flush = getattr(self._stream, "flush", None)
        if flush:
            flush()

    def next_event(self, timeout: float = 5.0) -> Optional[dict]:
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self) -> list[dict]:
        out = []
        while True:
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                return out

    @property
    def raw_log(self) -> list[str]:
        return list(self._raw_log)
