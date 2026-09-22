"""The probe's /delay route, which gives the gateway a destination that
answers too late.

painlessMesh #446 hid a truncated transport error behind a `uint16_t`. Every
error case the physical suite had pointed at this probe and asked it for a
status, which is a positive int on the wire, so the branch handling a request
that never got a response was never executed on hardware. A destination that
stalls past GATEWAY_HTTP_TIMEOUT_MS is one of the three that now do.
"""

import importlib.util
import time
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "suites" / "painlessmesh" / "gateway_probe_server.py"
)
SPEC = importlib.util.spec_from_file_location("gateway_probe_server", MODULE_PATH)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def test_non_delay_paths_are_not_stalled():
    for path in ("/status/200", "/echo", "/health", "/requests/abc", "/redirect"):
        assert probe.delay_seconds(path) is None


def test_delay_path_returns_the_requested_seconds():
    assert probe.delay_seconds("/delay/5") == 5.0
    assert probe.delay_seconds("/delay/0.5") == 0.5


def test_query_string_is_not_part_of_the_path():
    # The handler passes urlparse().path, so the tag never reaches this helper.
    assert probe.delay_seconds("/delay/5") == 5.0


def test_delay_is_capped_so_one_request_cannot_hold_a_thread_all_run():
    assert probe.delay_seconds("/delay/9999") == probe.MAX_DELAY_SECONDS


def test_unparseable_or_negative_delay_answers_immediately():
    # A mistyped path should show up as a fast, visibly wrong 200 rather than a
    # hang that looks like the timeout the test was trying to provoke.
    assert probe.delay_seconds("/delay/soon") == 0.0
    assert probe.delay_seconds("/delay/-3") == 0.0


def test_delay_exceeds_the_library_http_timeout():
    """GATEWAY_HTTP_TIMEOUT_MS is NODE_TIMEOUT/5 — 2 s at painlessMesh's
    defaults. The suite asks for 5 s; keep the cap above that so the value the
    tests use is never silently clamped down to something that returns in time.
    """
    assert probe.delay_seconds("/delay/5") == 5.0
    assert probe.MAX_DELAY_SECONDS > 2.0


# ---------------------------------------------------------------------------
# painlessMesh #450: the CallMeBot emulation and the delivery ledger.
#
# These run the real handler on a loopback port: the route's whole point is
# what goes on the wire versus what the ledger records, so a helper-level test
# would prove nothing.
# ---------------------------------------------------------------------------

import json
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest


@pytest.fixture()
def probe_server(tmp_path):
    store = probe.RequestStore(tmp_path / "requests.jsonl")
    server = ThreadingHTTPServer(("127.0.0.1", 0), probe.handler_for(store))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _get(url):
    try:
        with urlopen(url, timeout=5) as response:
            return response.status, response.read().decode()
    except HTTPError as exc:
        return exc.code, exc.read().decode()


def _ledger(base, tag):
    status, body = _get(f"{base}/requests/{tag}")
    assert status == 200, body
    return json.loads(body)


@pytest.mark.parametrize("profile", sorted(probe.CALLMEBOT_PROFILES))
def test_callmebot_profile_answers_its_status_and_records_delivery(probe_server, profile):
    expected_status, expected_body, delivered = probe.CALLMEBOT_PROFILES[profile]
    tag = f"t-{profile}"
    status, body = _get(
        f"{probe_server}/callmebot/whatsapp.php?phone=%2B1&apikey={profile}&text={tag}"
    )
    assert status == expected_status
    assert body == expected_body

    record = _ledger(probe_server, tag)
    assert record["delivered"] is delivered
    assert record["status"] == expected_status
    assert record["profile"] == profile
    # The text is the tag when none is given, so a suite row can find its own
    # message by what it sent.
    assert record["text"] == tag


def test_callmebot_status_does_not_encode_delivery(probe_server):
    """The property the emulation exists for: 200 and 201 are both 2xx, one
    delivered and one refused, and the 208 painlessMesh #452 saw in the field
    delivered nothing. A gateway that trusts the status class gets two of the
    three wrong."""
    verdicts = {}
    for profile in ("ratelimit-201", "queued", "unverified-208", "queued-208"):
        _get(f"{probe_server}/callmebot/whatsapp.php?phone=%2B1&apikey={profile}&text={profile}")
        verdicts[profile] = _ledger(probe_server, profile)["delivered"]
    assert verdicts["ratelimit-201"] is False and verdicts["queued"] is True
    assert verdicts["unverified-208"] is False
    # Same body as the delivered 200, different status, not delivered: neither
    # the status class nor the body's words decide on their own.
    assert verdicts["queued-208"] is False


def test_callmebot_unknown_profile_fails_loudly(probe_server):
    status, body = _get(f"{probe_server}/callmebot/whatsapp.php?phone=%2B1&apikey=nope&text=x")
    assert status == 400
    assert "nope" in body


def test_callmebot_profiles_match_the_painlessmesh_test_point():
    """painlessMesh's test/mock-http-server/server.py serves the same table.
    Pin the shape here so a change on either side is a deliberate one."""
    assert set(probe.CALLMEBOT_PROFILES) == {
        "queued", "ratelimit-203", "ratelimit-201", "unverified-208", "queued-208",
        "paused-after-echo", "queued-chunked",
    }
    assert probe.CALLMEBOT_PROFILES["queued-chunked"] == (200, probe.CALLMEBOT_QUEUED, True)
    assert probe.CALLMEBOT_CHUNKED == {"queued-chunked": 16}
    for status, body, delivered in probe.CALLMEBOT_PROFILES.values():
        assert 200 <= status < 300
        # Only a verified 200 with the queued text is a delivery; a refusal
        # page or a 208 never is, whatever the body (painlessMesh #452).
        assert delivered is (status == 200 and body == probe.CALLMEBOT_QUEUED)


def test_the_paused_reply_puts_its_verdict_past_what_a_gateway_keeps_from_the_start():
    """painlessMesh #463. A gateway keeps the first 512 and the last 256 bytes
    of a body; this reply must need the last part to be understood, or the row
    it feeds could pass on a gateway that keeps only the start."""
    body = probe.CALLMEBOT_PAUSED_AFTER_ECHO
    assert len(body) > 512 + 256
    verdict = body.index("Account is Paused")
    assert verdict > 512, "not in the head"
    assert verdict >= len(body) - 256, "in the tail"
    assert "+10000000000" in body, "a placeholder number, never a real one"


def _raw_get(base, path):
    """Everything the probe writes for one request, as bytes off the socket."""
    import socket
    from urllib.parse import urlparse

    address = urlparse(base)
    with socket.create_connection((address.hostname, address.port), timeout=5) as connection:
        connection.sendall(f"GET {path} HTTP/1.1\r\nHost: {address.netloc}\r\n\r\n".encode())
        received = b""
        while True:
            data = connection.recv(4096)
            if not data:
                break  # Connection: close -- the probe ends the reply by closing
            received += data
    return received


def test_the_chunked_profile_is_framed_as_the_real_callmebot_frames_it(probe_server):
    raw = _raw_get(probe_server, "/callmebot/whatsapp.php?phone=%2B10000000000&apikey=queued-chunked&text=chunky")
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    assert lines[0] == "HTTP/1.1 200 OK"
    headers = {name.lower(): value.strip() for name, _, value in (line.partition(":") for line in lines[1:])}
    assert headers["transfer-encoding"] == "chunked"
    assert headers["connection"] == "close"
    assert "content-length" not in headers

    expected = probe.CALLMEBOT_QUEUED.encode()
    # 16-byte chunks, a short last one, then the zero-length terminator.
    chunks, rest = [], body
    while True:
        size_line, _, rest = rest.partition(b"\r\n")
        size = int(size_line, 16)
        if size == 0:
            assert rest == b"\r\n", "the terminator, and nothing after it"
            break
        chunks.append(rest[:size])
        assert rest[size:size + 2] == b"\r\n"
        rest = rest[size + 2:]
    assert b"".join(chunks) == expected
    assert [len(chunk) for chunk in chunks[:-1]] == [16] * (len(chunks) - 1)
    assert 0 < len(chunks[-1]) <= 16 and len(chunks) == -(-len(expected) // 16)
    assert body.startswith(b"10\r\n")
    assert probe.chunked_body(expected, 16) == body

    record = _ledger(probe_server, "chunky")
    assert record["delivered"] is True and record["status"] == 200 and record["profile"] == "queued-chunked"

    # A client that speaks HTTP/1.1 reads the body decoded.
    status, text = _get(f"{probe_server}/callmebot/whatsapp.php?phone=%2B1&apikey=queued-chunked&text=decoded")
    assert status == 200 and text == probe.CALLMEBOT_QUEUED


def test_the_other_profiles_keep_their_content_length_replies(probe_server):
    raw = _raw_get(probe_server, "/callmebot/whatsapp.php?phone=%2B1&apikey=queued&text=plain")
    head, _, body = raw.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.0 200")
    assert b"Transfer-Encoding" not in head and f"Content-Length: {len(body)}".encode() in head
    assert body == probe.CALLMEBOT_QUEUED.encode()


def test_plain_routes_record_delivery_from_their_status(probe_server):
    _get(f"{probe_server}/status/503?tag=s503")
    assert _ledger(probe_server, "s503")["delivered"] is False
    assert _ledger(probe_server, "s503")["status"] == 503
    _get(f"{probe_server}/status/200?tag=s200")
    assert _ledger(probe_server, "s200")["delivered"] is True


def test_existing_record_shape_is_preserved(probe_server):
    """Rows written before #450 read method, path, tag, body and client."""
    _get(f"{probe_server}/echo?tag=shape")
    record = _ledger(probe_server, "shape")
    for key in ("ts", "method", "path", "tag", "body", "client"):
        assert key in record
    assert record["method"] == "GET" and record["path"] == "/echo"


# ---------------------------------------------------------------------------
# painlessMesh #464: one call, one request. The ledger counts what arrived under
# a tag and the request ids it carried; /retry-after refuses once with 429.
# ---------------------------------------------------------------------------

from urllib.request import Request


def _get_with(url, headers):
    try:
        with urlopen(Request(url, headers=headers), timeout=5) as response:
            return response.status, dict(response.headers), response.read().decode()
    except HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode()


def test_ledger_counts_requests_and_their_ids(probe_server):
    for _ in range(2):
        _get_with(f"{probe_server}/status/200?tag=dup", {"X-Request-Id": "pm-1-1"})
    record = _ledger(probe_server, "dup")
    assert record["count"] == 2
    assert record["request_ids"] == ["pm-1-1"]
    assert record["request_id"] == "pm-1-1"

    _get_with(f"{probe_server}/status/200?tag=two", {"X-Request-Id": "pm-1-1"})
    _get_with(f"{probe_server}/status/200?tag=two", {"X-Request-Id": "pm-1-2"})
    assert _ledger(probe_server, "two")["request_ids"] == ["pm-1-1", "pm-1-2"]


def test_a_request_without_an_id_is_counted_but_names_none(probe_server):
    _get(f"{probe_server}/status/200?tag=noid")
    record = _ledger(probe_server, "noid")
    assert record["count"] == 1
    assert record["request_ids"] == []
    assert record["request_id"] == ""


def test_retry_after_path_parsing():
    assert probe.retry_after_seconds("/status/200") is None
    assert probe.retry_after_seconds("/retry-after/3") == 3
    assert probe.retry_after_seconds("/retry-after/soon") == 0
    assert probe.retry_after_seconds("/retry-after/-4") == 0
    assert probe.retry_after_seconds("/retry-after/99999") == probe.MAX_RETRY_AFTER_SECONDS


def test_retry_after_refuses_once_then_accepts(probe_server):
    status, headers, _ = _get_with(f"{probe_server}/retry-after/1?tag=ra", {})
    assert status == 429
    assert headers.get("Retry-After") == "1"
    first = _ledger(probe_server, "ra")
    assert first["delivered"] is False and first["count"] == 1

    # Too soon: accepted, and marked early.
    status, _, _ = _get_with(f"{probe_server}/retry-after/1?tag=ra", {})
    assert status == 200
    early = _ledger(probe_server, "ra")
    assert early["delivered"] is True and early["early"] is True and early["count"] == 2


def test_retry_after_records_a_patient_retry(probe_server):
    _get_with(f"{probe_server}/retry-after/1?tag=patient", {})
    time.sleep(1.1)
    _get_with(f"{probe_server}/retry-after/1?tag=patient", {})
    record = _ledger(probe_server, "patient")
    assert record["early"] is False
    assert record["waited_s"] >= 1.0


def test_health_names_the_features_rows_ask_for(probe_server):
    status, body = _get(f"{probe_server}/health")
    assert status == 200
    payload = json.loads(body)
    assert payload["ok"] is True
    assert set(payload["features"]) == {
        "ledger.count", "ledger.request_ids", "retry_after", "callmebot.paused_after_echo",
        "callmebot.chunked",
    }


def test_retry_after_needs_a_tag(probe_server):
    status, _, _ = _get_with(f"{probe_server}/retry-after/1", {})
    assert status == 400
