"""The events the farm sends to somebody else's software.

The shape and the signature are Alteriom's webhook connector's, so a consumer
already ingesting from that connector reads these without being taught a
second format. These tests are what "the same" means, held against the
connector's own code (src/dispatch/http-adapter.ts, src/webhooks/verification.ts).
"""

import hashlib
import hmac
import json

import pytest

from alteriom_hil import webhooks


def test_an_envelope_is_the_one_the_connector_sends():
    """Field for field, in that order: a receiver that verifies a signature
    over re-serialized JSON has to get the same bytes back, so key order and
    the absence of empty fields are part of the contract, not decoration."""
    event = webhooks.Event(
        "run", "failed", rig="rig02", sender="sparck",
        summary='Rig Health Check failed on rig02: 2 of 6 boards red',
        payload={"job_id": "a" * 32, "profile": "canary", "boards": ["esp32-01", "esp32-02"]},
        received_at="2026-09-16T12:00:00+00:00",
    )
    body = webhooks.envelope(event, "run-failed-x", dispatched_at="2026-09-16T12:00:01+00:00")
    assert list(body) == ["event", "action", "delivery_id", "rig", "sender",
                          "summary", "payload", "received_at", "dispatched_at"]
    assert body["event"] == "run" and body["action"] == "failed"
    # The base type on the wire, never the compound form -- `run.failed` is
    # what a subscription filters on.
    assert "." not in body["event"]
    assert event.name == "run.failed"

    # Nothing empty is sent as null: the connector's JSON.stringify drops it.
    plain = webhooks.Event("queue", "paused", summary="The queue is paused")
    body = webhooks.envelope(plain, "queue-paused-x")
    assert "action" in body and "rig" not in body and "sender" not in body
    assert body["payload"] == {}


def test_the_signature_is_the_connector_s_over_the_bytes_that_are_sent():
    """HMAC-SHA256 of the serialized body and nothing else -- not the
    timestamp, not the delivery id, not a concatenation -- lowercase hex with
    `sha256=` in front. The same function, written out here, so a change to
    ours has to be deliberate."""
    event = webhooks.Event("board", "red", rig="rig02", summary="esp32-01 went red")
    body = webhooks.serialize(webhooks.envelope(event, "board-red-x"))
    secret = "a-shared-secret"

    signature = webhooks.sign(body, secret)
    assert signature.startswith("sha256=")
    assert signature == "sha256=" + hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    assert signature.lower() == signature, "lowercase hex"
    assert webhooks.verify(body, secret, signature)
    # As a receiver would check it, with or without the prefix.
    assert webhooks.verify(body, secret, signature.removeprefix("sha256="))
    assert not webhooks.verify(body, "another-secret", signature)
    assert not webhooks.verify(body + b" ", secret, signature)
    assert not webhooks.verify(body, secret, "")


def test_the_bytes_signed_are_the_bytes_sent():
    """One call produces both, because a body serialized twice is a signature
    that verifies on our side and fails on theirs."""
    event = webhooks.Event("rig", "offline", rig="rig02", summary="rig02 has gone quiet")
    body = webhooks.envelope(event, "rig-offline-x")
    raw = webhooks.serialize(body)
    assert json.loads(raw) == body
    assert b", " not in raw and b'": ' not in raw, "compact, as JSON.stringify writes it"
    # And what a receiver re-serializing its parsed body would get.
    assert json.dumps(json.loads(raw), separators=(",", ":"), ensure_ascii=False).encode() == raw


def test_a_timestamp_inside_the_envelope_is_what_a_replay_is_caught_by():
    """The connector signs the body and sends no timestamp header, so a header
    we invented would not be covered by the signature and would prove nothing.
    `dispatched_at` is in the envelope, so it is signed: a receiver refusing
    anything stamped too long ago has something it can trust."""
    event = webhooks.Event("rig", "offline", rig="rig02", summary="rig02 has gone quiet")
    body = webhooks.envelope(event, "rig-offline-x", dispatched_at="2026-09-16T12:00:01+00:00")
    raw = webhooks.serialize(body)
    assert b"dispatched_at" in raw
    assert webhooks.verify(raw, "s", webhooks.sign(raw, "s"))
    # Changing the stamp breaks the signature, which is the point of it being
    # inside rather than beside.
    moved = webhooks.serialize({**body, "dispatched_at": "2026-09-16T13:00:01+00:00"})
    assert not webhooks.verify(moved, "s", webhooks.sign(raw, "s"))


def test_the_headers_carry_the_signature_under_both_names_receivers_know():
    event = webhooks.Event("run", "passed", rig="rig02", summary="Rig Health Check passed")
    sent = webhooks.headers(event, "run-passed-x", "sha256=abc")
    assert sent["Content-Type"] == "application/json"
    assert sent["X-Webhook-Event"] == "run", "the base type, as the connector sends it"
    assert sent["X-Webhook-Delivery"] == "run-passed-x"
    assert sent["User-Agent"] == "Alteriom-ESP32-Farm/1.0"
    # The connector produces X-Connector-Signature-256; most receivers, and
    # Command Center, read X-Hub-Signature-256. The same signature under both,
    # so neither has to be taught about us.
    assert sent["X-Connector-Signature-256"] == "sha256=abc"
    assert sent["X-Hub-Signature-256"] == "sha256=abc"
    assert sent["X-Webhook-Action"] == "passed" and sent["X-Webhook-Rig"] == "rig02"


def test_a_delivery_id_is_stable_for_one_delivery_and_says_what_it_was():
    """A retry is the same delivery: the receiver deduplicates on this, and an
    id made afresh per attempt would be a second occurrence to it."""
    event = webhooks.Event("run", "failed", rig="rig02", summary="a run failed")
    first = webhooks.delivery_id(event)
    assert first.startswith("run-failed-")
    assert first != webhooks.delivery_id(event), "a different delivery is a different id"
    # It is passed in, not made again, so every attempt of one delivery agrees.
    one = webhooks.envelope(event, first)
    again = webhooks.envelope(event, first)
    assert one["delivery_id"] == again["delivery_id"] == first


def test_a_subscription_asks_for_a_type_an_action_or_everything():
    assert webhooks.wants(["*"], "run", "failed")
    assert webhooks.wants(["run"], "run", "failed"), "the base type is all of its actions"
    assert webhooks.wants(["run.failed"], "run", "failed")
    assert not webhooks.wants(["run.failed"], "run", "passed")
    assert not webhooks.wants(["board"], "run", "failed")
    # Asking for nothing means nothing, rather than quietly meaning everything.
    assert not webhooks.wants([], "run", "failed")

    assert webhooks.valid_event("*") and webhooks.valid_event("run") and webhooks.valid_event("run.failed")
    assert not webhooks.valid_event("run.exploded")
    assert not webhooks.valid_event("teleportation")
    # Every name that can be asked for is one the farm can send.
    for name in webhooks.every_event():
        assert webhooks.valid_event(name), name
    rig_names = webhooks.every_event(webhooks.RIG_EVENTS)
    assert "queue.paused" not in rig_names, "the queue is the portal's, not a rig's"
    assert "run.failed" in rig_names and "board.red" in rig_names


def test_an_event_says_what_it_is_and_carries_a_line_a_person_can_read():
    """The connector's summary is required, and it is what a person sees in
    whatever the receiver does with this."""
    with pytest.raises(ValueError, match="summary"):
        webhooks.Event("run", "failed", summary="")
    with pytest.raises(ValueError, match="event must be one of"):
        webhooks.Event("explosion", summary="the rig is on fire")
    with pytest.raises(ValueError, match="does not do"):
        webhooks.Event("run", "exploded", summary="a run exploded")


def test_a_secret_is_made_when_one_is_not_brought():
    first, second = webhooks.new_secret(), webhooks.new_secret()
    assert first != second
    assert len(first) == 64 and int(first, 16) >= 0, "32 bytes as hex, like the connector's"


def test_a_delivery_is_tried_again_and_every_attempt_carries_the_same_bytes():
    """A retry is the same delivery: the envelope, the signature and the id are
    built once. A receiver deduplicating on the id sees one event however many
    times the network made us ask."""
    event = webhooks.Event("run", "failed", rig="rig02", summary="a run failed")
    sub = webhooks.Subscription("s1", "farm", "https://ingest.example.invalid/hook",
                                "a-secret", backoff_ms=1)
    seen, waited, recorded = [], [], []

    def flaky(url, body, sent, timeout_ms):
        seen.append((url, body, dict(sent)))
        if len(seen) < 3:
            raise webhooks.DeliveryError("HTTP 502")
        return 200

    outcome = webhooks.deliver(sub, event, "run-failed-x", send=flaky,
                               record=lambda s, e, o: recorded.append(o["state"]),
                               sleep=waited.append)
    assert outcome["ok"] and outcome["attempt"] == 3 and outcome["status"] == 200
    assert {body for _, body, _ in seen} == {seen[0][1]}, "the same bytes every time"
    assert {sent["X-Webhook-Delivery"] for _, _, sent in seen} == {"run-failed-x"}
    assert {sent["X-Hub-Signature-256"] for _, _, sent in seen} == {seen[0][2]["X-Hub-Signature-256"]}
    assert webhooks.verify(seen[0][1], "a-secret", seen[0][2]["X-Connector-Signature-256"])
    # Backing off, doubling, and every attempt on the record.
    assert waited == [0.001, 0.002]
    assert recorded == ["failed", "failed", "success"]


def test_a_delivery_that_never_arrives_is_given_up_on_and_says_so():
    event = webhooks.Event("board", "red", rig="rig02", summary="esp32-01 went red")
    sub = webhooks.Subscription("s1", "farm", "https://ingest.example.invalid/hook",
                                "a-secret", max_retries=3, backoff_ms=1)
    states = []

    def refuse(url, body, sent, timeout_ms):
        raise webhooks.DeliveryError("HTTP 500: it is not having it")

    outcome = webhooks.deliver(sub, event, "board-red-x", send=refuse,
                               record=lambda s, e, o: states.append(o["state"]), sleep=lambda _: None)
    assert not outcome["ok"] and outcome["attempt"] == 3
    assert "HTTP 500" in outcome["error"]
    # The connector's word for the last attempt: a reader can tell a delivery
    # that is failing from one nobody will try again.
    assert states == ["failed", "failed", "dead"]

    # Anything thrown at all is a failed delivery, never the farm's problem.
    def explode(url, body, sent, timeout_ms):
        raise RuntimeError("something else entirely")

    outcome = webhooks.deliver(sub, event, "board-red-y", send=explode, sleep=lambda _: None)
    assert not outcome["ok"] and "something else entirely" in outcome["error"]


def test_a_subscription_takes_what_its_scope_and_its_list_cover():
    """A rig's own subscription gets that rig's events and no other rig's; the
    farm's gets everything. Off is off."""
    farm = webhooks.Subscription("f", "farm", "https://x.invalid", "s")
    mine = webhooks.Subscription("m", "rig02", "https://x.invalid", "s", events=("run", "board.red"))
    off = webhooks.Subscription("o", "farm", "https://x.invalid", "s", active=False)

    ours = webhooks.Event("run", "failed", rig="rig02", summary="a run failed")
    theirs = webhooks.Event("run", "failed", rig="rig07", summary="a run failed")
    fleet = webhooks.Event("queue", "paused", summary="the queue is paused")

    assert farm.takes(ours) and farm.takes(theirs) and farm.takes(fleet)
    assert mine.takes(ours)
    assert not mine.takes(theirs), "another rig's run is not this rig's business"
    assert not mine.takes(fleet), "and the fleet's queue is not either"
    assert not mine.takes(webhooks.Event("board", "missing", rig="rig02", summary="gone")), \
        "it asked for board.red, not every board event"
    assert not off.takes(ours)


def test_a_redirect_is_a_failed_delivery_and_never_followed():
    """`urlopen` follows 301, 302 and 303 by itself and turns the POST into a
    GET doing it: the body is dropped, the far end answers 2xx to something
    that carried nothing, and the delivery is recorded as having arrived. It
    would follow https to http too, which is what the https-only rule exists
    to prevent. Against a real server, because this is the standard library's
    behaviour and not ours -- a mock would prove nothing."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(("POST", self.path))
            self.send_response(302)
            self.send_header("Location", "/somewhere-else")
            self.end_headers()

        def do_GET(self):
            # Where a followed redirect would land: a 200 to a request
            # carrying none of the event.
            seen.append(("GET", self.path))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/hook"
        with pytest.raises(webhooks.DeliveryError) as caught:
            webhooks.post(url, b'{"event":"run"}', {"Content-Type": "application/json"}, 5000)
        assert "302" in str(caught.value)
        assert seen == [("POST", "/hook")], "the redirect was not followed"

        # And a delivery over it is a failed attempt that is tried again,
        # rather than a success nobody received.
        sub = webhooks.Subscription("s", "farm", url, "a-secret", max_retries=2, backoff_ms=1)
        event = webhooks.Event("run", "failed", rig="rig02", summary="a run failed")
        outcome = webhooks.deliver(sub, event, "run-failed-x", sleep=lambda _: None)
        assert not outcome["ok"] and "302" in outcome["error"]
        assert [method for method, _ in seen] == ["POST", "POST", "POST"]
    finally:
        server.shutdown()
