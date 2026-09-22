import json
import queue

from alteriom_hil.protocol import TimeoutWaitingFor
# The mesh verbs are painlessMesh's agent's, and live beside its suite.
from suites.painlessmesh.meshclient import MeshBoardClient as BoardClient


class FakeCapture:
    def __init__(self, events):
        self.events = queue.Queue()
        for event in events:
            self.events.put(event)
        self.writes = []
        self.raw_log = []

    def write_line(self, value):
        self.writes.append(json.loads(value))

    def next_event(self, timeout):
        try:
            return self.events.get_nowait()
        except queue.Empty:
            return None


def test_ack_recovers_a_damaged_send_result_frame():
    capture = FakeCapture(
        [{"evt": "ack", "node": 42, "delivered": False, "latencyMs": 1500}]
    )
    client = BoardClient("sender", capture)
    assert client.send_single(42, "payload", ack=True)
    assert client.wait_ack(42)["delivered"] is False


def test_gateway_status_retries_an_idempotent_query_after_damaged_frame():
    capture = FakeCapture([])
    client = BoardClient("gateway", capture)
    replies = iter(
        (
            TimeoutWaitingFor("gateway status", "gateway", ["damaged"]),
            {"evt": "gateway_status", "hasInternet": True},
        )
    )

    def wait_for(*_args, **_kwargs):
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    client.wait_for = wait_for
    assert client.gateway_status()["hasInternet"] is True
    assert [write["cmd"] for write in capture.writes] == [
        "gateway_status",
        "gateway_status",
    ]


def test_gateway_status_is_never_answered_by_an_earlier_reply():
    """Farm job 60e82d9c: a slow board answered both sends of one query, the
    spare reply was held back, and the next query returned that old state --
    Internet through a bridge the sender had already forgotten."""
    capture = FakeCapture([])
    client = BoardClient("sender", capture)
    # A late reply to an earlier query, one held back and one still unread.
    client._pending.append({"evt": "gateway_status", "hasInternet": True, "stale": 1})
    capture.events.put({"evt": "gateway_status", "hasInternet": True, "stale": 2})
    # Other events are kept, in order.
    capture.events.put({"evt": "recv", "from": 7, "msg": "keep"})

    original_send = client.send_cmd

    def send_and_answer(cmd, **kwargs):
        original_send(cmd, **kwargs)
        capture.events.put({"evt": "gateway_status", "hasInternet": False, "fresh": True})

    client.send_cmd = send_and_answer
    state = client.gateway_status(timeout=2)
    assert state.get("fresh") is True and state["hasInternet"] is False
    assert client.wait_for(lambda e: e["evt"] == "recv", "recv", timeout=1)["msg"] == "keep"
