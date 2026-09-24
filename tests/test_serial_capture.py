import queue
import time


from alteriom_hil.serial_capture import SerialCapture


class FakeStream:
    """Line source with a write sink, mimicking a serial port."""

    def __init__(self, lines):
        self._q = queue.Queue()
        for l in lines:
            self._q.put(l)
        self.written = []

    def readline(self):
        try:
            return self._q.get(timeout=0.05)
        except queue.Empty:
            return b""

    def write(self, data):
        self.written.append(data)

    def flush(self):
        pass

    def close(self):
        pass


def test_json_events_parsed_and_noise_kept_in_raw_log():
    stream = FakeStream(
        [
            b"ets Jun  8 2016 00:22:57\n",  # boot ROM noise
            b'{"evt":"boot","nodeId":123}\n',
            b"not json at all\n",
            b'{"not_an_event":1}\n',  # json but no evt key -> ignored
            b'{"evt":"info","nodeId":123,"version":"x"}\n',
        ]
    )
    cap = SerialCapture(lambda: stream).start()
    try:
        e1 = cap.next_event(timeout=2)
        e2 = cap.next_event(timeout=2)
        assert e1 == {"evt": "boot", "nodeId": 123}
        assert e2["evt"] == "info"
        assert cap.next_event(timeout=0.2) is None
        raw = cap.raw_log
        assert "ets Jun  8 2016 00:22:57" in raw
        assert "not json at all" in raw
    finally:
        cap.stop()


def test_a_frame_that_arrives_in_two_reads_is_one_event():
    # pyserial's readline() hands back whatever it has when the read timeout
    # expires. The rig recorded a frame as `{"evt":"internet_queued","t` and
    # `ag":"recovery-…"}` on consecutive reads, and the test waiting for that
    # event timed out with the event on the wire.
    stream = FakeStream(
        [
            b'{"evt":"internet_queued","t',
            b'ag":"recovery-1","messageId":969736196}\n',
            b'{"evt":"info","nodeId":7}\n',
        ]
    )
    cap = SerialCapture(lambda: stream).start()
    try:
        first = cap.next_event(timeout=2)
        assert first == {"evt": "internet_queued", "tag": "recovery-1", "messageId": 969736196}
        assert cap.next_event(timeout=2) == {"evt": "info", "nodeId": 7}
    finally:
        cap.stop()


def test_a_fragment_without_a_newline_is_still_passed_on():
    # A boot ROM banner, or a frame whose tail was lost for good, must not
    # stall the reader: alone for longer than the wait, it goes through as it
    # is, and the frames after it are read normally.
    stream = FakeStream([b"ets Jun  8 2016 00:22:57", b'{"evt":"boot","nodeId":1}\n'])
    cap = SerialCapture(lambda: stream).start()
    try:
        assert cap.next_event(timeout=4) == {"evt": "boot", "nodeId": 1}
        assert any("ets Jun" in line for line in cap.raw_log)
    finally:
        cap.stop()


class PacedStream(FakeStream):
    """A FakeStream whose items may be (delay_seconds, bytes) pairs."""

    def readline(self):
        item = super().readline()
        if isinstance(item, tuple):
            delay, data = item
            time.sleep(delay)
            return data
        return item


def test_a_frame_head_that_waited_out_the_stall_is_completed_by_its_tail():
    # The S3 stalls mid-frame for over a second: `{"evt":"node_list","nodes":[319`
    # then `8819345,…]}` a good while later. Held for the frame wait it is
    # one event; and even once passed on, the head is joined to the tail
    # for parsing, so the event is not lost either way.
    # The stall is a run of empty reads, as pyserial's timeout produces it;
    # the head has to be passed on before the tail comes for this to test
    # the carry rather than the wait.
    stream = PacedStream(
        [
            b'{"evt":"node_list","nodes":[319',
            (0.2, b""),
            (0.2, b""),
            (0.2, b""),
            (0.2, b""),
            b"8819345,1297448309]}\n",
            b'{"evt":"info","nodeId":7}\n',
        ]
    )
    cap = SerialCapture(lambda: stream)
    cap.PARTIAL_FRAME_MAX_WAIT = 0.3  # pass the head on before the tail comes
    cap.start()
    try:
        assert cap.next_event(timeout=4) == {"evt": "node_list", "nodes": [3198819345, 1297448309]}
        assert cap.next_event(timeout=2) == {"evt": "info", "nodeId": 7}
        raw = cap.raw_log
        assert '{"evt":"node_list","nodes":[319' in raw
        assert "8819345,1297448309]}" in raw
    finally:
        cap.stop()


def test_a_frame_head_is_held_longer_than_plain_noise():
    # Within the frame wait the two halves are one line, as they were sent:
    # 1.3 s of empty reads is past the plain-noise wait, not the frame's.
    stream = PacedStream(
        [
            b'{"evt":"node_list","nodes":[319',
            (0.65, b""),
            (0.65, b""),
            b"8819345]}\n",
        ]
    )
    cap = SerialCapture(lambda: stream).start()
    try:
        assert cap.next_event(timeout=5) == {"evt": "node_list", "nodes": [3198819345]}
        assert '{"evt":"node_list","nodes":[3198819345]}' in cap.raw_log
    finally:
        cap.stop()


def test_write_line_appends_newline():
    stream = FakeStream([])
    cap = SerialCapture(lambda: stream).start()
    try:
        cap.write_line('{"cmd":"info"}')
        assert stream.written == [b'{"cmd":"info"}\n']
    finally:
        cap.stop()


def test_write_line_reopens_and_retries_after_disconnected_fd():
    good = FakeStream([])

    class Disconnected(FakeStream):
        def write(self, data):
            raise OSError(5, "Input/output error")

    streams = iter((Disconnected([]), good))
    cap = SerialCapture(lambda: next(streams)).start()
    try:
        cap.write_line('{"cmd":"info"}')
        assert good.written == [b'{"cmd":"info"}\n']
    finally:
        cap.stop()


def test_long_write_is_paced_without_changing_protocol_frame():
    stream = FakeStream([])
    cap = SerialCapture(lambda: stream).start()
    try:
        command = '{"cmd":"send_single","msg":"' + ("x" * 600) + '"}'
        cap.write_line(command)
        assert len(stream.written) > 1
        assert all(len(chunk) <= 128 for chunk in stream.written)
        assert b"".join(stream.written) == (command + "\n").encode()
    finally:
        cap.stop()


def test_recovers_complete_events_after_truncated_or_joined_frames():
    stream = FakeStream(
        [
            b'{"evt":"info","nodeId":12{"evt":"info","nodeId":123}\n',
            b'{"evt":"connection","nodeId":7}{"evt":"recv","msg":"ok"}\n',
        ]
    )
    cap = SerialCapture(lambda: stream).start()
    try:
        assert cap.next_event(timeout=2) == {"evt": "info", "nodeId": 123}
        assert cap.next_event(timeout=2) == {"evt": "connection", "nodeId": 7}
        assert cap.next_event(timeout=2) == {"evt": "recv", "msg": "ok"}
    finally:
        cap.stop()


def test_mesh_log_events_stay_in_raw_log_but_never_reach_a_wait():
    stream = FakeStream(
        [
            b'{"evt":"mesh_log","level":"CONNECTION","line":"stationScan(): x"}\n',
            b'{"evt":"boot","nodeId":123}\n',
            b'{"evt":"mesh_log","level":"ERROR","line":"wifi scan failed"}\n',
        ]
    )
    cap = SerialCapture(lambda: stream).start()
    try:
        assert cap.next_event(timeout=2) == {"evt": "boot", "nodeId": 123}
        assert cap.next_event(timeout=0.2) is None
        raw = "\n".join(cap.raw_log)
        assert "stationScan(): x" in raw
        assert "wifi scan failed" in raw
    finally:
        cap.stop()


def test_a_huge_line_is_bounded_in_the_raw_log():
    """An ESP32's boot ROM prints at 74880 baud.

    Read at 115200 that is binary noise with no newline in it, so readline()
    accumulates until the firmware's first real output. One reset produced a
    74 KB "line": a serial log of 318 lines and 91 KB, unreadable in an editor
    or a CI artifact preview, and occupying one slot of a ring sized in lines
    rather than bytes.
    """
    from alteriom_hil.serial_capture import SerialCapture

    noise = "x" * 80_000
    bounded = SerialCapture._bounded(noise)

    assert len(bounded) < 3000, "the log must not carry the whole blob"
    assert bounded.startswith("x" * 100)
    # The signal survives: something unreadable arrived, which means the board
    # reset, and that is itself diagnostic.
    assert "not logged" in bounded


def test_an_ordinary_line_is_untouched():
    from alteriom_hil.serial_capture import SerialCapture

    line = '{"evt":"ack","node":123,"delivered":true}'
    assert SerialCapture._bounded(line) == line


def test_control_bytes_do_not_make_the_log_binary():
    """A single control byte makes grep call the whole file binary.

    It then prints "binary file matches" and none of the matching lines, so
    someone grepping a failed run for the value they care about gets that one
    line back and concludes the board never printed it. Exactly what happened
    reading a real two-board run: the boot ROM header is binary at 115200, and
    grepping the log for "Mesh Node Id:" returned nothing while the log held
    fifty-eight mesh lines.
    """
    noisy = "boot" + "".join(chr(b) for b in range(1, 32)) + "done"

    bounded = SerialCapture._bounded(noisy)

    assert "boot" in bounded and "done" in bounded
    assert not any(ord(c) < 32 and c != "	" for c in bounded)


def test_tabs_and_emoji_survive():
    """Only control characters go. The firmware logs emoji deliberately, and a
    tab is layout rather than noise."""
    line = "🕸️ MESH STATUS:	Mesh Node Id: 3711130777"

    assert SerialCapture._bounded(line) == line


def test_a_line_with_undecodable_bytes_and_no_event_is_framed_as_unreadable():
    # esp32-fde4's scan reply (farm run be46491e) arrived with its first 32
    # characters -- `{"evt":"wifi_scan","ms":6218,"co` -- as 64 bytes that
    # were not UTF-8, and the tail intact. The tail's inner object parses
    # but carries no "evt", so the line yielded no event and the check waited
    # 45 s for an answer that was in the raw log. The capture now says what
    # it saw, as its own event, so a command safe to repeat can act on it.
    tail = (
        b'unt":5,"ok":true,"networks":[{"ssid":"Alteriom-HIL","rssi":-41,'
        b'"channel":1}],"seen":true}'
    )
    stream = FakeStream([b"\xff" * 64 + tail + b"\n"])
    cap = SerialCapture(lambda: stream).start()
    try:
        evt = cap.next_event(timeout=2)
        assert evt == {
            "evt": "unreadable",
            "source": "rig",
            "undecodable": 64,
            "chars": 64 + len(tail),
        }
        assert cap.next_event(timeout=0.2) is None
        # The line itself stays in the raw log, U+FFFD and all: the evidence
        # that the board answered and what its answer was.
        assert any('"seen":true}' in line and "\ufffd" * 64 in line for line in cap.raw_log)
    finally:
        cap.stop()


def test_a_carried_frame_head_with_an_undecodable_byte_is_judged_with_its_tail():
    # A head passed on before its tail (the S3's stall, above) that carries
    # a byte that was not UTF-8 inside a string is a valid event once the
    # tail joins it. Calling it unreadable while it is still being carried
    # would have a repeatable command resent for a reply that completes a
    # moment later, so the judgement waits for the carry to be resolved.
    stream = PacedStream(
        [
            b'{"evt":"wifi_scan","ssid":"Alteri\xffom","networks":[',
            (0.2, b""),
            (0.2, b""),
            (0.2, b""),
            (0.2, b""),
            b'{"rssi":-41}],"seen":true}\n',
        ]
    )
    cap = SerialCapture(lambda: stream)
    cap.PARTIAL_FRAME_MAX_WAIT = 0.3  # pass the head on before the tail comes
    cap.start()
    try:
        evt = cap.next_event(timeout=4)
        assert evt["evt"] == "wifi_scan" and evt["seen"] is True
        assert evt["ssid"] == "Alteri\ufffdom"
        assert cap.next_event(timeout=0.5) is None, "no unreadable for a frame that completed"
    finally:
        cap.stop()


def test_a_carried_frame_head_whose_tail_never_completes_it_is_unreadable():
    # The same head, followed by a tail that does not finish the frame: now
    # nothing will, and what was seen -- head and tail together -- is said.
    stream = PacedStream(
        [
            b'{"evt":"wifi_scan","ssid":"Alteri\xffom"',
            (0.2, b""),
            (0.2, b""),
            (0.2, b""),
            (0.2, b""),
            b'ks":[],"seen":false\n',
        ]
    )
    cap = SerialCapture(lambda: stream)
    cap.PARTIAL_FRAME_MAX_WAIT = 0.3
    cap.start()
    try:
        evt = cap.next_event(timeout=4)
        assert evt["evt"] == "unreadable" and evt["undecodable"] == 1
        assert evt["chars"] == len('{"evt":"wifi_scan","ssid":"Alteri?om"' + 'ks":[],"seen":false')
        assert cap.next_event(timeout=0.5) is None
    finally:
        cap.stop()


def test_undecodable_bytes_beside_a_whole_event_are_not_unreadable():
    # The ESP32 classic prints one damaged byte at boot ("Incorrect.\ufffdxize
    # of core dump image") and its boot ROM banner is binary at this baud;
    # a line that still yields an event is that event, nothing more, and a
    # clean non-JSON line is nothing at all.
    stream = FakeStream(
        [
            b'\xff\xfe{"evt":"info","nodeId":7}\n',
            b"E (344) esp_core_dump_flash: Incorrect size of core dump image: 1\n",
            b'{"evt":"boot","nodeId":7}\n',
        ]
    )
    cap = SerialCapture(lambda: stream).start()
    try:
        assert cap.next_event(timeout=2) == {"evt": "info", "nodeId": 7}
        assert cap.next_event(timeout=2) == {"evt": "boot", "nodeId": 7}
        assert cap.next_event(timeout=0.2) is None
    finally:
        cap.stop()
