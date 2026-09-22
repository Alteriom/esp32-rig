"""painlessMesh's HIL agent, simulated: the mesh half of a simulated board.

`alteriom_hil.sim` simulates a board that boots, answers `info` and the health
check, and has pins. This is the firmware painlessMesh's suite is written
for, on top of that: `node_list`, `send_single` and `send_broadcast` with ack
semantics that mirror painlessMesh's delivery-confirmation API (issue #379 /
PR #383), a `stall` that stops a board servicing serial, and the `listening`
an agent reports from `tcpListening()`. The suite's conftest hands it to the
hub through the `sim_firmware` fixture. Keep its behaviour aligned with
`firmware/src/main.cpp`.

It does not exercise painlessMesh -- the rig does. What it catches is every
bug in the suite before it costs rig time.
"""

from __future__ import annotations

import threading
import time

from alteriom_hil.sim import SimBoard, SimFirmware


class MeshFirmware(SimFirmware):
    SIM_LATENCY_MS = 15

    def info_fields(self, board: SimBoard) -> dict:
        # What the agent reports from painlessMesh's tcpListening() on a
        # board: a simulated node accepts peers whenever it answers at all,
        # so the row that asks (node.listener_serving) runs here rather than
        # skipping -- but a pass here proves the row's own logic, not the
        # library's listener; that is the rig's to prove.
        return {"listening": True}

    def handle(self, board: SimBoard, name: str, cmd: dict) -> bool:
        if name == "node_list":
            others = [b.node_id for b in self.hub.boards if b is not board]
            board.emit({"evt": "node_list", "nodes": others})
        elif name == "send_single":
            self.send_single(board, cmd)
        elif name == "send_broadcast":
            self.send_broadcast(board, cmd)
        elif name == "stall":
            board.stalled_until = time.monotonic() + cmd["ms"] / 1000.0
            board.emit({"evt": "stalled", "ms": cmd["ms"]})
        else:
            return False
        return True

    # ---- message semantics ----
    def _deliver(self, sender: SimBoard, dest: SimBoard, msg: str, ack: bool,
                 ack_timeout_ms: int):
        if dest.stalled:
            # message sits unprocessed: no recv, no ack -> timeout fires
            if ack:
                t = threading.Timer(
                    ack_timeout_ms / 1000.0,
                    lambda: sender.emit(
                        {
                            "evt": "ack",
                            "node": dest.node_id,
                            "delivered": False,
                            "latencyMs": ack_timeout_ms,
                        }
                    ),
                )
                t.daemon = True
                t.start()
            return
        dest.emit({"evt": "recv", "from": sender.node_id, "msg": msg})
        if ack:
            t = threading.Timer(
                self.SIM_LATENCY_MS / 1000.0,
                lambda: sender.emit(
                    {
                        "evt": "ack",
                        "node": dest.node_id,
                        "delivered": True,
                        "latencyMs": self.SIM_LATENCY_MS,
                    }
                ),
            )
            t.daemon = True
            t.start()

    def send_single(self, sender: SimBoard, cmd: dict):
        dest = self.hub.by_id(int(cmd["dest"]))
        ack = bool(cmd.get("ack"))
        if dest is None:
            # mirrors router::send failing: nothing sent, callback never fires
            sender.emit({"evt": "send_result", "ok": False})
            return
        sender.emit({"evt": "send_result", "ok": True})
        self._deliver(
            sender, dest, cmd["msg"], ack, int(cmd.get("ackTimeoutMs", 5000))
        )

    def send_broadcast(self, sender: SimBoard, cmd: dict):
        others = [b for b in self.hub.boards if b is not sender]
        if not others:
            sender.emit({"evt": "send_result", "ok": False})
            return
        sender.emit({"evt": "send_result", "ok": True})
        ack = bool(cmd.get("ack"))
        for dest in others:
            self._deliver(
                sender, dest, cmd["msg"], ack, int(cmd.get("ackTimeoutMs", 5000))
            )
