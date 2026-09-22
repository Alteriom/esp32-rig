#!/usr/bin/env python3
"""Deterministic HTTP target for physical painlessMesh gateway tests.

Two kinds of route live here. The plain ones (``/status/{code}``, ``/echo``,
``/delay/{seconds}``, ``/redirect``, ``/retry-after/{seconds}``) answer with
whatever the path asks for.
The CallMeBot emulation (``/callmebot/whatsapp.php``) answers the way that
real service does -- with HTTP statuses that do not encode delivery -- so the
suite can hold the gateway to the delivery ledger rather than to a status code.

Every request is recorded under a tag and ``GET /requests/{tag}`` returns the
latest record, with ``delivered`` saying whether the emulated service accepted
the message, ``count`` how many requests arrived under the tag, and
``request_ids`` the distinct ``X-Request-Id`` values they carried -- so a row
can tell one request retried from one call issued several times. painlessMesh's own test point (``test/mock-http-server/server.py``
in that repository) serves the same routes with the same record shape, so a
suite row runs unchanged against either.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse


# What this probe offers beyond the routes every version has, named on
# /health. The probe is a rig resource, installed and restarted with each farm
# release; a row that needs one of these asks for it first and is SKIPPED on a
# rig whose probe does not offer it -- a rig without the feature, not a
# library that failed it. painlessMesh's test point names the same features.
PROBE_FEATURES = (
    "ledger.count",        # records carry how many requests a tag has seen
    "ledger.request_ids",  # ...and the distinct X-Request-Id values they carried
    "retry_after",         # /retry-after/{seconds}
    "callmebot.paused_after_echo",  # the #463 reply: long echo, verdict last
    "callmebot.chunked",   # queued-chunked: the reply in chunked transfer coding
)


class RequestStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.latest: dict[str, dict] = {}
        self.counts: dict[str, int] = {}
        self.first_seen: dict[str, float] = {}
        self.request_ids: dict[str, list[str]] = {}

    def append(self, record: dict) -> None:
        tag = record.get("tag")
        with self.lock:
            if tag:
                self.counts[tag] = self.counts.get(tag, 0) + 1
                self.first_seen.setdefault(tag, time.monotonic())
                ids = self.request_ids.setdefault(tag, [])
                if record.get("request_id") and record["request_id"] not in ids:
                    ids.append(record["request_id"])
                record["count"] = self.counts[tag]
                record["request_ids"] = list(ids)
                self.latest[tag] = record
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, sort_keys=True) + "\n")

    def get(self, tag: str) -> Optional[dict]:
        with self.lock:
            return self.latest.get(tag)

    def seen(self, tag: str) -> tuple[int, Optional[float]]:
        """How many requests arrived under tag, and when (monotonic) the first did."""
        with self.lock:
            return self.counts.get(tag, 0), self.first_seen.get(tag)


# A gateway request that must outlast painlessMesh's own HTTP timeout.
# GATEWAY_HTTP_TIMEOUT_MS is NODE_TIMEOUT/5, so 2s by default; the cap keeps a
# mistyped path from holding a probe thread for a whole suite.
MAX_DELAY_SECONDS = 30.0


# CallMeBot's WhatsApp API does not encode delivery in its HTTP status. Probed
# on 2026-09-10 while triaging painlessMesh #450: a bogus phone/apikey answered
# 203 with an HTML "Too many requests" page, a request missing the apikey or
# the phone answered 201 with the same page, and the reporter's bridge got a
# 208. With the body read (#451) the same bridge printed that 208 as "sent"
# and the message still never arrived (#452): 208 is a non-delivery whatever
# the body says, and its real body is still unknown. The success text is the
# one CallMeBot's examples show for a queued message. The profile is chosen by
# the ``apikey`` parameter; ``delivered`` is the ledger's ground truth for it.
# Keep this table identical to painlessMesh's test/mock-http-server/server.py.
CALLMEBOT_QUEUED = (
    "<p><b>Message queued.</b> You will receive it within a few seconds.</p>"
)
CALLMEBOT_TOO_MANY = (
    "<h1>Oops! Too many requests...</h1>"
    "<p>You have called to the API to often. Please review your script/code/app.</p>"
)
CALLMEBOT_ALREADY_REPORTED = (
    "<p>HTTP 208 Already Reported</p>"
    "<p>Seen from CallMeBot in painlessMesh #450 and #452; the message never arrived.</p>"
)
# The reply of painlessMesh #463, in shape: CallMeBot echoed the request -- the
# recipient and the whole text -- and gave its verdict last. Longer than the
# 512 + 256 bytes a gateway keeps of a body, with the verdict only in the
# tail, so a gateway that keeps just the start hands the application the echo
# and nothing that says why. The number is a placeholder; the reporter's real
# one is public in that issue and is not repeated here.
CALLMEBOT_PAUSED_AFTER_ECHO = (
    "<p>371 Message to: +10000000000</p><p>Text to send: "
    + "ALARM: O2 level critical at 5.4 mg/L! Node: 3394043125 " * 16
    + "</p><p><b>Your Account is Paused</b> due to technical issues. Please send "
    "the word 'resume' to the bot to re-enable the service.</p>"
)
CALLMEBOT_PROFILES = {
    "queued": (200, CALLMEBOT_QUEUED, True),
    "ratelimit-203": (203, CALLMEBOT_TOO_MANY, False),
    "ratelimit-201": (201, CALLMEBOT_TOO_MANY, False),
    "unverified-208": (208, CALLMEBOT_ALREADY_REPORTED, False),
    # The same 208 under the friendliest body the service has. Undelivered
    # all the same: a gateway that decides by the body's words instead of the
    # status would report this one as sent, and this row is what rejects it.
    "queued-208": (208, CALLMEBOT_QUEUED, False),
    # #463: HTTP 200, a long echo, and the refusal at the very end.
    "paused-after-echo": (200, CALLMEBOT_PAUSED_AFTER_ECHO, False),
    # The queued reply as the real service frames it: HTTP/1.1,
    # Transfer-Encoding: chunked, no Content-Length (see CALLMEBOT_CHUNKED).
    "queued-chunked": (200, CALLMEBOT_QUEUED, True),
}
# Profiles served with Transfer-Encoding: chunked, and their chunk size in
# bytes. The real CallMeBot answers chunked; farm run 35002111795 (painlessMesh
# 2b19aba) handed the application "a6 Message to: ... 0" -- the framing
# leaked into the body. Keep identical to painlessMesh's test point.
CALLMEBOT_CHUNKED = {"queued-chunked": 16}


def chunked_body(body: bytes, size: int) -> bytes:
    """``body`` in HTTP/1.1 chunked transfer coding: chunks of ``size`` bytes,
    each ``<hex size>\\r\\n<data>\\r\\n``, then the ``0\\r\\n\\r\\n`` terminator."""
    framed = b"".join(
        b"%x\r\n%s\r\n" % (len(body[start:start + size]), body[start:start + size])
        for start in range(0, len(body), size)
    )
    return framed + b"0\r\n\r\n"


# The longest Retry-After /retry-after/{seconds} will ask for. painlessMesh
# retries on its own only up to 60 s; the cap keeps a mistyped path from
# parking a row for an hour.
MAX_RETRY_AFTER_SECONDS = 120


def retry_after_seconds(path: str):
    """Seconds a /retry-after/{seconds} request asks the client to wait.

    None when the path is not a retry-after request. An unparseable or
    negative value becomes 0: the refusal still happens, with no wait asked.
    """
    if not path.startswith("/retry-after/"):
        return None
    raw = path[len("/retry-after/"):].split("/", 1)[0]
    try:
        seconds = int(raw)
    except ValueError:
        return 0
    return max(0, min(seconds, MAX_RETRY_AFTER_SECONDS))


def delay_seconds(path: str):
    """Seconds to stall before answering a /delay/{seconds} request.

    Returns None when the path is not a delay request, so the caller can fall
    through to the normal routes. An unparseable or negative value answers
    immediately rather than failing: the point of the route is the stall, and a
    test that mistypes it should see a fast 200 it can notice, not a hang.
    """
    if not path.startswith("/delay/"):
        return None
    raw = path[len("/delay/"):].split("/", 1)[0]
    try:
        seconds = float(raw)
    except ValueError:
        return 0.0
    if seconds < 0:
        return 0.0
    return min(seconds, MAX_DELAY_SECONDS)


def handler_for(store: RequestStore):
    class ProbeHandler(BaseHTTPRequestHandler):
        server_version = "AlteriomHILProbe/2"

        def _reply(self, status: int, payload: dict, headers: Optional[dict] = None) -> None:
            self._reply_raw(status, json.dumps(payload, sort_keys=True).encode(),
                            "application/json", headers)

        def _reply_raw(self, status: int, body: bytes, content_type: str,
                       headers: Optional[dict] = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _tag(self, text: str = "") -> str:
            query = parse_qs(urlparse(self.path).query)
            tag = query.get("tag", [self.headers.get("X-HIL-Tag", "")])[0]
            return tag or text

        def _record(self, body: str = "", status: int = 200,
                    delivered: Optional[bool] = None, **extra) -> dict:
            parsed = urlparse(self.path)
            tag = self._tag(extra.get("text") or "")
            record = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "method": self.command,
                "path": parsed.path,
                "tag": tag,
                "body": body,
                "client": self.client_address[0],
                "status": status,
                "delivered": (200 <= status < 300) if delivered is None else bool(delivered),
                # painlessMesh sends one id on every attempt at a call (#464)
                "request_id": self.headers.get("X-Request-Id", ""),
                "idempotency_key": self.headers.get("Idempotency-Key", ""),
            }
            record.update(extra)
            store.append(record)
            return record

        def _callmebot(self, parsed) -> None:
            query = parse_qs(parsed.query)
            phone = query.get("phone", [None])[0]
            apikey = query.get("apikey", [None])[0]
            text = query.get("text", [None])[0]
            profile = CALLMEBOT_PROFILES.get(apikey or "")
            if profile is None or not phone or not text:
                # A typo in a row must fail loudly, not pass for a service quirk.
                self._record(status=400, delivered=False, text=text or "",
                             profile=apikey or "")
                self._reply(400, {
                    "error": "unknown CallMeBot profile or missing phone/text",
                    "apikey": apikey,
                    "profiles": sorted(CALLMEBOT_PROFILES),
                })
                return
            status, body, delivered = profile
            self._record(status=status, delivered=delivered, text=text,
                         profile=apikey, response=body)
            chunk = CALLMEBOT_CHUNKED.get(apikey)
            if chunk:
                self._reply_chunked(status, body.encode(), "text/html; charset=utf-8", chunk)
                return
            self._reply_raw(status, body.encode(), "text/html; charset=utf-8")

        def _reply_chunked(self, status: int, body: bytes, content_type: str, size: int) -> None:
            """HTTP/1.1 with Transfer-Encoding: chunked and no Content-Length,
            then the connection closes -- as the real CallMeBot answers."""
            self.protocol_version = "HTTP/1.1"
            self.close_connection = True
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(chunked_body(body, size))

        def _retry_after(self, seconds: int) -> None:
            """Refuse the first request under a tag with 429 and Retry-After,
            accept every later one, and record whether it came early."""
            tag = self._tag()
            if not tag:
                self._reply(400, {"error": "/retry-after needs a tag"})
                return
            seen, first = store.seen(tag)
            if seen == 0:
                self._record(status=429, delivered=False, retry_after_s=seconds)
                self._reply(429, {"ok": False, "retry_after_s": seconds},
                            headers={"Retry-After": str(seconds)})
                return
            waited = time.monotonic() - first
            record = self._record(status=200, delivered=True, retry_after_s=seconds,
                                  waited_s=round(waited, 3), early=waited < seconds)
            self._reply(200, {"ok": True, "request": record})

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._reply(200, {"status": "ok", "ok": True,
                                  "features": list(PROBE_FEATURES)})
                return
            if parsed.path.startswith("/requests/"):
                tag = parsed.path.removeprefix("/requests/")
                record = store.get(tag)
                self._reply(200 if record else 404, record or {"error": "not found"})
                return
            if parsed.path == "/callmebot/whatsapp.php":
                self._callmebot(parsed)
                return
            wait = retry_after_seconds(parsed.path)
            if wait is not None:
                self._retry_after(wait)
                return
            stall = delay_seconds(parsed.path)
            if stall is not None:
                # Recorded before the stall, so /requests/{tag} proves the
                # gateway did issue the request even when it gives up before
                # this reply lands.
                record = self._record()
                time.sleep(stall)
                self._reply(200, {"ok": True, "delayed": stall, "request": record})
                return
            if parsed.path.startswith("/status/"):
                try:
                    status = int(parsed.path.rsplit("/", 1)[1])
                except ValueError:
                    status = 400
            elif parsed.path == "/redirect":
                status = 302
            else:
                status = 200
            record = self._record(status=status)
            self._reply(status, {"ok": 200 <= status < 300, "request": record})

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8", errors="replace")
            record = self._record(body)
            self._reply(200, {"ok": True, "request": record})

        def log_message(self, _format: str, *_args) -> None:
            return

    return ProbeHandler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument(
        "--log", type=Path, default=Path("/var/lib/alteriom-hil/gateway-requests.jsonl")
    )
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.bind, args.port), handler_for(RequestStore(args.log)))
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
