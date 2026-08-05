"""Delivery-confirmation API (painlessMesh issue #379 / PR #383) on real
hardware: sendSingle with ack callback, latency sanity, no-route
rejection, and real timeout via a stalled receiver."""

import pytest

from alteriom_hil.protocol import TimeoutWaitingFor

pytestmark = pytest.mark.hil_only(reason="radio_timing")


def test_send_single_with_ack_delivers(pair):
    sender, receiver, receiver_id = pair
    assert sender.send_single(receiver_id, "hil-ack-1", ack=True)
    recv = receiver.wait_recv(timeout=15)
    assert recv["msg"] == "hil-ack-1"
    ack = sender.wait_ack(node=receiver_id, timeout=15)
    assert ack["delivered"] is True
    # real-radio round trip: nonzero-ish and far below the 5 s default
    assert 0 <= ack["latencyMs"] < 5000


def test_send_single_without_ack_still_delivers(pair):
    sender, receiver, receiver_id = pair
    assert sender.send_single(receiver_id, "hil-plain-1", ack=False)
    recv = receiver.wait_recv(timeout=15)
    assert recv["msg"] == "hil-plain-1"
    with pytest.raises(TimeoutWaitingFor):
        sender.wait_ack(timeout=1.0)  # no callback requested -> no ack event


def test_send_to_unknown_node_rejected_without_callback(pair):
    sender, _, _ = pair
    assert sender.send_single(4041904190, "void", ack=True) is False
    with pytest.raises(TimeoutWaitingFor):
        sender.wait_ack(timeout=1.5)


def test_stalled_receiver_times_out_with_delivered_false(pair):
    sender, receiver, receiver_id = pair
    receiver.stall(4000)
    receiver.wait_for(
        lambda e: e["evt"] == "stalled", "stall confirmation", timeout=10
    )
    # receiver is still in the routing tables but not servicing update()
    assert sender.send_single(
        receiver_id, "hil-timeout-1", ack=True, ack_timeout_ms=1500
    )
    ack = sender.wait_ack(node=receiver_id, timeout=20)
    assert ack["delivered"] is False

    # Recovery: the node must answer again once the stall expires, and any
    # late-delivered buffered messages are drained so they cannot leak into
    # the next test. (On hardware the TCP-buffered message is processed
    # after the stall — its late auto-ACK is correctly ignored by the
    # sender's tracker, whose entry already timed out.)
    assert receiver.info(timeout=15)["nodeId"] == receiver_id
    receiver.clear_pending()
    sender.clear_pending()
