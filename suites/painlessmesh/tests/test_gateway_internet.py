"""Dedicated-bridge and sendToInternet validation on the physical farm."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import urlopen

import pytest

from alteriom_hil import providers
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

    A transport failure must arrive as status 0 — no server answered — carrying
    the real reason in the error string. Whether the library retried it is a
    separate question, answered by `test_a_request_that_may_have_arrived_is_issued_once`.
    """
    _require_upstream(gateway_mesh)
    sender = gateway_mesh["sender"]
    tag = f"{failure}-{time.time_ns()}"
    url = _unreachable_targets(gateway_mesh["endpoint"], tag)[failure]

    message_id = sender.send_to_internet(tag, url)
    assert message_id > 0
    # Generous: a refused connection is retried with exponential backoff
    # before the library reports, and before painlessMesh #464 the timeout
    # case was too, spending GATEWAY_HTTP_TIMEOUT_MS on every attempt.
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


def _ledger(endpoint: str, tag: str) -> dict:
    with urlopen(f"{endpoint}/requests/{tag}", timeout=5) as response:
        observed = json.load(response)
    assert observed["tag"] == tag, observed
    return observed


def _probe_features(endpoint: str) -> set[str]:
    """The features the rig's gateway probe names on /health (none for a probe
    from before they were named)."""
    try:
        with urlopen(f"{endpoint}/health", timeout=5) as response:
            payload = json.load(response)
    except (OSError, ValueError) as exc:
        pytest.skip(f"blocked: the rig's gateway probe did not answer /health: {exc}")
    features = payload.get("features") if isinstance(payload, dict) else None
    return set(features) if isinstance(features, list) else set()


def _require_probe_features(endpoint: str, *needed: str) -> None:
    """Skip, as a rig without the feature, when the probe does not offer one.

    The probe is installed with the farm release, so this only trips on a rig
    whose probe was not refreshed -- and that must read as a rig condition,
    not as a painlessMesh failure under this module's `real_bug` class.
    """
    missing = sorted(set(needed) - _probe_features(endpoint))
    if missing:
        pytest.skip(
            f"blocked: the rig's gateway probe does not offer {', '.join(missing)}; "
            "it predates the farm release this suite came from"
        )


def _has_result_api(result: dict) -> bool:
    """True when the agent was built against a painlessMesh with the
    InternetResult callback (#464), which reports `attempts` and `response`."""
    return "attempts" in result


@pytest.mark.capability("internet.single_delivery")
def test_a_request_that_may_have_arrived_is_issued_once(gateway_mesh):
    """painlessMesh #464.

    A destination that answers after GATEWAY_HTTP_TIMEOUT_MS has the request:
    HTTPClient gives up reading, not sending. The library counted that read
    timeout as a network error and retried it three times, and on this rig
    (gate run 34670860918) one send reached the probe four times. For a
    message service that is four messages -- or one and three refusals, which
    is what the CallMeBot reports in painlessMesh #450, #452 and #463 look
    like from the user's side.

    The ledger counts every request the probe saw under the tag, so this row
    holds the library to one call, one request.
    """
    _require_upstream(gateway_mesh)
    sender = gateway_mesh["sender"]
    endpoint = gateway_mesh["endpoint"]
    _require_probe_features(endpoint, "ledger.count", "ledger.request_ids")
    tag = f"once-{time.time_ns()}"

    message_id = sender.send_to_internet(tag, _unreachable_targets(endpoint, tag)["timeout"])
    assert message_id > 0
    result = sender.wait_internet_result(tag, timeout=120)
    assert result["success"] is False, result
    assert result["httpStatus"] == 0, result

    observed = _ledger(endpoint, tag)
    assert observed["count"] == 1, (
        f"one sendToInternet() call reached the destination {observed['count']} "
        "times: a request that timed out waiting for the reply was resent, and "
        f"the server already had it (painlessMesh #464). Result: {result}"
    )
    if _has_result_api(result):
        assert result["attempts"] == 1, result
        assert result["retryable"] is False, result
        assert "may have reached the server" in str(result["error"]), result
        assert observed["request_ids"], (
            f"the gateway sent no X-Request-Id: {observed}"
        )


# The Retry-After the probe asks for: longer than the library's first backoff
# (1 s), so a retry that ignores the header arrives visibly early.
RETRY_AFTER_S = 3


@pytest.mark.capability("internet.retry_after", "internet.recovery")
def test_retry_after_is_honoured_and_the_retry_is_the_same_request(gateway_mesh):
    """painlessMesh #464.

    429 is the one refusal a retry is for: the server says it did not take the
    request, and when to come back. The probe refuses the first request with
    Retry-After: 3 and accepts the next, recording how long after the first it
    came. The retry must wait, and it must carry the same request id -- so a
    service that honours Idempotency-Key, and this ledger, can tell a retry
    from a second message.
    """
    _require_upstream(gateway_mesh)
    sender = gateway_mesh["sender"]
    endpoint = gateway_mesh["endpoint"]
    _require_probe_features(endpoint, "retry_after", "ledger.count", "ledger.request_ids")
    tag = f"retry-after-{time.time_ns()}"

    message_id = sender.send_to_internet(tag, f"{endpoint}/retry-after/{RETRY_AFTER_S}?tag={tag}")
    assert message_id > 0
    result = sender.wait_internet_result(tag, timeout=120)
    assert result["success"] is True, result
    assert result["httpStatus"] == 200, result

    observed = _ledger(endpoint, tag)
    assert observed["count"] == 2, (
        f"expected the refused request and one retry, saw {observed['count']}: {observed}"
    )
    assert observed["early"] is False, (
        f"the retry came {observed['waited_s']}s after the 429, sooner than "
        f"Retry-After: {RETRY_AFTER_S} asked (painlessMesh #464): {observed}"
    )
    assert len(observed["request_ids"]) == 1, (
        "every attempt at one call must carry the same X-Request-Id, and one "
        f"must be sent at all (painlessMesh #464): {observed}"
    )
    if _has_result_api(result):
        assert result["attempts"] == 2, result


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
                # Its gateway, specifically: a node the failover row demoted
                # stays in the sender's list for the library's 60 s bridge
                # timeout and can win on RSSI. Rows that measure something
                # else must not depend on how that plays out; the stale case
                # has its own row, test_a_rebooted_bridge_answers_...
                if last["hasInternet"] and int(last["primaryGateway"]) == gateway_node:
                    return last
        except TimeoutWaitingFor:
            pass
        time.sleep(2)
    if strict:
        pytest.fail(
            "the sender did not come to route through the live bridge within "
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


# profile -> (delivered, words the application must be able to read in the
# reply). Mirrors runner/gateway_probe_server.py, which mirrors painlessMesh's
# test/mock-http-server/server.py. The 208 is painlessMesh #452's field
# finding: CallMeBot answered it to a message that never arrived, so no body
# makes it a delivery -- `queued-208` carries the delivered profile's own body.
# Each phrase is unique to the response entity: "Already Reported" is also the
# HTTP reason phrase for 208, which a gateway could echo without ever reading
# the body, so it is not used.
CALLMEBOT_PROFILES = {
    "queued": (True, "Message queued"),
    "ratelimit-203": (False, "Too many requests"),
    "ratelimit-201": (False, "Too many requests"),
    "unverified-208": (False, "never arrived"),
    "queued-208": (False, "Message queued"),
    # painlessMesh #463: HTTP 200, a request echoed back at length, and the
    # verdict only in the last bytes -- past the head a gateway keeps.
    "paused-after-echo": (False, "Account is Paused"),
}

# Profiles a probe serves only from the release that added them.
CALLMEBOT_PROFILE_FEATURES = {"paused-after-echo": "callmebot.paused_after_echo"}

# What HTTP, and so the library, calls a success.
HTTP_SUCCESS = {200, 201, 202, 204}


@pytest.mark.capability("internet.service_semantics", "internet.single_delivery")
@pytest.mark.parametrize("profile", sorted(CALLMEBOT_PROFILES))
def test_service_reply_reaches_the_application_intact(gateway_mesh, profile):
    """painlessMesh #450, #452, #464.

    CallMeBot does not encode delivery in its status: it answers a rate-limit
    refusal with 203 and with 201 -- the same HTML page under both -- and 208
    to messages that never arrive. 2.0.3 matched CallMeBot's wording inside the
    gateway. Since #464 the library applies HTTP's meaning of the status and
    hands the application the reply, and the application -- the
    sendToInternet example's callmebot.h -- decides what CallMeBot meant; that
    reading is tested against these same profiles on the desktop.

    So on hardware this row holds the library to what only the physical relay
    can show: the service's status and its own words reach the application
    through the mesh, a reply is never resent, and the probe's ledger is the
    ground truth the application's reading is compared with. An agent built
    against a library older than #464 is held to that library's contract
    instead: its verdict matched delivery and carried the words on failure.
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

    if profile in CALLMEBOT_PROFILE_FEATURES:
        _require_probe_features(endpoint, CALLMEBOT_PROFILE_FEATURES[profile])

    message_id = sender.send_to_internet(tag, url)
    assert message_id > 0
    # Before #464 a 203 was retried with backoff before the library reported.
    result = sender.wait_internet_result(tag, timeout=120)

    observed = _ledger(endpoint, tag)
    delivered = observed["delivered"]
    expected_delivered, phrase = CALLMEBOT_PROFILES[profile]
    assert delivered is expected_delivered, (
        f"the probe's ledger disagrees with this suite's table for {profile}: "
        f"{observed}"
    )

    if not _has_result_api(result):
        assert result["success"] is delivered, (
            f"the service {'delivered' if delivered else 'did not deliver'} the "
            f"message (HTTP {observed['status']}, body {observed['response']!r}) "
            f"but the gateway reported {result}"
        )
        if not delivered:
            assert phrase in str(result["error"]), (
                "a non-delivery must carry the service's words to the origin node, "
                f"not only a status code: expected {phrase!r} in {result}"
            )
        return

    assert result["httpStatus"] == observed["status"], (
        f"the application was told HTTP {result['httpStatus']}, the service "
        f"answered {observed['status']}: {result}"
    )
    assert result["success"] is (observed["status"] in HTTP_SUCCESS), (
        f"success must mean what HTTP says for {observed['status']}: {result}"
    )
    assert phrase in str(result["response"]), (
        "the application must receive the service's own words to decide what "
        f"its reply meant: expected {phrase!r} in {result}"
    )
    assert result["attempts"] == 1, (
        f"the service answered, so the request must not be resent (painlessMesh #464): {result}"
    )
    # The ledger's own count is the stronger evidence, where the probe keeps one.
    if "ledger.count" in _probe_features(endpoint):
        assert observed["count"] == 1, (
            "the service answered, so the request must not be resent "
            f"(painlessMesh #464): ledger {observed}, result {result}"
        )


def _real_callmebot_link(redactor):
    """The rig's own CallMeBot link and whether this run may spend a message
    on it, or a skip saying why not. Nothing here touches hardware, so a rig
    that is not configured skips before the mesh is waited on."""
    url_file = os.environ.get(providers.ENV_URL_FILE)
    if not url_file:
        pytest.skip(
            "the rig has no CallMeBot link configured "
            "(alteriom-hil-admin providers set callmebot)"
        )
    try:
        link = providers.load_callmebot_link(url_file)
    except providers.ProviderError as exc:
        pytest.skip(f"blocked: the rig's CallMeBot link is not usable: {redactor.scrub(str(exc))}")
    send = os.environ.get(providers.ENV_SEND) or providers.DEFAULT_CALLMEBOT["send"]
    if send == "never":
        pytest.skip("rig parameter providers.callmebot.send is never")
    if send == "release" and os.environ.get(providers.ENV_RUN_KIND) != "release":
        pytest.skip("the rig sends real messages only for a release build")
    if send not in providers.SEND_POLICIES:
        pytest.skip(f"blocked: rig parameter providers.callmebot.send is not one of {providers.SEND_POLICIES}")
    return link


def _fail_scrubbed(redactor, message: str):
    """Fail with a message the redactor has seen, and without a Python
    traceback: pytest's assertion rewriting prints the operands, and the
    result dict carries the service's reply, which echoes the number."""
    pytest.fail(redactor.scrub(message), pytrace=False)


@pytest.mark.capability("internet.provider.callmebot")
def test_real_callmebot_accepts_a_message_through_the_mesh(gateway_mesh):
    """The real CallMeBot API, reached from a regular node through the mesh.

    Every other row here talks to the rig's own probe, which proves the
    library carries a service's reply intact. Only the real service can say
    it accepts what the library actually sends -- the https request through
    the gateway, the encoding of the text, the headers. That is the path the
    CallMeBot reports in painlessMesh #450, #452 and #463 took.

    It uses the rig owner's own link, stored on the rig (docs/providers.md),
    so it runs only where the owner said: never, on a release build (the
    default), or always; and never more than the rig's daily budget. A
    refusal that is CallMeBot's state -- rate limited, account paused -- is a
    skip: it says nothing about the library. The reply is read with the rules
    of painlessMesh's examples/sendToInternet/callmebot.h, which is what a
    user's sketch reads it with.

    Nothing this row prints may carry the link: every message goes through
    the rig's redactor, and the farm scrubs the evidence again after the run.
    """
    redactor = providers.Redactor.from_env()
    link = _real_callmebot_link(redactor)
    _require_upstream(gateway_mesh)
    # The rows before this one reboot the bridge.
    _wait_for_relay_ready(gateway_mesh)

    max_per_day = os.environ.get(providers.ENV_MAX_PER_DAY) or str(providers.DEFAULT_CALLMEBOT["max_per_day"])
    try:
        allowance = int(max_per_day)
    except ValueError:
        pytest.skip(f"blocked: rig parameter providers.callmebot.max_per_day is not an integer: {max_per_day!r}")
    budget = providers.budget_file_for(os.environ)
    try:
        allowed = providers.consume_budget(budget, allowance)
    except OSError as exc:
        pytest.skip(f"blocked: cannot record today's CallMeBot budget in {budget}: {exc.strerror or exc}")
    if not allowed:
        pytest.skip(f"today's CallMeBot budget ({allowance}) on this rig is used")

    sender = gateway_mesh["sender"]
    tag = f"callmebot-{time.time_ns()}"
    revision = (os.environ.get("PAINLESSMESH_REF") or os.environ.get("HIL_FIRMWARE_SHA") or "")[:12]
    text = f"painlessMesh HIL {revision} {tag}"
    failure = None
    try:
        message_id = sender.send_to_internet(tag, providers.message_url(link, text))
        # A refusal before #464 was retried with backoff before it reported.
        result = sender.wait_internet_result(tag, timeout=120) if message_id > 0 else None
    except TimeoutWaitingFor as exc:
        # The exception carries the board's last serial lines, which can hold
        # the URL. Failed outside the handler, so the original is not chained.
        failure = f"no result from the sender: {exc}"
    if failure is not None:
        _fail_scrubbed(redactor, failure)
    if result is None:
        _fail_scrubbed(redactor, f"sendToInternet() refused the request (message id {message_id})")

    if not _has_result_api(result):
        pytest.skip("the HIL agent predates painlessMesh #464's InternetResult; it reports no attempts or reply")

    status = int(result.get("httpStatus") or 0)
    error = str(result.get("error") or "")
    response = str(result.get("response") or "")
    if providers.upstream_unreachable(status, error):
        pytest.skip(
            "blocked: the rig's gateway cannot reach "
            f"{providers.CALLMEBOT_HOST}: {redactor.scrub(error)}"
        )

    verdict = providers.judge_callmebot_reply(status, response)
    summary = (
        f"HTTP {status}, attempts {result.get('attempts')}, success {result.get('success')}, "
        f"error {error!r}, reply {response!r}"
    )
    # A reply that is not a 429 is one the service gave: resending it is a
    # second message (painlessMesh #464), whatever the reply said.
    if status != 429 and result.get("attempts") != 1:
        _fail_scrubbed(redactor, f"the service answered, so the request must not be resent: {summary}")
    if verdict == "rate_limited":
        pytest.skip("CallMeBot refused: rate limited (service state, not a library verdict)")
    if verdict == "account_paused":
        pytest.skip("CallMeBot refused: the account is paused (service state, not a library verdict)")
    if status != 200:
        _fail_scrubbed(redactor, f"CallMeBot did not answer HTTP 200 ({verdict}): {summary}")
    if verdict != "queued":
        _fail_scrubbed(redactor, f"CallMeBot's reply is not an accepted message ({verdict}): {summary}")


# The library trusts a bridge's last status for bridgeTimeoutMs, 60 s by default.
BRIDGE_TIMEOUT_S = 60.0


@pytest.mark.capability("gateway.stale_after_reboot", "internet.recovery")
def test_a_rebooted_bridge_answers_instead_of_going_silent(gateway_mesh):
    """A bridge that reboots as a regular node must not swallow requests.

    Found by this suite while validating painlessMesh 2.0.3: the failover row
    reboots its promoted backup as a regular node, a reboot announces nothing
    (only a bridge stepping down in-process sends `leaving`), and the sender
    kept routing to it for the library's 60 s bridge timeout. A regular node
    had no gateway handler, dropped each request without a reply, and the
    sender reported "Request timed out" 30 s later. Any crash, power loss or
    reflash of a bridge does the same.

    That rig state depended on which node won on RSSI. This row makes it
    deterministic: the sender's only gateway reboots as a regular node, and
    the sender sends while it still trusts the stale advertisement. On the
    unfixed library the request times out after 30 s; fixed, the ex-bridge
    answers, and the sender fails at once, naming it. Then the bridge comes
    back and the sender uses it again.
    """
    _require_upstream(gateway_mesh)
    _wait_for_relay_ready(gateway_mesh)
    gateway = gateway_mesh["gateway"]
    sender = gateway_mesh["sender"]
    bridge_node = gateway_mesh["node_ids"][gateway_mesh["gateway_id"]]
    ssid, password, endpoint = _gateway_settings()

    # Anchor the window on a fresh advertisement, so the reboot and rejoin
    # fit inside the 60 s the sender will still trust it. The agent reports
    # the age of the sender's record of its primary gateway; bridges advertise
    # every 30 s, so a status younger than FRESH_MS arrives within one
    # interval. (The first cut waited for the sender's "Bridge status
    # received" mesh_log line, which the host capture keeps in the raw log
    # and never delivers as an event, so it could not match on any run.)
    FRESH_MS = 3000
    anchor_deadline = time.monotonic() + 45
    state = sender.gateway_status(timeout=10)
    if "primaryGatewayAgeMs" not in state:
        pytest.fail(f"the HIL agent does not report primaryGatewayAgeMs: {state}")
    while not (
        int(state["primaryGateway"]) == bridge_node
        and 0 <= int(state["primaryGatewayAgeMs"]) <= FRESH_MS
    ):
        if time.monotonic() >= anchor_deadline:
            pytest.fail(
                "the sender did not hear a fresh status from the bridge within "
                f"45 s, although bridges advertise every 30 s: {state}"
            )
        time.sleep(0.5)
        state = sender.gateway_status(timeout=10)
    heard_at = time.monotonic() - int(state["primaryGatewayAgeMs"]) / 1000.0

    # A reboot into the regular role: nothing is announced.
    gateway.start_regular_mesh(timeout=35)

    window_closes = heard_at + BRIDGE_TIMEOUT_S - 10
    state = None
    while time.monotonic() < window_closes:
        try:
            if bridge_node in set(sender.node_list(timeout=10)):
                state = sender.gateway_status(timeout=10)
                break
        except TimeoutWaitingFor:
            pass
        time.sleep(1)
    if state is None or int(state["primaryGateway"]) != bridge_node:
        pytest.skip(
            "blocked: the ex-bridge did not rejoin while the sender still trusted "
            f"its last status, so the stale path was not exercised: {state}"
        )

    tag = f"stale-{time.time_ns()}"
    started = time.monotonic()
    message_id = sender.send_to_internet(tag, f"{endpoint}/status/200?tag={tag}")
    assert message_id > 0
    result = sender.wait_internet_result(tag, timeout=60)
    elapsed = time.monotonic() - started

    assert result["success"] is False, result
    assert "not an Internet gateway" in str(result["error"]), (
        "a node that is no longer a gateway must say so; waiting out the "
        f"request timeout is the bug: {result}"
    )
    assert elapsed < 15, (
        f"the answer took {elapsed:.1f}s; a silent drop is what takes 30 s: {result}"
    )
    try:
        urlopen(f"{endpoint}/requests/{tag}", timeout=5)
        pytest.fail("the probe saw a request no gateway should have made")
    except HTTPError as exc:
        assert exc.code == 404

    # The bridge returns, advertises, and the sender uses it again.
    restarted = gateway.start_gateway(ssid, password)
    association_deadline = time.monotonic() + 45
    while int(restarted["wifiStatus"]) != 3 and time.monotonic() < association_deadline:
        time.sleep(1)
        restarted = gateway.gateway_status(timeout=10)
    _wait_for_relay_ready(gateway_mesh)

    tag = f"stale-recovered-{time.time_ns()}"
    message_id = sender.send_to_internet(tag, f"{endpoint}/status/200?tag={tag}")
    assert message_id > 0
    result = sender.wait_internet_result(tag, timeout=90)
    assert result["success"] is True, result
    assert result["httpStatus"] == 200, result
