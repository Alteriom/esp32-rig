import queue
import threading
import time

import pytest

from alteriom_hil.protocol import TimeoutWaitingFor
# The mesh verbs are painlessMesh's agent's, and live beside its suite.
from suites.painlessmesh.meshclient import MeshBoardClient as BoardClient
from alteriom_hil.serial_capture import SerialCapture


class FakeStream:
    def __init__(self):
        self._q = queue.Queue()
        self.written = []

    def feed(self, line: str):
        self._q.put(line.encode() + b"\n")

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


def test_clear_pending_forgets_unread_events_as_well_as_held_ones():
    # The broadcast test retries after a failed attempt. Its previous
    # attempt's delivered=false ack lands at the 5 s timeout, after the test
    # has given up and slept, so it is sitting unread in the capture queue —
    # not in the client's held-back list — when the next attempt starts.
    # clear_pending() only emptied the held-back list, and the retry's
    # wait_ack() consumed the stale false ack as its own answer.
    stream = FakeStream()
    cap = SerialCapture(lambda: stream).start()
    client = BoardClient("esp8266-7d75", cap)
    try:
        stream.feed('{"evt":"ack","node":3711130777,"delivered":false,"latencyMs":5000}')
        # Wait for the reader to have queued it -- unread -- rather than
        # guessing at a sleep: on a loaded runner a fixed wait let the stale
        # ack land *after* clear_pending() and be taken for the retry's answer,
        # which is this test failing for the very reason it exists.
        deadline = time.monotonic() + 5
        while cap._events.qsize() < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert cap._events.qsize() == 1, "the stale ack is queued and unread"
        client.clear_pending()
        stream.feed('{"evt":"ack","node":3711130777,"delivered":true,"latencyMs":1846}')
        ack = client.wait_for(lambda e: e["evt"] == "ack", "ack", timeout=2)
        assert ack["delivered"] is True
    finally:
        cap.stop()


def test_waiting_for_a_named_peer_is_not_the_same_as_waiting_for_a_count():
    """`wait_mesh_size` is satisfied by any N peers.

    A board can pass it while the one node the test is about to send to is
    the one that left -- which is how a 16 s mesh outage on esp32-c3-02
    reached a delivery assertion and was reported as "timed out waiting for
    recv event" (farm job 981450ca). The sender had already said
    delivered=False, correctly. `wait_for_peer` asks the question the test
    actually depends on, and its failure names the peer.
    """
    stream = FakeStream()
    cap = SerialCapture(lambda: stream).start()
    client = BoardClient("esp32-s3-01", cap)
    try:
        # Three peers, none of them the one asked for: a count check passes
        # here and this must not.
        present = [3198819345, 3711130777, 139984357]
        for _ in range(40):
            stream.feed('{"evt":"node_list","nodes":[3198819345,3711130777,139984357]}')
        assert client.wait_mesh_size(3, timeout=5) == present
        with pytest.raises(TimeoutWaitingFor) as refused:
            client.wait_for_peer(2101688781, timeout=1.0)
        message = str(refused.value)
        assert "node 2101688781 to be a peer" in message, "the missing peer is named"
        assert "3198819345" in message, "and what it did see, for the diagnosis"
        assert "[esp32-s3-01]" in message, "on which board"
        assert refused.value.board_id == "esp32-s3-01", "the recovery hook reads this"
    finally:
        cap.stop()


def test_a_peer_that_appears_while_polling_is_waited_for_not_refused():
    """A poll landing mid-sync sees a list without the peer. That is not a
    board leaving the mesh, and a fixture that treated it as one would make
    every delivery test flaky in the other direction."""
    stream = FakeStream()
    cap = SerialCapture(lambda: stream).start()
    client = BoardClient("esp32-s3-01", cap)
    try:
        stream.feed('{"evt":"node_list","nodes":[3198819345]}')
        stream.feed('{"evt":"node_list","nodes":[3198819345]}')
        stream.feed('{"evt":"node_list","nodes":[3198819345,2101688781]}')
        assert 2101688781 in client.wait_for_peer(2101688781, timeout=5.0)
    finally:
        cap.stop()

def test_a_poll_the_board_is_too_busy_to_answer_does_not_spend_the_window():
    """Farm run 34900496756: an ESP32-C3 in painlessMesh's blocking channel
    scan answered its node_list seven seconds late, listing every peer. The
    pair fixture's 3 s wait reported "saw no peers" and failed the row."""
    stream = FakeStream()
    cap = SerialCapture(lambda: stream).start()
    client = BoardClient("esp32-c3-02", cap)
    try:
        timer = threading.Timer(
            4.0, stream.feed, args=('{"evt":"node_list","nodes":[2098834584,3198819345]}',)
        )
        timer.start()
        started = time.monotonic()
        assert 2098834584 in client.wait_for_peer(2098834584, timeout=3.0)
        assert time.monotonic() - started >= 3.5, "the late reply is the one judged"
    finally:
        cap.stop()


def test_a_board_that_never_answers_fails_as_unresponsive_not_as_peerless(monkeypatch):
    stream = FakeStream()
    cap = SerialCapture(lambda: stream).start()
    client = BoardClient("esp32-c3-02", cap)
    monkeypatch.setattr(BoardClient, "PEER_POLL_REPLY_TIMEOUT", 0.5)
    try:
        with pytest.raises(TimeoutWaitingFor) as refused:
            client.wait_for_peer(2098834584, timeout=3.0)
        message = str(refused.value)
        assert "did not answer node_list" in message
        assert "saw no peers" not in message
    finally:
        cap.stop()


class ResettableStream(FakeStream):
    """A stream with modem lines, like the CP210x/CH340/JTAG ports on the rig."""

    def __init__(self, boot_on_reset=True):
        super().__init__()
        self.rts = False
        self.dtr = False
        self.resets = 0
        self._boot_on_reset = boot_on_reset

    def __setattr__(self, name, value):
        # A reset is the RTS rising edge while DTR is low, as esptool drives it.
        if name == "rts" and value and getattr(self, "dtr", True) is False:
            object.__setattr__(self, name, value)
            self.resets += 1
            hook = getattr(self, "on_reset", None)
            if hook is not None:
                hook()
            elif self._boot_on_reset:
                self.feed('{"evt":"boot","nodeId":1297448309,"target":"esp8266"}')
                self.feed('{"evt":"info","nodeId":1297448309,"target":"esp8266"}')
            return
        object.__setattr__(self, name, value)


def test_a_wedged_board_is_reset_and_comes_back():
    # The ESP8266 hard-hung mid-suite: port open, nothing printed, no reply,
    # and no watchdog. It stayed that way into every later run. One RTS/DTR
    # pulse — the sequence esptool uses — brought it straight back.
    stream = ResettableStream()
    cap = SerialCapture(lambda: stream).start()
    client = BoardClient("esp8266-7d75", cap)
    try:
        info = client.ensure_responsive(timeout=1.0)
        assert stream.resets == 1, "a silent board must be reset exactly once"
        assert info["target"] == "esp8266"
    finally:
        cap.stop()


def test_a_board_that_answers_is_never_reset():
    stream = ResettableStream()
    cap = SerialCapture(lambda: stream).start()
    client = BoardClient("esp32-02", cap)
    try:
        stream.feed('{"evt":"info","nodeId":139984357,"target":"esp32"}')
        assert client.ensure_responsive(timeout=2.0)["target"] == "esp32"
        assert stream.resets == 0
    finally:
        cap.stop()


def test_a_board_that_stays_dead_after_a_reset_is_reported_not_hidden():
    from alteriom_hil.protocol import TimeoutWaitingFor

    stream = ResettableStream(boot_on_reset=False)
    cap = SerialCapture(lambda: stream).start()
    client = BoardClient("esp32-c5-1c10", cap)
    try:
        try:
            client.ensure_responsive(timeout=0.5)
        except TimeoutWaitingFor as exc:
            assert "esp32-c5-1c10" in str(exc)
        else:
            raise AssertionError("a dead board must not be reported as healthy")
        assert stream.resets == 1
    finally:
        cap.stop()


def test_reset_reports_failure_on_a_stream_with_no_modem_lines():
    # Sim mode and the unit tests run over an in-memory pipe. Claiming to have
    # reset one would be worse than saying it cannot.
    cap = SerialCapture(lambda: FakeStream()).start()
    try:
        assert cap.pulse_reset() is False
    finally:
        cap.stop()


def test_a_slow_booting_board_gets_the_post_reset_budget_not_the_pre_check_one():
    # Reset to `boot` is ~1 s on every family, but reset to a first `info`
    # reply is 15 s on the ESP32-C5. Giving the recovered board only the
    # pre-check budget would reset it and then call it dead.
    import threading

    stream = ResettableStream(boot_on_reset=False)

    def on_reset():
        stream.feed('{"evt":"boot","nodeId":3198819345,"target":"esp32-c5"}')
        threading.Timer(
            1.5,
            lambda: stream.feed(
                '{"evt":"info","nodeId":3198819345,"target":"esp32-c5"}'
            ),
        ).start()

    stream.on_reset = on_reset
    cap = SerialCapture(lambda: stream).start()
    client = BoardClient("esp32-c5-1c10", cap)
    try:
        info = client.ensure_responsive(timeout=0.5, after_reset_timeout=20.0)
        assert info["target"] == "esp32-c5"
        assert stream.resets == 1
    finally:
        cap.stop()


def test_a_test_failure_on_a_still_healthy_board_causes_no_reset():
    # A test can time out waiting for a mesh event for reasons that say
    # nothing about the board — a peer never forwarded, an election never
    # ran. Resetting then would turn one honest failure into several
    # invented ones in the tests that follow.
    from alteriom_hil.pytest_plugin import _recover_if_wedged

    stream = ResettableStream()
    cap = SerialCapture(lambda: stream).start()
    client = BoardClient("esp32-02", cap)
    try:
        stream.feed('{"evt":"info","nodeId":139984357,"target":"esp32"}')
        assert _recover_if_wedged({"esp32-02": client}, "esp32-02") is None
        assert stream.resets == 0
    finally:
        cap.stop()


def test_a_board_that_hung_mid_suite_is_reset_so_it_does_not_cascade():
    from alteriom_hil.pytest_plugin import _recover_if_wedged

    stream = ResettableStream()
    cap = SerialCapture(lambda: stream).start()
    client = BoardClient("esp8266-7d75", cap)
    try:
        note = _recover_if_wedged({"esp8266-7d75": client}, "esp8266-7d75", probe_timeout=0.5)
        assert stream.resets == 1
        assert note and "was reset" in note and "esp8266-7d75" in note
    finally:
        cap.stop()


def test_a_board_that_will_not_come_back_is_reported_not_silently_skipped():
    from alteriom_hil.pytest_plugin import _recover_if_wedged

    stream = ResettableStream(boot_on_reset=False)
    cap = SerialCapture(lambda: stream).start()
    client = BoardClient("esp32-c5-1c10", cap)
    try:
        note = _recover_if_wedged({"esp32-c5-1c10": client}, "esp32-c5-1c10", probe_timeout=0.5)
        assert note and "did not come back" in note
    finally:
        cap.stop()


def test_timeout_carries_the_board_id_for_the_recovery_hook():
    from alteriom_hil.protocol import TimeoutWaitingFor

    exc = TimeoutWaitingFor("info reply", "esp32-c6-14b4", ["noise"])
    assert exc.board_id == "esp32-c6-14b4"
    assert exc.description == "info reply"


def test_board_health_records_the_heap_trend_and_names_a_board_that_stopped():
    # The ESP8266's free heap fell from 18.5 K to 8 K across a suite and it
    # was the board whose deliveries then went unacknowledged — visible only
    # by grepping serial logs afterwards. It belongs in the run's artifacts.
    from alteriom_hil.pytest_plugin import _board_health

    healthy = ResettableStream()
    dead = ResettableStream(boot_on_reset=False)
    cap_ok = SerialCapture(lambda: healthy).start()
    cap_dead = SerialCapture(lambda: dead).start()
    try:
        ok = BoardClient("esp8266-7d75", cap_ok)
        gone = BoardClient("esp32-c5-1c10", cap_dead)
        healthy.feed(
            '{"evt":"info","nodeId":1297448309,"target":"esp8266",'
            '"freeHeap":8152,"bootId":7}'
        )
        health = _board_health(
            {"esp8266-7d75": ok, "esp32-c5-1c10": gone},
            {
                "esp8266-7d75": {
                    "target": "esp8266",
                    "freeHeap": 18496,
                    "bootId": 7,
                },
                "esp32-c5-1c10": {
                    "target": "esp32-c5",
                    "freeHeap": 180000,
                    "bootId": 3,
                },
            },
        )
        heap = health["esp8266-7d75"]
        assert heap["free_heap_start"] == 18496 and heap["free_heap_end"] == 8152
        assert heap["free_heap_delta_percent"] == -55.9
        assert heap["rebooted_during_suite"] is False
        assert heap["responsive_at_end"] is True
        # A board that stopped answering is the most important row, so it is
        # recorded as unresponsive rather than left out of the file.
        assert health["esp32-c5-1c10"]["responsive_at_end"] is False
        assert health["esp32-c5-1c10"]["free_heap_start"] == 180000
    finally:
        cap_ok.stop()
        cap_dead.stop()


def test_a_command_the_board_could_not_parse_is_resent():
    # The ESP8266 answered a role-change command with
    # {"evt":"error","error":"bad json"} — the frame arrived corrupted, so
    # the command never ran and the whole module's fixture failed with the
    # board perfectly healthy. That error is a precise statement that
    # nothing happened, which makes a resend safe.
    stream = ResettableStream()
    cap = SerialCapture(lambda: stream).start()
    client = BoardClient("esp8266-7d75", cap)
    try:
        sent_before = len(stream.written)
        import threading

        # Both arrive after the send: send_cmd_awaiting clears the queue
        # first, exactly so it cannot mistake an older event for its reply.
        threading.Timer(
            0.2, lambda: stream.feed('{"evt":"error","error":"bad json"}')
        ).start()
        threading.Timer(
            0.8, lambda: stream.feed('{"evt":"mesh_restarting","meshPrefix":"m"}')
        ).start()
        evt = client.send_cmd_awaiting(
            "mesh_configure",
            lambda e: e["evt"] == "mesh_restarting",
            "isolated mesh restart acknowledgement",
            5.0,
            prefix="m",
            password="hil-not-secret",
        )
        assert evt["meshPrefix"] == "m"
        # One initial send plus one resend after the parse failure.
        assert len(stream.written) - sent_before == 2
    finally:
        cap.stop()


def test_a_command_that_ran_but_whose_reply_was_lost_is_never_resent():
    # Resending a role change that already rebooted the board would be a
    # worse guess than failing, so only a reported parse failure retries.
    stream = ResettableStream()
    cap = SerialCapture(lambda: stream).start()
    client = BoardClient("esp32-02", cap)
    try:
        before = len(stream.written)
        try:
            client.send_cmd_awaiting(
                "gateway_start",
                lambda e: e["evt"] == "gateway_restarting",
                "gateway restart acknowledgement",
                1.0,
                ssid="x",
                password="y",
            )
        except TimeoutWaitingFor as exc:
            assert exc.board_id == "esp32-02"
        else:
            raise AssertionError("a missing reply must not be treated as success")
        assert len(stream.written) - before == 1, "sent exactly once"
    finally:
        cap.stop()


def test_heap_is_sampled_per_board_after_each_test_and_never_fails_it():
    # board-health.json gives the suite's two endpoints; the ESP8266 loses
    # half its heap between them and the endpoints cannot say which tests
    # take it. A board that does not answer is recorded as None, never as
    # an error that fails the test being reported.
    from alteriom_hil.pytest_plugin import _sample_board_heap

    ok, dead = ResettableStream(), ResettableStream(boot_on_reset=False)
    cap_ok, cap_dead = SerialCapture(lambda: ok).start(), SerialCapture(lambda: dead).start()
    try:
        ok.feed('{"evt":"info","nodeId":1297448309,"target":"esp8266","freeHeap":8120}')
        sample = _sample_board_heap(
            {"esp8266-7d75": BoardClient("esp8266-7d75", cap_ok),
             "esp32-c5-1c10": BoardClient("esp32-c5-1c10", cap_dead)},
            timeout=0.5,
        )
        assert sample == {"esp8266-7d75": 8120, "esp32-c5-1c10": None}
        assert dead.resets == 0, "a sample must never reset a board"
    finally:
        cap_ok.stop(); cap_dead.stop()


def test_a_swallowed_frame_reported_by_the_agent_is_resent():
    # A frame that loses its tail, newline included, used to sit in the
    # agent's line buffer with nothing said. The agent now reports
    # "frame dropped: incomplete" half a second later; that is as precise a
    # statement that the command did not run as "bad json" is.
    import threading

    stream = ResettableStream()
    cap = SerialCapture(lambda: stream).start()
    client = BoardClient("esp8266-7d75", cap)
    try:
        before = len(stream.written)
        threading.Timer(
            0.2, lambda: stream.feed('{"evt":"error","error":"frame dropped: incomplete"}')
        ).start()
        threading.Timer(
            0.8,
            lambda: stream.feed(
                '{"evt":"shared_gateway_restarting","ssidLength":12,"passwordLength":32}'
            ),
        ).start()
        evt = client.send_cmd_awaiting(
            "shared_gateway_start",
            lambda e: e["evt"] == "shared_gateway_restarting",
            "shared gateway restart acknowledgement",
            5.0,
            ssid="Alteriom-HIL", password="x" * 32,
        )
        assert evt["passwordLength"] == 32
        assert len(stream.written) - before == 2
    finally:
        cap.stop()


def test_a_module_fixture_failure_after_passes_does_not_abort_the_session(monkeypatch):
    # One board losing one role-change command failed the shared-gateway
    # module's fixture, and the session stopped: the soak never ran in a
    # suite that already had 21 passes. Only a setup failure before
    # anything has passed — the bank itself — should stop everything.
    from types import SimpleNamespace

    from alteriom_hil import pytest_plugin as plugin

    monkeypatch.setenv("ALTERIOM_HIL_MODE", "hardware")
    monkeypatch.delenv("ALTERIOM_HIL_RUN_LOG", raising=False)
    plugin._SESSION_HAS_PASSED_A_TEST.clear()

    def run(when, passed):
        session = SimpleNamespace(shouldstop=False)
        item = SimpleNamespace(session=session, nodeid="suites/x/test_y.py::t")
        report = SimpleNamespace(when=when, passed=passed, failed=not passed,
                                 skipped=False, sections=[])
        outcome = SimpleNamespace(get_result=lambda: report)
        gen = plugin.pytest_runtest_makereport(item, SimpleNamespace(excinfo=None))
        next(gen)
        try:
            gen.send(outcome)
        except StopIteration:
            pass
        return session.shouldstop

    assert run("setup", passed=False), "bank failure before any pass must stop"
    plugin._SESSION_HAS_PASSED_A_TEST.clear()
    assert run("call", passed=True) is False
    assert run("setup", passed=False) is False, "a module fixture failing later must not"
    plugin._SESSION_HAS_PASSED_A_TEST.clear()
