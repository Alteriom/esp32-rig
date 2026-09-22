"""End-to-end HAL test against the simulated hub.

These are the same interaction shapes the painlessMesh hardware suite
uses — if these pass, the orchestration layer works; the hardware run then
only tests the firmware/library itself.
"""

import pytest

from alteriom_hil.protocol import TimeoutWaitingFor
# The mesh verbs are painlessMesh's agent's, and live beside its suite.
from suites.painlessmesh.meshclient import MeshBoardClient as BoardClient
from alteriom_hil.serial_capture import SerialCapture
from alteriom_hil.sim import SimHub
from suites.painlessmesh.simmesh import MeshFirmware


@pytest.fixture()
def trio():
    hub = SimHub(3, firmware=MeshFirmware)
    caps, clients = [], []
    for b in hub.boards:
        cap = SerialCapture(b.open_host_stream).start()
        caps.append(cap)
        clients.append(BoardClient(f"sim-{b.node_id}", cap))
    yield clients
    for cap in caps:
        cap.stop()


def test_info_and_node_list(trio):
    a, b, c = trio
    ida, idb, idc = a.node_id(), b.node_id(), c.node_id()
    assert len({ida, idb, idc}) == 3
    assert sorted(a.node_list()) == sorted([idb, idc])


def test_send_single_with_ack_delivers(trio):
    a, b, _ = trio
    idb = b.node_id()
    assert a.send_single(idb, "hello", ack=True)
    recv = b.wait_recv(from_node=a.node_id())
    assert recv["msg"] == "hello"
    ack = a.wait_ack(node=idb)
    assert ack["delivered"] is True
    assert ack["latencyMs"] >= 0


def test_send_single_to_unknown_node_fails_fast(trio):
    a, _, _ = trio
    assert a.send_single(999999, "void", ack=True) is False
    with pytest.raises(TimeoutWaitingFor):
        a.wait_ack(timeout=0.5)


def test_stalled_node_causes_ack_timeout(trio):
    a, b, _ = trio
    idb = b.node_id()
    b.stall(2000)
    b.wait_for(lambda e: e["evt"] == "stalled", "stall confirmation")
    assert a.send_single(idb, "are-you-there", ack=True, ack_timeout_ms=500)
    ack = a.wait_ack(node=idb, timeout=5)
    assert ack["delivered"] is False


def test_broadcast_acks_once_per_node(trio):
    a, b, c = trio
    idb, idc = b.node_id(), c.node_id()
    assert a.send_broadcast("to-all", ack=True)
    acks = {a.wait_ack()["node"] for _ in range(2)}
    assert acks == {idb, idc}
    assert b.wait_recv(from_node=a.node_id())["msg"] == "to-all"
    assert c.wait_recv(from_node=a.node_id())["msg"] == "to-all"


def test_wait_for_retains_out_of_order_events(trio):
    a, b, _ = trio
    idb = b.node_id()
    # ack + recv arrive; consuming the ack first must not lose the recv
    assert a.send_single(idb, "m1", ack=True)
    a.wait_ack(node=idb)
    assert b.wait_recv(from_node=a.node_id())["msg"] == "m1"
