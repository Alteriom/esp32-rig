"""Dedicated-bridge and sendToInternet validation on the physical farm."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

import pytest

from alteriom_hil.protocol import BoardClient, TimeoutWaitingFor

pytestmark = [
    pytest.mark.hil_only(reason="peripheral"),
    # A fixture failure here is a failed physical library capability, not a
    # runner outage: the fixture has already established serial and mesh I/O.
    pytest.mark.failure_class("real_bug"),
]


def _gateway_settings():
    if os.environ.get("ALTERIOM_HIL_MODE") != "hardware":
        pytest.skip("gateway validation requires physical Wi-Fi radios")
    ssid = os.environ.get("ALTERIOM_HIL_WIFI_SSID")
    password_file = os.environ.get("ALTERIOM_HIL_WIFI_PASSWORD_FILE")
    endpoint = os.environ.get("ALTERIOM_HIL_GATEWAY_ENDPOINT")
    if not ssid or not password_file or not endpoint:
        pytest.skip("gateway test network is not configured")
    try:
        password = Path(password_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        pytest.skip(f"cannot read gateway Wi-Fi password: {exc}")
    return ssid, password, endpoint.rstrip("/")


def _wait_for_peer(client, peer_node_id: int, timeout: float) -> set[int]:
    deadline = time.monotonic() + timeout
    peers: set[int] = set()
    while time.monotonic() < deadline:
        peers = set(client.node_list(timeout=10))
        if peer_node_id in peers:
            return peers
        time.sleep(2)
    return peers


def _restore_regular_mesh(clients, node_ids, attempts: int = 3) -> None:
    """Reboot all roles and require a complete post-transition topology.

    A bridge election can leave two internally valid subtrees after every
    device returns to station/AP mode.  Also, inexpensive USB-UART adapters can
    lose one reply while several boards reboot.  Retry the whole recovery
    sequence, but only return once every physical node reports every peer.
    """
    last: dict[str, set[int] | str] = {}
    for _ in range(attempts):
        # Together, not in turn: one at a time left the mesh split across
        # two channels for minutes, long enough for the nodes already back
        # to follow the ones still in gateway mode.
        BoardClient.restart_all_regular(clients, timeout=35)

        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            complete = True
            for board_id, client in clients.items():
                expected = {
                    node_id
                    for peer_id, node_id in node_ids.items()
                    if peer_id != board_id
                }
                try:
                    peers = set(client.node_list(timeout=10))
                    last[board_id] = peers
                except TimeoutWaitingFor as exc:
                    last[board_id] = str(exc).splitlines()[0]
                    complete = False
                    continue
                if not peers >= expected:
                    complete = False
            if complete and _delivery_works(clients, node_ids, last):
                return
            time.sleep(2)

    pytest.fail(f"regular mesh did not recover usable routing after the gateway phase: {last}")


def _delivery_works(clients, node_ids, last: dict) -> bool:
    """Can every node still deliver, not merely list its peers?

    ``node_list`` is an optimistic signal after a failover: it retains the
    complete topology while some of the rebuilt station/AP routes no longer
    carry traffic, which this module's own teardown comment warns about. A
    teardown that trusts it hands the next test file a mesh that looks healthy
    and drops messages, and the failure then lands on an unrelated test — a
    gateway defect reported as a mesh-formation defect.

    One acknowledged message per node is enough to tell the two apart, and it
    keeps the failure attributed to the phase that caused it.
    """
    board_ids = list(clients)
    for index, board_id in enumerate(board_ids):
        peer_id = board_ids[(index + 1) % len(board_ids)]
        if peer_id == board_id:
            continue
        try:
            if not clients[board_id].send_single(
                node_ids[peer_id], "post-gateway routing probe", ack=True
            ):
                last[board_id] = f"no ack from {peer_id} despite listing it as a peer"
                return False
        except TimeoutWaitingFor as exc:
            last[board_id] = str(exc).splitlines()[0]
            return False
    return True


def _can_serve_as_bridge(board_id, board_map) -> bool:
    """A bridge has mesh children behind it. The ESP8266 is specified as a
    leaf in a mesh this size and the agent runs it station-only, so it can
    neither bridge nor stand in as the failover backup; every ESP32 family
    can. Without a board map (simulation) every board qualifies."""
    if board_map is None:
        return True
    board = next((b for b in board_map if b.id == board_id), None)
    return board is None or board.target != "esp8266"


@pytest.fixture(scope="module")
def gateway_mesh(mesh, board_map):
    clients, node_ids = mesh
    if len(clients) < 2:
        pytest.skip("gateway validation needs a bridge and a regular node")
    ssid, password, endpoint = _gateway_settings()
    requested = os.environ.get("ALTERIOM_HIL_GATEWAY_BOARD")
    gateway_id = requested or next(
        (board_id for board_id in clients if _can_serve_as_bridge(board_id, board_map)),
        None,
    )
    if gateway_id is None:
        pytest.skip("gateway validation needs a board that can serve as a bridge")
    if gateway_id not in clients:
        pytest.fail(f"configured gateway board is absent: {gateway_id}")
    sender_id = next(board_id for board_id in clients if board_id != gateway_id)
    gateway = clients[gateway_id]
    sender = clients[sender_id]

    gateway_started = False
    try:
        state = gateway.start_gateway(ssid, password)
        gateway_started = True
        # initAsBridge returns after the station starts connecting.  Do not
        # mistake that asynchronous initial state for an association failure.
        association_deadline = time.monotonic() + 45
        while int(state["wifiStatus"]) != 3 and time.monotonic() < association_deadline:
            time.sleep(1)
            state = gateway.gateway_status(timeout=10)

        sender_state = None
        upstream_ready = (
            state["initialized"] is True
            and state["isBridge"] is True
            and int(state["wifiStatus"]) == 3
            and state["localIP"] != "0.0.0.0"
        )
        gateway_peers: set[int] = set()
        sender_peers: set[int] = set()
        converged = False
        if upstream_ready:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                try:
                    seen_gateway = _wait_for_peer(
                        gateway, node_ids[sender_id], timeout=10
                    )
                    seen_sender = _wait_for_peer(
                        sender, node_ids[gateway_id], timeout=10
                    )
                    both_see_each_other = (
                        node_ids[sender_id] in seen_gateway
                        and node_ids[gateway_id] in seen_sender
                    )
                    # Keep the converged observation once it happens. A poll
                    # taken while the mesh reorganises sees a partial topology,
                    # and `_wait_for_peer` returns what it last saw even when
                    # the peer never appeared. Overwriting a good reading with
                    # that is what failed the bridge assertion on a mesh that
                    # had in fact formed: the peers were there, the last
                    # snapshot was not.
                    if both_see_each_other:
                        gateway_peers, sender_peers = seen_gateway, seen_sender
                        converged = True
                    elif not converged:
                        gateway_peers, sender_peers = seen_gateway, seen_sender
                    if not converged:
                        continue
                    sender_state = sender.gateway_status(timeout=10)
                    if sender_state["hasInternet"]:
                        break
                except TimeoutWaitingFor:
                    pass
                time.sleep(2)

        yield {
            "clients": clients,
            "node_ids": node_ids,
            "board_map": board_map,
            "gateway_id": gateway_id,
            "gateway": gateway,
            "sender_id": sender_id,
            "sender": sender,
            "endpoint": endpoint,
            "gateway_state": state,
            "sender_state": sender_state,
            "gateway_peers": gateway_peers,
            "sender_peers": sender_peers,
            "upstream_ready": upstream_ready,
            "converged": converged,
        }
    finally:
        if gateway_started:
            # A failover exercise rebuilds several overlapping station/AP
            # routes.  node_list can briefly retain the complete topology even
            # when one of those routes is no longer usable.  Reboot every role
            # into the regular mesh so later feature tests start from clean
            # routing state rather than a stale post-election tree.
            _restore_regular_mesh(clients, node_ids)
        for client in clients.values():
            client.clear_pending()


def _require_upstream(gateway_mesh):
    if not gateway_mesh["upstream_ready"]:
        pytest.skip("blocked: bridge did not establish a usable upstream connection")
    if gateway_mesh["sender_state"] is None:
        pytest.skip("blocked: controlled bridge/sender mesh route did not converge")


@pytest.mark.capability("gateway.bridge")
def test_dedicated_bridge_has_upstream_wifi_and_mesh(gateway_mesh):
    state = gateway_mesh["gateway_state"]
    assert state["initialized"] is True
    assert state["isBridge"] is True
    assert int(state["wifiStatus"]) == 3  # Arduino WL_CONNECTED
    assert state["localIP"] != "0.0.0.0"
    assert 1 <= int(state["channel"]) <= 13
    # Named rather than a bare set-membership failure: "the mesh did not form"
    # and "the bridge lost a peer" are different problems and used to look
    # identical in the report.
    seen = sorted(gateway_mesh["gateway_peers"])
    expected = gateway_mesh["node_ids"][gateway_mesh["sender_id"]]
    assert gateway_mesh["converged"], (
        f"bridge and sender never saw each other within 120s: "
        f"expected {expected} among the bridge's peers, last saw {seen}"
    )


@pytest.mark.capability("gateway.discovery")
def test_regular_node_discovers_gateway_internet(gateway_mesh):
    _require_upstream(gateway_mesh)
    state = gateway_mesh["sender_state"]
    assert state is not None
    assert state["isBridge"] is False
    assert state["hasInternet"] is True
    assert state["hasLocalInternet"] is False


def _send_and_wait(gateway_mesh, tag: str, path: str, payload: str = "") -> dict:
    _require_upstream(gateway_mesh)
    sender = gateway_mesh["sender"]
    url = f"{gateway_mesh['endpoint']}{path}"
    message_id = sender.send_to_internet(tag, url, payload=payload)
    assert message_id > 0
    return sender.wait_internet_result(tag, timeout=90)


@pytest.mark.capability("internet.get")
def test_regular_node_relays_http_get_through_bridge(gateway_mesh):
    tag = f"get-{time.time_ns()}"
    result = _send_and_wait(gateway_mesh, tag, f"/status/200?tag={tag}")
    assert result["success"] is True
    assert result["httpStatus"] == 200


@pytest.mark.capability("internet.post")
def test_regular_node_relays_http_post_payload_through_bridge(gateway_mesh):
    tag = f"post-{time.time_ns()}"
    payload = json.dumps({"source": gateway_mesh["sender_id"], "value": 42})
    result = _send_and_wait(gateway_mesh, tag, f"/echo?tag={tag}", payload)
    assert result["success"] is True
    assert result["httpStatus"] == 200

    with urlopen(f"{gateway_mesh['endpoint']}/requests/{tag}", timeout=5) as response:
        observed = json.load(response)
    assert observed["method"] == "POST"
    assert json.loads(observed["body"]) == json.loads(payload)


@pytest.mark.capability("internet.errors")
def test_gateway_returns_honest_http_failure(gateway_mesh):
    tag = f"error-{time.time_ns()}"
    result = _send_and_wait(gateway_mesh, tag, f"/status/400?tag={tag}")
    assert result["success"] is False
    assert result["httpStatus"] == 400
    assert "400" in result["error"]


@pytest.mark.capability("internet.recovery")
def test_gateway_relays_again_after_upstream_request_failure(gateway_mesh):
    """A failed upstream transaction must not poison the relay data path."""
    failed_tag = f"recovery-fail-{time.time_ns()}"
    failed = _send_and_wait(
        gateway_mesh, failed_tag, f"/status/503?tag={failed_tag}"
    )
    assert failed["success"] is False
    assert failed["httpStatus"] == 503

    recovered_tag = f"recovery-ok-{time.time_ns()}"
    recovered = _send_and_wait(
        gateway_mesh, recovered_tag, f"/status/200?tag={recovered_tag}"
    )
    assert recovered["success"] is True
    assert recovered["httpStatus"] == 200


def _unreachable_targets(endpoint: str, tag: str) -> dict:
    """Destinations that fail *below* HTTP, where HTTPClient returns a negative
    code rather than a status.

    Every error case this module had before pointed at the probe and asked it
    for a status — 400, 503 — which is a positive int on the wire. The branch
    that handles a request which never reached a server had no hardware
    coverage at all, and that is where painlessMesh #446 lived.
    """
    host = urlparse(endpoint).hostname
    return {
        # TCP refused: the discard port has nothing listening on it.
        "refused": f"http://{host}:9/refused?tag={tag}",
        # DNS failure: .invalid is reserved and never resolves (RFC 2606).
        "unresolvable": f"http://probe.invalid/nowhere?tag={tag}",
        # Read timeout: the probe stalls well past GATEWAY_HTTP_TIMEOUT_MS,
        # which is NODE_TIMEOUT/5 — two seconds at the library's defaults.
        "timeout": f"{endpoint}/delay/5?tag={tag}",
    }


@pytest.mark.capability("gateway.local_internet")
def test_bridge_reports_internet_on_its_own_uplink(gateway_mesh):
    """painlessMesh #445.

    `sendToInternet()` short-circuits on `hasLocalInternet()` so a node with its
    own uplink serves the request itself instead of routing it to a peer. That
    flag is driven only by the Internet health checker, and `initAsBridge()`
    never started it — only `initAsSharedGateway()` did. On a bridge the flag
    was therefore false for the life of the node.

    This suite did assert on `hasLocalInternet`, but only on the *sender*, where
    it asserts False. Nothing here has ever asserted that a bridge reports True,
    which is exactly the flag that was never being set.
    """
    _require_upstream(gateway_mesh)
    gateway = gateway_mesh["gateway"]

    # The checker's first run happens before the station has associated, and it
    # then waits a full interval (30 s by default). Allow three.
    deadline = time.monotonic() + 100
    state = gateway.gateway_status(timeout=10)
    while not state["hasLocalInternet"] and time.monotonic() < deadline:
        time.sleep(2)
        state = gateway.gateway_status(timeout=10)

    assert state["isBridge"] is True, state
    assert state["hasLocalInternet"] is True, (
        "the bridge has an associated station and an IP but never reported "
        f"local Internet, so sendToInternet() cannot use its own uplink: {state}"
    )


@pytest.mark.capability("internet.self", "internet.get")
def test_bridge_relays_its_own_request(gateway_mesh):
    """painlessMesh #445, the user-visible half.

    Every other test in this module makes the *sender* the originator. The
    bridge is never asked to call `sendToInternet()` itself, which is what the
    reporter's sketch did and what failed with "No active mesh connections".

    The bridge is the only gateway in this mesh, so a successful relay here
    cannot have gone through a peer: `getPrimaryBridge()` would return the
    bridge itself and there is no route from a node to itself. Success proves
    the local path ran.
    """
    _require_upstream(gateway_mesh)
    gateway = gateway_mesh["gateway"]
    tag = f"self-{time.time_ns()}"

    message_id = gateway.send_to_internet(
        tag, f"{gateway_mesh['endpoint']}/status/200?tag={tag}"
    )
    assert message_id > 0
    result = gateway.wait_internet_result(tag, timeout=90)

    assert result["success"] is True, result
    assert result["httpStatus"] == 200, result

    with urlopen(f"{gateway_mesh['endpoint']}/requests/{tag}", timeout=5) as response:
        observed = json.load(response)
    assert observed["tag"] == tag


@pytest.mark.capability("internet.transport_errors")
@pytest.mark.parametrize("failure", ["refused", "unresolvable", "timeout"])
def test_transport_failure_is_reported_as_a_network_error(gateway_mesh, failure):
    """painlessMesh #446.

    The gateway stored `HTTPClient`'s `int` result in a `uint16_t`, so
    `HTTPC_ERROR_CONNECTION_REFUSED` (-1) wrapped to 65535 — a value that passes
    `httpCode > 0`. Three things followed: the node was told "HTTP 65535", the
    `errorToString()` branch was unreachable, and `handleGatewayAck()` filed a
    retryable network failure as a permanent HTTP status.

    A transport failure must arrive as status 0 — the value the origin node
    already treats as retryable — carrying the real reason in the error string.
    """
    _require_upstream(gateway_mesh)
    sender = gateway_mesh["sender"]
    tag = f"{failure}-{time.time_ns()}"
    url = _unreachable_targets(gateway_mesh["endpoint"], tag)[failure]

    message_id = sender.send_to_internet(tag, url)
    assert message_id > 0
    # Generous: the library retries with exponential backoff before reporting,
    # and the timeout case spends GATEWAY_HTTP_TIMEOUT_MS on every attempt.
    result = sender.wait_internet_result(tag, timeout=120)

    assert result["success"] is False, result
    assert result["httpStatus"] == 0, (
        "a request that never reached a server has no HTTP status; 65535 is the "
        f"truncated -1 from issue #446: {result}"
    )
    assert result["error"], f"a transport failure must carry a real reason: {result}"
    assert "65535" not in str(result["error"]), result


@pytest.mark.capability("internet.transport_errors", "internet.recovery")
def test_relay_still_works_after_a_transport_failure(gateway_mesh):
    """A failure below HTTP must not poison the relay.

    The module already proves this for an HTTP-level failure (503). A transport
    failure takes a different path through the gateway — no `http.begin()`
    success, no response to read — so it needs its own recovery evidence.
    """
    _require_upstream(gateway_mesh)
    sender = gateway_mesh["sender"]

    failed_tag = f"transport-fail-{time.time_ns()}"
    failed = sender.send_to_internet(
        failed_tag,
        _unreachable_targets(gateway_mesh["endpoint"], failed_tag)["refused"],
    )
    assert failed > 0
    failure = sender.wait_internet_result(failed_tag, timeout=120)
    assert failure["success"] is False, failure
    assert failure["httpStatus"] == 0, failure

    recovered_tag = f"transport-ok-{time.time_ns()}"
    recovered = _send_and_wait(
        gateway_mesh, recovered_tag, f"/status/200?tag={recovered_tag}"
    )
    assert recovered["success"] is True, recovered
    assert recovered["httpStatus"] == 200, recovered


@pytest.mark.capability("gateway.failover", "internet.recovery")
def test_backup_gateway_carries_traffic_after_primary_leaves(gateway_mesh):
    """Remove the active bridge and prove a second physical bridge carries data."""
    clients = gateway_mesh["clients"]
    if len(clients) < 3:
        pytest.skip("gateway failover needs two bridges and one regular node")
    _require_upstream(gateway_mesh)

    backup_id = next(
        (
            board_id
            for board_id in clients
            if board_id not in {gateway_mesh["gateway_id"], gateway_mesh["sender_id"]}
            and _can_serve_as_bridge(board_id, gateway_mesh.get("board_map"))
        ),
        None,
    )
    if backup_id is None:
        pytest.skip("gateway failover needs a second board that can serve as a bridge")
    backup = clients[backup_id]
    ssid, password, _ = _gateway_settings()
    backup_started = False
    try:
        backup_state = backup.start_gateway_failover(ssid, password)
        backup_started = True
        assert backup_state["isBridge"] is False, backup_id

        backup_node = gateway_mesh["node_ids"][backup_id]
        sender = gateway_mesh["sender"]
        sender_peers = _wait_for_peer(sender, backup_node, timeout=120)
        assert backup_node in sender_peers

        control_tag = f"failover-before-{time.time_ns()}"
        control = _send_and_wait(
            gateway_mesh, control_tag, f"/status/200?tag={control_tag}"
        )
        assert control["success"] is True

        # This reboots the primary into a regular mesh node, removing its
        # upstream route without relying on unsupported per-port hub power.
        gateway_mesh["gateway"].start_regular_mesh(timeout=35)

        # Election/promotion and regular-node route convergence are separate
        # asynchronous phases.  Do not spend the sender's reconnect budget
        # while the candidate is still waiting for the election monitor, and
        # avoid continuously polling both radios during that transition.
        promotion_deadline = time.monotonic() + 120
        while time.monotonic() < promotion_deadline:
            backup_state = backup.gateway_status(timeout=10)
            if (
                backup_state["isBridge"] is True
                and backup_state["hasInternet"] is True
            ):
                break
            time.sleep(2)
        assert backup_state["isBridge"] is True, backup_state
        assert backup_state["hasInternet"] is True, backup_state

        discovery_deadline = time.monotonic() + 120
        sender_state = sender.gateway_status(timeout=10)
        while (
            int(sender_state["primaryGateway"]) != backup_node
            and time.monotonic() < discovery_deadline
        ):
            time.sleep(2)
            sender_state = sender.gateway_status(timeout=10)
        assert int(sender_state["primaryGateway"]) == backup_node, sender_state

        # Gateway discovery can precede readiness of the newly rebuilt data
        # path by one transaction.  Use unique request IDs and require a real
        # successful relay within a small bounded recovery window.
        recovered = None
        for attempt in range(1, 4):
            recovered_tag = f"failover-after-{attempt}-{time.time_ns()}"
            recovered = _send_and_wait(
                gateway_mesh, recovered_tag, f"/status/200?tag={recovered_tag}"
            )
            if recovered["success"] is True:
                break
            time.sleep(3)
            sender_state = sender.gateway_status(timeout=10)
            assert int(sender_state["primaryGateway"]) == backup_node, sender_state
        assert recovered is not None
        assert recovered["success"] is True
        assert recovered["httpStatus"] == 200, recovered
    finally:
        if backup_started:
            backup.start_regular_mesh(timeout=35)


# ---------------------------------------------------------------------------
# painlessMesh #450
# ---------------------------------------------------------------------------

# The bridge health checker's periodic interval at the library's defaults.
# initAsBridge() takes no config to shorten it, so the row below has to live
# inside it.
HEALTH_CHECK_INTERVAL_S = 30.0


def _wait_for_relay_ready(gateway_mesh, timeout: float = 120.0, strict: bool = True):
    """Block until the sender can relay through the (possibly rebooted) bridge.

    The module fixture converged the mesh once, at setup. A row that reboots
    the bridge leaves the sender to rejoin the bridge's channel and rediscover
    its Internet advertisement -- the same two phases the fixture allows 120 s
    for -- so the rows after it must poll the live state, not the fixture's
    cached picture of it. With ``strict`` the wait fails the row; without it
    (a teardown after a skip) it only gives the mesh its chance to settle.
    """
    gateway_node = gateway_mesh["node_ids"][gateway_mesh["gateway_id"]]
    sender = gateway_mesh["sender"]
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            if gateway_node in set(sender.node_list(timeout=10)):
                last = sender.gateway_status(timeout=10)
                if last["hasInternet"]:
                    return last
        except TimeoutWaitingFor:
            pass
        time.sleep(2)
    if strict:
        pytest.fail(
            "the sender did not regain a route to a bridge with Internet within "
            f"{timeout:.0f}s of the bridge restarting: {last}"
        )
    return last


@pytest.mark.capability("internet.self.first_send", "internet.self")
def test_bridge_relays_its_own_request_right_after_association(gateway_mesh):
    """painlessMesh #450, the timing half.

    The #445 rows above wait up to 100 s for `hasLocalInternet` before the
    bridge sends anything. The reporter's sketch sent 8 s after boot. Both the
    library and this suite had a hole the same shape: `initAsBridge()` arms the
    health checker one line after it re-issues `WiFi.begin()`, the first probe
    runs on the next scheduler pass while the station is still associating and
    fails, and the next probe is a full interval (30 s) away. Every send in
    that window fell through to the mesh path and was refused with "No active
    mesh connections".

    The window is therefore anchored to `initAsBridge()`, not to association:
    the send has to leave before the periodic probe that follows the failed
    one, or the unfixed library passes on its own once that probe has run.
    `start_gateway()` returns on the event the agent emits right after
    `initAsBridge()`, so its return is the anchor, and a send that cannot leave
    inside the interval is a skipped row, never a passed one. The assertion
    that fails on the unfixed library is `message_id > 0`.
    """
    _require_upstream(gateway_mesh)
    gateway = gateway_mesh["gateway"]
    ssid, password, endpoint = _gateway_settings()

    try:
        state = gateway.start_gateway(ssid, password)
        initialized_at = time.monotonic()
        association_deadline = initialized_at + 45
        while int(state["wifiStatus"]) != 3 and time.monotonic() < association_deadline:
            time.sleep(1)
            state = gateway.gateway_status(timeout=10)
        if int(state["wifiStatus"]) != 3 or state["localIP"] == "0.0.0.0":
            pytest.skip(f"blocked: the bridge did not re-associate upstream: {state}")

        # A margin under the interval covers serial latency and the send
        # itself. Association slower than that is a rig condition, not a
        # library verdict: the window has closed, and a send now would tell
        # nothing about the code under test.
        if time.monotonic() - initialized_at > HEALTH_CHECK_INTERVAL_S - 5.0:
            pytest.skip(
                "blocked: association took longer than the first health-check "
                "interval, so the window this row measures had already closed"
            )

        tag = f"first-send-{time.time_ns()}"
        message_id = gateway.send_to_internet(tag, f"{endpoint}/status/200?tag={tag}")
        sent_after = time.monotonic() - initialized_at
        assert sent_after < HEALTH_CHECK_INTERVAL_S, (
            f"the send left {sent_after:.1f}s after initAsBridge(), past the first "
            "health-check interval, so this row no longer exercises the window"
        )
        assert message_id > 0, (
            "sendToInternet() refused the bridge's own request right after "
            "association -- painlessMesh #450: the first health probe ran during "
            "station association, failed, and nothing re-probed before the send"
        )

        result = gateway.wait_internet_result(tag, timeout=90)
        assert result["success"] is True, result
        assert result["httpStatus"] == 200, result

        with urlopen(f"{endpoint}/requests/{tag}", timeout=5) as response:
            observed = json.load(response)
        assert observed["tag"] == tag
    finally:
        # The bridge rebooted. Hand the rows that follow a converged mesh; a
        # skip above must stay a skip, so this wait does not fail the row.
        _wait_for_relay_ready(gateway_mesh, strict=False)


# profile -> delivered. Mirrors runner/gateway_probe_server.py, which mirrors
# painlessMesh's test/mock-http-server/server.py; the two 208 profiles are the
# two readings of the reporter's HTTP 208.
CALLMEBOT_PROFILES = {
    "queued": True,
    "ratelimit-203": False,
    "ratelimit-201": False,
    "queued-208": True,
    "error-208": False,
}


@pytest.mark.capability("internet.service_semantics")
@pytest.mark.parametrize("profile", sorted(CALLMEBOT_PROFILES))
def test_gateway_verdict_matches_service_delivery(gateway_mesh, profile):
    """painlessMesh #450, the verdict half.

    The reporter's bridge reached CallMeBot, got HTTP 208, and reported
    "Ambiguous response ... not actual delivery". CallMeBot does not encode
    delivery in the status: probed while triaging, it answered a rate-limit
    refusal with 203 and with 201 -- the same HTML error page under both -- and
    201 is on the library's success list. The gateway also discards the body,
    so the origin node is told a number and nothing else.

    Every row above asks the probe for a status and checks that the gateway
    repeated it. This one asks the probe what it *did* and checks that the
    gateway's verdict agrees, and that a refusal reaches the origin node with
    the service's own reason attached.
    """
    _require_upstream(gateway_mesh)
    # The row before this one rebooted the bridge; the fixture's cached state
    # says nothing about whether the sender has rejoined it yet.
    _wait_for_relay_ready(gateway_mesh)
    sender = gateway_mesh["sender"]
    endpoint = gateway_mesh["endpoint"]
    tag = f"{profile}-{time.time_ns()}"
    url = (
        f"{endpoint}/callmebot/whatsapp.php"
        f"?phone=%2B10000000000&apikey={profile}&text={tag}"
    )

    message_id = sender.send_to_internet(tag, url)
    assert message_id > 0
    # 203 is retried with backoff before the library reports; allow for it.
    result = sender.wait_internet_result(tag, timeout=120)

    with urlopen(f"{endpoint}/requests/{tag}", timeout=5) as response:
        observed = json.load(response)
    assert observed["tag"] == tag, observed
    delivered = observed["delivered"]

    assert result["success"] is delivered, (
        f"the service {'delivered' if delivered else 'refused'} the message "
        f"(HTTP {observed['status']}, body {observed['response']!r}) but the "
        f"gateway reported {result}"
    )
    if not delivered:
        assert "Too many requests" in str(result["error"]), (
            "a refusal must carry the service's reason to the origin node, "
            f"not only a status code: {result}"
        )
