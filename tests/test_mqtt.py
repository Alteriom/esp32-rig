"""The queue helper: what a suite reads a gateway's MQTT traffic through.

No broker here. The capture is fed through ``inject`` (what a sim double
does) or through a fake client that delivers messages the way paho would,
so the contract -- collect, match, wait, keep the evidence -- is checked
without a network.
"""

from __future__ import annotations

import json
import threading

import pytest

from alteriom_hil.mqtt import Message, QueueCapture, QueueTimeout, topic_matches


@pytest.mark.parametrize(
    "filter_, topic, expected",
    [
        ("#", "anything/at/all", True),
        ("alteriom/#", "alteriom/gateways/abc/status", True),
        ("alteriom/#", "mesh/status/nodes", False),
        ("alteriom/gateways/+/status", "alteriom/gateways/abc/status", True),
        ("alteriom/gateways/+/status", "alteriom/gateways/abc/info", False),
        ("alteriom/gateways/+/status", "alteriom/gateways/abc/def/status", False),
        ("alteriom/nodes/+/info", "alteriom/nodes/381621429/info", True),
        ("exact/topic", "exact/topic", True),
        ("exact/topic", "exact/topic/more", False),
    ],
)
def test_topic_matching_follows_mqtt_wildcards(filter_, topic, expected):
    assert topic_matches(filter_, topic) is expected


def test_injected_messages_are_read_back_by_filter(tmp_path):
    capture = QueueCapture(url="mqtt://10.42.0.1:1883", log_path=tmp_path / "mqtt" / "queue.jsonl")
    capture.inject("alteriom/gateways/G1/status", {"free_heap": 123456, "uptime_s": 7}, retain=True)
    capture.inject("alteriom/nodes/42/info", {"node_id": "42"})
    capture.inject("mesh/status/nodes", {"node_count": 2})

    assert [m.topic for m in capture.messages("alteriom/#")] == [
        "alteriom/gateways/G1/status",
        "alteriom/nodes/42/info",
    ]
    status = capture.wait_for("alteriom/gateways/+/status", timeout=0.1)
    assert status.retain is True
    assert status.json()["free_heap"] == 123456


def test_the_capture_is_evidence_on_disk_as_it_arrives(tmp_path):
    """Written line by line, not at teardown: a suite that hangs still
    leaves what was received, which is when the file gets read."""
    log = tmp_path / "mqtt" / "queue.jsonl"
    capture = QueueCapture(url="mqtt://10.42.0.1:1883", log_path=log)
    capture.inject("alteriom/gateways/G1/data", {"sensor": "bme280"})
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["topic"] == "alteriom/gateways/G1/data"
    assert json.loads(row["payload"]) == {"sensor": "bme280"}
    assert "ts" in row


def test_waiting_names_what_did_arrive_when_nothing_matched():
    capture = QueueCapture(url="mqtt://10.42.0.1:1883")
    capture.inject("alteriom/gateways/G1/status", "{}")
    with pytest.raises(QueueTimeout, match="alteriom/gateways/G1/status"):
        capture.wait_for("alteriom/gateways/+/data", timeout=0.2, description="a data message")


def test_wait_for_returns_a_message_that_arrives_later():
    capture = QueueCapture(url="mqtt://10.42.0.1:1883")
    threading.Timer(0.2, capture.inject, args=("alteriom/nodes/7/info", {"node_id": "7"})).start()
    message = capture.wait_for("alteriom/nodes/+/info", predicate=lambda m: m.json()["node_id"] == "7", timeout=3)
    assert message.topic == "alteriom/nodes/7/info"


def test_from_env_is_none_without_a_broker(monkeypatch, tmp_path):
    """A host without a broker exports no URL; the suite then skips its
    queue scenario rather than connecting to nothing."""
    monkeypatch.delenv("ALTERIOM_HIL_MQTT_URL", raising=False)
    assert QueueCapture.from_env(tmp_path) is None

    monkeypatch.setenv("ALTERIOM_HIL_MQTT_URL", "mqtt://10.42.0.1:1883")
    capture = QueueCapture.from_env(tmp_path, name="run")
    assert capture is not None
    assert capture.url == "mqtt://10.42.0.1:1883"
    assert capture.log_path == tmp_path / "mqtt" / "run.jsonl"


class _FakeMessage:
    def __init__(self, topic, payload, retain=False, qos=0):
        self.topic, self.payload, self.retain, self.qos = topic, payload, retain, qos


class _FakeClient:
    """Delivers CONNACK and messages the way paho's loop thread would."""

    def __init__(self):
        self.subscriptions = []
        self.published = []
        self.on_connect = None
        self.on_message = None

    def connect(self, host, port, keepalive):
        self.connected_to = (host, port)

    def loop_start(self):
        threading.Timer(0.05, lambda: self.on_connect(self, None, {}, 0)).start()

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def subscribe(self, filter_):
        self.subscriptions.append(filter_)

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))

    def deliver(self, topic, payload: bytes, retain=False):
        self.on_message(self, None, _FakeMessage(topic, payload, retain))


def test_a_real_connection_subscribes_and_records_what_the_broker_delivers(tmp_path):
    fake = _FakeClient()
    capture = QueueCapture(
        url="mqtt://10.42.0.1:1883",
        log_path=tmp_path / "mqtt" / "queue.jsonl",
        filters=("alteriom/#", "mesh/status/#"),
        client_factory=lambda: fake,
    )
    capture.start(timeout=2)
    assert fake.connected_to == ("10.42.0.1", 1883)
    assert fake.subscriptions == ["alteriom/#", "mesh/status/#"]

    fake.deliver("alteriom/gateways/G1/status", b'{"uptime_s": 12}', retain=True)
    status = capture.wait_for("alteriom/gateways/+/status", timeout=1)
    assert status.payload == '{"uptime_s": 12}'
    assert status.retain is True

    capture.publish("alteriom/config/command", {"command": "get_config"})
    assert fake.published == [("alteriom/config/command", '{"command": "get_config"}', False)]
    capture.stop()


def test_a_broker_that_never_answers_is_an_error_not_a_hang():
    class Silent(_FakeClient):
        def loop_start(self):
            pass

    capture = QueueCapture(url="mqtt://10.42.0.1:1883", client_factory=Silent)
    with pytest.raises(ConnectionError, match="no CONNACK"):
        capture.start(timeout=0.2)


def test_only_mqtt_urls_are_accepted():
    with pytest.raises(ValueError, match="mqtt://"):
        QueueCapture(url="http://10.42.0.1:1883", client_factory=_FakeClient).start(timeout=0.1)
