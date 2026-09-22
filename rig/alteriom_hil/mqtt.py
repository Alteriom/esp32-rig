"""The queue side of a run: what a gateway board published, captured and asserted on.

The product's last third is a gateway publishing to an MQTT queue, and a
suite needs to read that queue the way it reads a serial port -- collect
everything, wait for a line that matches, keep the whole capture as
evidence. This is that, shared by every consumer's suite and (later) by the
simulator, so a sim-mode test and a hardware test read the queue through
the same call.

The broker is the farm host's (``ALTERIOM_HIL_MQTT_URL``; see
``docs/mqtt-validation-plan.md``). ``QueueCapture.from_env`` returns None
when the variable is absent, so a suite skips its queue scenario rather
than connecting to nothing. ``inject`` feeds a capture without a broker --
what a sim double uses -- and ``paho-mqtt`` is imported only when a real
connection is opened, so sim mode needs nothing installed.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse


class QueueTimeout(AssertionError):
    """Nothing matching arrived in time. Carries what did, for the failure
    message: a queue that stays silent is a question about what the gateway
    said instead, and an assertion cannot answer it without that."""

    def __init__(self, what: str, seen: list[str]):
        tail = ", ".join(seen[-8:]) or "nothing"
        super().__init__(f"timed out waiting for {what}; last topics seen: {tail}")
        self.seen = seen


@dataclass
class Message:
    ts: float
    topic: str
    payload: str
    retain: bool = False
    qos: int = 0

    def json(self):
        """The payload as JSON, or None when it is not JSON."""
        try:
            return json.loads(self.payload)
        except ValueError:
            return None


def topic_matches(filter_: str, topic: str) -> bool:
    """MQTT subscription matching: ``+`` one level, ``#`` the rest.

    Local rather than paho's ``topic_matches_sub`` so sim mode, which never
    opens a connection, needs no paho installed.
    """
    if filter_ == "#":
        return True
    want = filter_.split("/")
    have = topic.split("/")
    for i, part in enumerate(want):
        if part == "#":
            return True
        if i >= len(have):
            return False
        if part != "+" and part != have[i]:
            return False
    return len(want) == len(have)


@dataclass
class QueueCapture:
    """Everything received from a broker, appended to a JSONL log as it arrives.

    ``url`` is ``mqtt://host:port``. ``log_path`` is where the capture goes
    (``<log_dir>/mqtt/<name>.jsonl`` from ``from_env``); the farm serves it
    as evidence beside the serial logs. ``client_factory`` is what opens the
    connection -- paho by default, injectable for tests.
    """

    url: str
    log_path: Optional[Path] = None
    filters: tuple[str, ...] = ("#",)
    client_factory: Optional[Callable[[], object]] = None
    _messages: list[Message] = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _client: object = field(default=None, repr=False)
    _connected: threading.Event = field(default_factory=threading.Event, repr=False)

    @classmethod
    def from_env(cls, log_dir: str | os.PathLike | None = None, name: str = "queue") -> "QueueCapture | None":
        url = os.environ.get("ALTERIOM_HIL_MQTT_URL")
        if not url:
            return None
        log_dir = log_dir if log_dir is not None else os.environ.get("ALTERIOM_HIL_LOG_DIR")
        log_path = Path(log_dir) / "mqtt" / f"{name}.jsonl" if log_dir else None
        return cls(url=url, log_path=log_path)

    # ---- connection ----

    def start(self, timeout: float = 10.0) -> "QueueCapture":
        parsed = urlparse(self.url)
        if parsed.scheme != "mqtt" or not parsed.hostname:
            raise ValueError(f"not an mqtt://host:port URL: {self.url}")
        client = (self.client_factory or self._paho_client)()
        client.on_connect = lambda c, u, flags, rc, *rest: self._on_connect(c, rc)
        client.on_message = lambda c, u, msg: self._on_message(msg.topic, msg.payload, msg.retain, msg.qos)
        client.connect(parsed.hostname, parsed.port or 1883, keepalive=30)
        client.loop_start()
        self._client = client
        if not self._connected.wait(timeout):
            client.loop_stop()
            raise ConnectionError(f"no CONNACK from {self.url} within {timeout:.0f}s")
        return self

    def stop(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass

    @staticmethod
    def _paho_client():
        import paho.mqtt.client as mqtt  # only here: sim mode never gets this far

        try:
            return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        except AttributeError:  # paho < 2
            return mqtt.Client()

    def _on_connect(self, client, rc) -> None:
        for filter_ in self.filters:
            client.subscribe(filter_)
        self._connected.set()

    def _on_message(self, topic: str, payload, retain: bool, qos: int) -> None:
        text = payload.decode("utf-8", errors="replace") if isinstance(payload, (bytes, bytearray)) else str(payload)
        self._record(Message(time.time(), topic, text, bool(retain), int(qos)))

    # ---- feeding without a broker ----

    def inject(self, topic: str, payload, retain: bool = False) -> None:
        """What a sim double calls instead of a gateway publishing."""
        text = payload if isinstance(payload, str) else json.dumps(payload)
        self._record(Message(time.time(), topic, text, retain, 0))

    def _record(self, message: Message) -> None:
        with self._lock:
            self._messages.append(message)
            if self.log_path is not None:
                try:
                    self.log_path.parent.mkdir(parents=True, exist_ok=True)
                    with self.log_path.open("a", encoding="utf-8") as out:
                        out.write(json.dumps(message.__dict__) + "\n")
                except OSError:
                    # The capture in memory is what the test asserts on; the
                    # file is evidence, and a missing one is not a verdict.
                    pass

    # ---- reading ----

    def messages(self, topic_filter: str = "#") -> list[Message]:
        with self._lock:
            return [m for m in self._messages if topic_matches(topic_filter, m.topic)]

    def wait_for(
        self,
        topic_filter: str,
        predicate: Optional[Callable[[Message], bool]] = None,
        timeout: float = 30.0,
        description: Optional[str] = None,
    ) -> Message:
        """The first message matching ``topic_filter`` (and ``predicate``),
        already received or arriving within ``timeout``."""
        deadline = time.monotonic() + timeout
        while True:
            for message in self.messages(topic_filter):
                if predicate is None or predicate(message):
                    return message
            if time.monotonic() >= deadline:
                with self._lock:
                    seen = [m.topic for m in self._messages]
                raise QueueTimeout(description or topic_filter, seen)
            time.sleep(0.1)

    # ---- writing ----

    def publish(self, topic: str, payload, retain: bool = False) -> None:
        """For a test that has to send something -- a config command a
        gateway answers. Needs a connection; a double records it instead."""
        text = payload if isinstance(payload, str) else json.dumps(payload)
        if self._client is None:
            self.inject(topic, text, retain)
            return
        self._client.publish(topic, text, qos=0, retain=retain)
