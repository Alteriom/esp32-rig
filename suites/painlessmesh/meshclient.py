"""painlessMesh's HIL agent, driven: the mesh, OTA and gateway verbs.

`alteriom_hil.protocol.BoardClient` is the transport every agent the rig talks
to shares -- newline-delimited JSON, `wait_for`, resets, `info` -- and knows
nothing of what a particular firmware can be asked. This is what painlessMesh's
agent (`firmware/`) can be asked, as methods on a client of it. It lived in
the HAL's `BoardClient` until the rig was separated from the project it was
built for; the suite's boards are built as this class by the
`board_client_class` fixture in `tests/conftest.py`.
"""

from __future__ import annotations

import base64
import hashlib
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from alteriom_hil.protocol import BoardClient, ProtocolError, TimeoutWaitingFor


class MeshBoardClient(BoardClient):
    def node_id(self, timeout: float = 10.0) -> int:
        return int(self.info(timeout)["nodeId"])

    def node_list(self, timeout: float = 10.0) -> list[int]:
        self.send_cmd("node_list")
        evt = self.wait_for(
            lambda e: e["evt"] == "node_list", "node_list reply", timeout
        )
        return [int(n) for n in evt["nodes"]]

    def send_single(
        self,
        dest: int,
        msg: str,
        ack: bool = False,
        ack_timeout_ms: int = 5000,
        priority: Optional[int] = None,
        timeout: float = 10.0,
    ) -> bool:
        if priority is not None and priority not in range(4):
            raise ValueError("priority must be 0..3")
        if ack and priority is not None:
            raise ValueError("painlessMesh does not combine priority and ack overloads")
        self.send_cmd(
            "send_single",
            dest=dest,
            msg=msg,
            ack=ack,
            ackTimeoutMs=ack_timeout_ms,
            priority=priority,
        )
        evt = self.wait_for(
            lambda e: e["evt"] == "send_result"
            or (
                ack
                and e["evt"] == "ack"
                and int(e.get("node", -1)) == int(dest)
            ),
            "send_result or delivery acknowledgement",
            timeout,
        )
        if evt["evt"] == "ack":
            # A delivery callback proves the command was accepted even when
            # USB noise damaged the preceding send_result frame. Preserve it
            # for the caller's explicit wait_ack() assertion.
            self._pending.append(evt)
            return True
        return bool(evt["ok"])

    def send_broadcast(
        self,
        msg: str,
        ack: bool = False,
        ack_timeout_ms: int = 5000,
        include_self: bool = False,
        priority: Optional[int] = None,
        timeout: float = 10.0,
    ) -> bool:
        if priority is not None and priority not in range(4):
            raise ValueError("priority must be 0..3")
        if ack and priority is not None:
            raise ValueError("painlessMesh does not combine priority and ack overloads")
        self.send_cmd(
            "send_broadcast",
            msg=msg,
            ack=ack,
            ackTimeoutMs=ack_timeout_ms,
            includeSelf=include_self,
            priority=priority,
        )
        evt = self.wait_for(
            lambda e: e["evt"] == "send_result", "send_result reply", timeout
        )
        return bool(evt["ok"])

    def stall(self, ms: int):
        """Make the agent stop servicing mesh.update() for ``ms`` — used to
        provoke real ACK timeouts without cutting power."""
        self.send_cmd("stall", ms=ms)

    def enable_ota_receiver(self, role: str, timeout: float = 45.0) -> dict:
        if not 1 <= len(role) <= 31:
            raise ValueError("OTA role must be 1..31 characters")
        self.clear_pending()
        self.send_cmd("ota_receive_enable", role=role)
        self.wait_for(
            lambda e: e["evt"] == "ota_receiver_restarting" and e["role"] == role,
            "OTA receiver restart acknowledgement",
            10.0,
        )
        self.capture.reopen(timeout=20.0)
        self.clear_pending()
        return self.wait_for(
            lambda e: e["evt"] == "boot",
            "OTA receiver boot",
            timeout,
        )

    def upload_ota_source(
        self, image: Path, role: str, chunk_size: int = 1536,
        timeout: float = 360.0,
    ) -> dict:
        content = image.read_bytes()
        md5 = hashlib.md5(content).hexdigest()
        self.send_cmd("ota_upload_begin", size=len(content), md5=md5, role=role)
        self.wait_for(
            lambda e: e["evt"] == "ota_upload_ready" and e.get("ok") is True,
            "OTA upload ready",
            15.0,
        )
        deadline = time.monotonic() + timeout
        offset = 0
        while offset < len(content):
            chunk = content[offset : offset + chunk_size]
            expected = offset + len(chunk)
            last_timeout = None
            for _ in range(3):
                self.send_cmd(
                    "ota_upload_chunk",
                    offset=offset,
                    length=len(chunk),
                    md5=hashlib.md5(chunk).hexdigest(),
                    data=base64.b64encode(chunk).decode("ascii"),
                )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    event = self.wait_for(
                        lambda e, start=offset, end=expected: e["evt"]
                        in {"ota_upload_chunk", "ota_upload_retry"}
                        and int(e["bytes"]) in {start, end},
                        f"OTA chunk acknowledgement ({expected}/{len(content)})",
                        min(remaining, 10.0),
                    )
                except TimeoutWaitingFor as exc:
                    last_timeout = exc
                    continue
                if event["evt"] == "ota_upload_chunk":
                    offset = expected
                    break
            else:
                if last_timeout is not None:
                    raise last_timeout
                raise ProtocolError(
                    f"[{self.board_id}] OTA chunk rejected after 3 attempts "
                    f"at offset {offset}"
                )
            if offset != expected:
                raise TimeoutWaitingFor(
                    f"OTA source upload ({offset}/{len(content)} bytes)",
                    self.board_id,
                    self.capture.raw_log,
                )
        self.send_cmd("ota_upload_finish")
        verified = self.wait_for(
            lambda e: e["evt"] == "ota_upload_verified" and e.get("ok") is True,
            "OTA source verification",
            30.0,
        )
        verified["md5"] = md5
        return verified

    def offer_ota(self, timeout: float = 15.0) -> dict:
        self.send_cmd("ota_offer")
        return self.wait_for(
            lambda e: e["evt"] == "ota_offered",
            "OTA offer",
            timeout,
        )

    def wait_ota_generation(self, generation: int, timeout: float = 300.0) -> dict:
        return self.wait_for(
            lambda e: e["evt"] == "boot"
            and int(e.get("otaGeneration", 0)) == generation,
            f"OTA generation {generation} boot",
            timeout,
        )

    def start_gateway(self, ssid: str, password: str, timeout: float = 45.0) -> dict:
        restart = self.send_cmd_awaiting(
            "gateway_start",
            lambda e: e["evt"] == "gateway_restarting",
            "gateway restart acknowledgement",
            10.0,
            ssid=ssid, password=password,
        )
        if int(restart["passwordLength"]) != len(password):
            raise ProtocolError("gateway password was truncated in transit")
        self.capture.reopen(timeout=20.0)
        self.clear_pending()
        return self.wait_for(
            lambda e: e["evt"] == "gateway_started",
            "gateway initialization",
            timeout,
        )

    def start_shared_gateway(
        self, ssid: str, password: str, health_endpoint: str, timeout: float = 60.0
    ) -> dict:
        endpoint = urlparse(health_endpoint)
        if not endpoint.hostname or not endpoint.port:
            raise ValueError("shared gateway health endpoint must include host and port")
        restart = self.send_cmd_awaiting(
            "shared_gateway_start",
            lambda e: e["evt"] == "shared_gateway_restarting",
            "shared gateway restart acknowledgement",
            10.0,
            ssid=ssid, password=password,
            healthHost=endpoint.hostname, healthPort=endpoint.port,
        )
        if int(restart["passwordLength"]) != len(password):
            raise ProtocolError("shared gateway password was truncated in transit")
        self.capture.reopen(timeout=20.0)
        self.clear_pending()
        return self.wait_for(
            lambda e: e["evt"] == "shared_gateway_started",
            "shared gateway initialization",
            timeout,
        )

    def start_gateway_failover(
        self, ssid: str, password: str, timeout: float = 45.0
    ) -> dict:
        restart = self.send_cmd_awaiting(
            "gateway_failover_start",
            lambda e: e["evt"] == "gateway_failover_restarting",
            "gateway failover restart acknowledgement",
            10.0,
            ssid=ssid, password=password,
        )
        if int(restart["passwordLength"]) != len(password):
            raise ProtocolError("gateway failover password was truncated in transit")
        self.capture.reopen(timeout=20.0)
        self.clear_pending()
        return self.wait_for(
            lambda e: e["evt"] == "gateway_failover_started",
            "gateway failover initialization",
            timeout,
        )

    @staticmethod
    def restart_all_regular(clients: dict, timeout: float = 35.0) -> dict:
        """Put every board back into the regular mesh at the same time.

        Done one board at a time, with up to ``timeout`` each, the change
        of role took minutes across the bank, and for all of that time the
        mesh was in two states at once: nodes already back on the mesh
        channel, and nodes still in gateway mode on the router's channel.
        painlessMesh's channel re-detection cannot tell such a straggler
        from a bridge that has moved except by how long it stays, and one
        that stays for minutes was followed — a node and its subtree left
        the mesh for a partition about to disappear. Restarted together,
        the mixed state lasts seconds. Each board has its own serial port,
        so the restarts are independent; the first failure is raised after
        every board has been given its chance.
        """
        from concurrent.futures import ThreadPoolExecutor

        def one(item):
            board_id, client = item
            return board_id, client.start_regular_mesh(timeout=timeout)

        with ThreadPoolExecutor(max_workers=max(1, len(clients))) as pool:
            futures = [pool.submit(one, item) for item in clients.items()]
            results, first_error = {}, None
            for future in futures:
                try:
                    board_id, state = future.result()
                    results[board_id] = state
                except Exception as exc:  # noqa: BLE001 - reported once, below
                    first_error = first_error or exc
        if first_error is not None:
            raise first_error
        return results

    def start_regular_mesh(self, timeout: float = 20.0) -> dict:
        self.send_cmd_awaiting(
            "mesh_start",
            lambda e: e["evt"] == "mesh_restarting",
            "regular mesh restart acknowledgement",
            10.0,
        )
        self.capture.reopen(timeout=20.0)
        self.clear_pending()
        return self.wait_for(
            lambda e: e["evt"] == "mesh_started", "regular mesh restart", timeout
        )

    def configure_mesh(self, prefix: str, password: str, timeout: float = 30.0) -> dict:
        if not 1 <= len(prefix) <= 31 or len(password) < 8:
            raise ValueError("mesh prefix must be 1..31 characters and password 8+ characters")
        restart = self.send_cmd_awaiting(
            "mesh_configure",
            lambda e: e["evt"] == "mesh_restarting",
            "isolated mesh restart acknowledgement",
            10.0,
            prefix=prefix, password=password,
        )
        if restart.get("meshPrefix") != prefix:
            raise ProtocolError("mesh prefix was truncated in transit")
        self.capture.reopen(timeout=20.0)
        self.clear_pending()
        state = self.wait_for(
            lambda e: e["evt"] == "mesh_started", "isolated mesh restart", timeout
        )
        # `mesh_started` means the mesh is up, not that the agent is ready to
        # answer: an ESP32-C5 takes about fifteen seconds from its restart to
        # reply to `info`, roughly ten of them after `mesh_started`. The ten
        # this allowed expired right at that edge, so the C5 failed here every
        # single run while every other family cleared it. Give the reply the
        # same budget as the restart it follows rather than a tighter one.
        active = self.info(timeout=timeout)
        if active.get("meshPrefix") != prefix:
            raise ProtocolError("board restarted with an unexpected mesh prefix")
        state["meshPrefix"] = active["meshPrefix"]
        return state

    def _forget_earlier_replies(self, evt: str) -> None:
        """Drop every ``evt`` event already received, read or not.

        For a state query the answer is the reply to *this* question. The
        query below resends after half its timeout, and a board that was only
        slow then answers both; the spare reply used to stay held back and
        answer the next query with the old state. On the rig it told
        `_wait_for_relay_ready` the sender had Internet through the bridge
        twenty seconds after the sender had forgotten that bridge, and the row
        sent into a node with no gateway (farm job 60e82d9c).
        """
        while True:
            evt_now = self.capture.next_event(timeout=0)
            if evt_now is None:
                break
            self._pending.append(evt_now)
        self._pending = [e for e in self._pending if e.get("evt") != evt]

    def gateway_status(self, timeout: float = 10.0) -> dict:
        last_timeout = None
        for _ in range(2):
            self._forget_earlier_replies("gateway_status")
            self.send_cmd("gateway_status")
            try:
                return self.wait_for(
                    lambda e: e["evt"] == "gateway_status",
                    "gateway status",
                    timeout / 2,
                )
            except TimeoutWaitingFor as exc:
                last_timeout = exc
        assert last_timeout is not None
        raise last_timeout

    def send_to_internet(
        self,
        tag: str,
        url: str,
        payload: str = "",
        priority: int = 2,
        timeout: float = 10.0,
    ) -> int:
        if priority not in range(4):
            raise ValueError("priority must be 0..3")
        self.send_cmd(
            "internet_send", tag=tag, url=url, payload=payload, priority=priority
        )
        event = self.wait_for(
            lambda e: e["evt"] == "internet_queued" and e["tag"] == tag,
            f"Internet request queued ({tag})",
            timeout,
        )
        return int(event["messageId"])

    def wait_internet_result(self, tag: str, timeout: float = 45.0) -> dict:
        return self.wait_for(
            lambda e: e["evt"] == "internet_result" and e["tag"] == tag,
            f"Internet result ({tag})",
            timeout,
        )

    def wait_ack(
        self, node: Optional[int] = None, timeout: float = 10.0
    ) -> dict:
        def match(e: dict) -> bool:
            if e["evt"] != "ack":
                return False
            return node is None or int(e["node"]) == node

        return self.wait_for(
            match, f"ack event (node={node})", timeout
        )

    def wait_recv(
        self, from_node: Optional[int] = None, timeout: float = 10.0
    ) -> dict:
        def match(e: dict) -> bool:
            if e["evt"] != "recv":
                return False
            return from_node is None or int(e["from"]) == from_node

        return self.wait_for(
            match, f"recv event (from={from_node})", timeout
        )

    # How long one node_list poll waits for its reply. A healthy agent answers
    # in milliseconds; this only matters while the board cannot read serial,
    # and painlessMesh's all-channel mesh scan blocks for about seven seconds.
    PEER_POLL_REPLY_TIMEOUT = 15.0

    def wait_for_peer(self, node_id: int, timeout: float = 3.0):
        """Poll node_list until ``node_id`` is one of this board's peers.

        A count is not the same question. `wait_mesh_size` is satisfied by
        any N peers, so a board can pass it while the one node a test is
        about to send to is missing -- which is how a 16 s mesh outage
        reached a delivery assertion and was reported as lost data.

        The default window is short on purpose. A peer that is briefly
        absent from a freshly-read list is a poll landing mid-sync; a peer
        that is gone for longer has actually left, and a test should say
        that rather than wait for it. painlessMesh takes about 15 s to
        rejoin after dropping its last uplink (`0.5 * SCAN_INTERVAL`, which
        its log calls "fast"), so anything in that class fails here with a
        message naming the peer instead of failing later as a delivery
        timeout.

        The window is about what the board *says*, so a poll the board is too
        busy to answer does not spend it. On farm run 34900496756 an
        ESP32-C3 listed every peer before and after, but its first poll landed
        in painlessMesh's blocking channel scan; the 3 s poll timed out and
        the pair fixture reported "saw no peers". A poll now waits up to
        ``PEER_POLL_REPLY_TIMEOUT`` for its reply and its answer is judged
        when it arrives; a board that answers nothing fails as unresponsive,
        which is a different finding from a peer that left.
        """
        deadline = time.monotonic() + timeout
        last: Optional[list[int]] = None
        while True:
            try:
                last = self.node_list(timeout=self.PEER_POLL_REPLY_TIMEOUT)
            except TimeoutWaitingFor:
                raise TimeoutWaitingFor(
                    f"node {node_id} to be a peer (the board did not answer node_list "
                    f"within {self.PEER_POLL_REPLY_TIMEOUT:.0f}s"
                    + (f"; last answer {last}" if last is not None else "") + ")",
                    self.board_id,
                    self.capture.raw_log,
                ) from None
            if node_id in last:
                return last
            if time.monotonic() >= deadline:
                break
            time.sleep(0.25)
        raise TimeoutWaitingFor(
            f"node {node_id} to be a peer (saw {last or 'no peers'})",
            self.board_id,
            self.capture.raw_log,
        )

    def wait_mesh_size(self, size: int, timeout: float = 90.0):
        """Poll node_list until the mesh (excluding self) reaches ``size``."""
        deadline = time.monotonic() + timeout
        last: list[int] = []
        while time.monotonic() < deadline:
            try:
                last = self.node_list(timeout=5.0)
            except TimeoutWaitingFor:
                last = []
            if len(last) >= size:
                return last
            time.sleep(1.0)
        raise TimeoutWaitingFor(
            f"mesh of size {size} (last saw {last})",
            self.board_id,
            self.capture.raw_log,
        )
