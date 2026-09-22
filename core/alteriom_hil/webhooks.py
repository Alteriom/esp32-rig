"""Events the farm sends to somebody else's software.

A notification is for a person: a line in Telegram saying a board went red.
This is the other thing -- an **event**, for a program: the same fact, whole,
signed, with an identity of its own so the receiver can tell a retry from a
second occurrence. The farm has had the first since the beginning; consumers
that want to act on what the farm sees need the second.

The shape is deliberately the one Alteriom's webhook connector already sends
(`src/dispatch/http-adapter.ts` there), so anything already ingesting from it
-- its Python client, Command Center -- reads these without being taught a
second format:

    {"event": "run", "action": "failed", "delivery_id": "...",
     "rig": "rig02", "summary": "...", "payload": {...},
     "received_at": "...", "dispatched_at": "..."}

`event` is the base type and `action` is separate: `run.failed` is a
subscription filter, never a value on the wire. A field with nothing in it is
left out rather than sent as null, because the connector's `JSON.stringify`
drops undefined and a receiver that verifies a signature over re-serialized
JSON must see the same bytes.

The one addition is `rig`, where the connector puts `repository`: the farm's
events are about rigs. It is absent on an event about the farm itself, the way
`repository` is absent on an organisation-level event there.

Signing is the connector's, exactly: HMAC-SHA256 over the serialized body and
nothing else, lowercase hex, `sha256=` in front. Not the timestamp, not the
delivery id, not a concatenation -- the bytes that are transmitted.

There is no replay window in that scheme. `dispatched_at` is *inside* the
envelope, so it is covered by the signature: a receiver that wants one rejects
an envelope stamped too long ago, without needing a header the signature does
not cover.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

# What the farm can say, as base type and the actions each takes. A
# subscription names either the base type ("run": everything a run does) or one
# action of it ("run.failed"), or "*" for all of it.
EVENTS: dict[str, tuple[str, ...]] = {
    # A rig, as the farm sees it from outside: it appeared, it stopped
    # answering, it is behind the release the farm wants it on.
    "rig": ("joined", "online", "offline", "behind", "drained", "resumed"),
    # One run, through its life.
    "run": ("queued", "started", "passed", "failed", "cancelled"),
    # A board on a rig.
    "board": ("red", "recovered", "registered", "missing"),
    # The queue a portal allocates from.
    "queue": ("paused", "resumed"),
    # What a rig's own health check concluded about its host.
    "health": ("ok", "degraded", "unhealthy"),
}

# Everything a rig's own subscription may ask for: what the farm knows about
# that rig. The queue belongs to the portal, so it is the farm's to report.
RIG_EVENTS: tuple[str, ...] = ("rig", "run", "board", "health")

WILDCARD = "*"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_secret() -> str:
    """A signing secret, when whoever is subscribing did not bring one."""
    return secrets.token_hex(32)


def valid_event(name: str) -> bool:
    """Whether a subscription may ask for this, as a base type or one action."""
    if name == WILDCARD:
        return True
    base, _, action = name.partition(".")
    if base not in EVENTS:
        return False
    return not action or action in EVENTS[base]


def every_event(scope_events: tuple[str, ...] = tuple(EVENTS)) -> list[str]:
    """Every name a subscription could list, base types and actions both."""
    names: list[str] = []
    for base in scope_events:
        names.append(base)
        names.extend(f"{base}.{action}" for action in EVENTS[base])
    return names


def wants(asked: list[str] | tuple[str, ...], event: str, action: str | None) -> bool:
    """Whether a subscription that asked for these wants this event.

    `["run"]` is every run; `["run.failed"]` is only that one. An empty list is
    nothing -- a subscription that asks for nothing is off, and saying so here
    is better than quietly meaning everything.
    """
    for name in asked:
        if name == WILDCARD:
            return True
        base, _, wanted = name.partition(".")
        if base != event:
            continue
        if not wanted or wanted == (action or ""):
            return True
    return False


@dataclass
class Event:
    """One thing that happened, before it belongs to any subscription."""

    event: str
    action: str | None = None
    rig: str | None = None
    summary: str = ""
    payload: dict = field(default_factory=dict)
    sender: str | None = None
    received_at: str = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if self.event not in EVENTS:
            raise ValueError(f"event must be one of {', '.join(EVENTS)}")
        if self.action and self.action not in EVENTS[self.event]:
            raise ValueError(f"a {self.event} event does not do {self.action}")
        if not self.summary:
            raise ValueError("an event carries a summary: one line a person can read")

    @property
    def name(self) -> str:
        """`run.failed` -- what a subscription filters on, never sent as
        `event`."""
        return f"{self.event}.{self.action}" if self.action else self.event


def delivery_id(event: Event, at: str | None = None) -> str:
    """The identity of one delivery, and what a receiver deduplicates on.

    Structured like the connector's (`batch-<id>`, `source-silence-<id>`) so it
    reads as something rather than as a random string, and stable across the
    retries of one delivery -- a retried alert must not look like a second one.
    """
    stamp = (at or utcnow()).replace(":", "").replace("-", "")
    return f"{event.name.replace('.', '-')}-{stamp}-{uuid.uuid4().hex[:12]}"


def envelope(event: Event, delivery: str, dispatched_at: str | None = None) -> dict:
    """What is sent, in the order the connector writes it.

    Nothing empty is included: the connector's JSON.stringify drops undefined,
    and a receiver verifying the signature over re-serialized JSON has to see
    the same bytes back.
    """
    body = {"event": event.event}
    if event.action:
        body["action"] = event.action
    body["delivery_id"] = delivery
    if event.rig:
        body["rig"] = event.rig
    if event.sender:
        body["sender"] = event.sender
    body["summary"] = event.summary
    body["payload"] = event.payload or {}
    body["received_at"] = event.received_at
    body["dispatched_at"] = dispatched_at or utcnow()
    return body


def serialize(body: dict) -> bytes:
    """The bytes that are sent and the bytes that are signed -- one call, so
    they cannot drift. Compact separators, keys in the order they were built:
    a receiver that re-serializes what it parsed gets the same string."""
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign(body: bytes, secret: str) -> str:
    """The connector's signature, byte for byte: HMAC-SHA256 of the body and
    nothing else, lowercase hex, `sha256=` in front."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify(body: bytes, secret: str, signature: str) -> bool:
    """For a receiver, and for our own tests: the same comparison the connector
    makes, in constant time, tolerating the prefix being absent."""
    said = (signature or "").strip()
    said = said[7:] if said.lower().startswith("sha256=") else said
    want = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    if len(said) != len(want):
        return False
    return hmac.compare_digest(said.lower(), want)


def headers(event: Event, delivery: str, signature: str) -> dict[str, str]:
    """What a delivery carries besides its body.

    `X-Connector-Signature-256` is what the connector produces and what its
    own consumers were built against. `X-Hub-Signature-256` carries the same
    value because that is the name most receivers already know -- including
    Command Center, which reads that one and would otherwise refuse a delivery
    it could have verified. Both are the same signature over the same bytes:
    a receiver checks whichever it knows.
    """
    signed = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "X-Webhook-Event": event.event,
        "X-Webhook-Delivery": delivery,
        "X-Connector-Signature-256": signature,
        "X-Hub-Signature-256": signature,
    }
    if event.action:
        # Not part of the connector's set, and additive: a receiver that
        # routes on the base type is unaffected, one that wants the action
        # need not parse the body to find it.
        signed["X-Webhook-Action"] = event.action
    if event.rig:
        signed["X-Webhook-Rig"] = event.rig
    return signed


USER_AGENT = "Alteriom-ESP32-Farm/1.0"


# ---- delivering ----------------------------------------------------------------
# The connector's semantics, because a receiver written against it already
# behaves this way: only 2xx is success, a redirect is a failure rather than
# something to follow, and an attempt that fails is tried again with the delay
# doubling. Every attempt is recorded, so "it is not arriving" is answerable
# from the page rather than from a log nobody kept.

DEFAULT_TIMEOUT_MS = 30_000
DEFAULT_MAX_RETRIES = 5
DEFAULT_BACKOFF_MS = 5_000


class DeliveryError(RuntimeError):
    """A delivery that did not arrive, with what the far end said."""


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect is a failure, not a place to go.

    `urlopen` follows 301, 302 and 303 by itself, and turns the POST into a
    GET while it does: the event body is dropped, the far end answers 2xx to
    something that carried nothing, and the delivery is recorded as having
    arrived. It will also follow https to http, which is the one thing the
    https-only rule exists to prevent. So the 3xx is raised and recorded as
    the failed attempt it is -- which is what the connector does too.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102 - stdlib contract
        return None


def post(url: str, body: bytes, sent: dict[str, str], timeout_ms: int) -> int:
    """One attempt. Raises DeliveryError for anything but a 2xx."""
    request = urllib.request.Request(url, data=body, method="POST", headers=sent)
    opener = urllib.request.build_opener(_NoRedirects)
    try:
        with opener.open(request, timeout=timeout_ms / 1000) as response:
            status = int(response.status)
            if not 200 <= status < 300:
                raise DeliveryError(f"HTTP {status}")
            return status
    except urllib.error.HTTPError as exc:
        # A redirect included: the connector does not follow one, and a
        # subscription pointed at a redirect is a subscription pointed at the
        # wrong place.
        detail = ""
        try:
            detail = (exc.read() or b"")[:200].decode("utf-8", "replace")
        except Exception:  # pragma: no cover - the body is a courtesy
            pass
        raise DeliveryError(f"HTTP {exc.code}{': ' + detail if detail else ''}") from None
    except urllib.error.URLError as exc:
        raise DeliveryError(f"{exc.reason}") from None
    except OSError as exc:
        raise DeliveryError(f"{exc}") from None


@dataclass
class Subscription:
    """Where one consumer wants the farm's events, and what it wants."""

    id: str
    scope: str                 # "farm", or the name of one rig
    url: str
    secret: str
    events: tuple[str, ...] = (WILDCARD,)
    active: bool = True
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    max_retries: int = DEFAULT_MAX_RETRIES
    backoff_ms: int = DEFAULT_BACKOFF_MS

    def takes(self, event: Event) -> bool:
        """Whether this subscription wants this event at all: its scope covers
        it, it is switched on, and it asked for that kind."""
        if not self.active:
            return False
        if self.scope != "farm" and self.scope != (event.rig or ""):
            return False
        return wants(self.events, event.event, event.action)


def deliver(
    subscription: Subscription,
    event: Event,
    delivery: str,
    *,
    send=post,
    record=None,
    sleep=None,
) -> dict:
    """Send one event to one subscriber, trying again while it fails.

    The envelope is built once and signed once: every attempt of a delivery
    carries the same bytes, the same signature and the same delivery id, so a
    receiver deduplicating on that id sees one event however many times the
    network made us ask.
    """
    import time

    sleep = sleep or time.sleep
    body = serialize(envelope(event, delivery))
    signature = sign(body, subscription.secret)
    sent = headers(event, delivery, signature)
    attempts = max(1, int(subscription.max_retries))
    outcome: dict = {}
    for attempt in range(1, attempts + 1):
        began = time.monotonic()
        try:
            status = send(subscription.url, body, sent, subscription.timeout_ms)
            outcome = {"ok": True, "status": status, "attempt": attempt, "error": None}
        except DeliveryError as exc:
            outcome = {"ok": False, "status": None, "attempt": attempt, "error": str(exc)}
        except Exception as exc:  # a delivery never takes the farm down with it
            outcome = {"ok": False, "status": None, "attempt": attempt,
                       "error": f"{exc.__class__.__name__}: {exc}"}
        outcome["latency_ms"] = int((time.monotonic() - began) * 1000)
        outcome["delivery_id"] = delivery
        if record is not None:
            # `dead` is the connector's word for an attempt that was the last
            # one: a reader can tell "failing" from "given up on".
            outcome["state"] = ("success" if outcome["ok"]
                                else "failed" if attempt < attempts else "dead")
            record(subscription, event, outcome)
        if outcome["ok"]:
            return outcome
        if attempt < attempts:
            sleep((subscription.backoff_ms / 1000) * (2 ** (attempt - 1)))
    return outcome
