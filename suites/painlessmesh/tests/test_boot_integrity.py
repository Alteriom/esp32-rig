"""Every node boots once, without a crash, and serves.

painlessMesh #466: an uninitialised listener pointer crashed a node inside
``init()`` before it served a single connection. On this rig that read as
"the mesh never formed" -- a timeout in the ``mesh`` fixture, with the
crash and the boot loop behind it visible only to whoever grepped the
serial log afterwards. These rows make the rig say what happened.

Two capabilities, deliberately separate:

* ``node.boot_integrity`` needs only the bank, not a formed mesh, so a
  board that boot-loops is reported *by this test* with its crash marker
  and boot count, instead of blocking every test behind the ``mesh``
  fixture. It reads the serial capture the bank has kept since it came
  up: the flash tool's own reset precedes the capture, so a healthy board
  shows no ``boot`` frame at all (or exactly one, if the bank had to reset
  a wedged board before measuring), and a crashing one shows a fresh boot
  id per crash.
* ``node.listener_serving`` asks the node itself whether its TCP listener
  is in LISTEN, through the ``listening`` field the agent reports when the
  library under test has ``tcpListening()`` (painlessMesh >= 2.1.1). The
  mesh forming already proves *some* listener accepted *some* peer; this
  proves every node's is listening now, which is what #435's promoted
  bridge lacked for two minutes while the mesh looked whole.
"""

from __future__ import annotations

import json
import time

import pytest

from alteriom_hil.protocol import TimeoutWaitingFor

# Printed by the ESP32 ROM / ESP-IDF panic handler, the ESP8266 boot ROM
# and SDK, or the Arduino core, when a chip did not reboot on purpose. A
# deliberate reset -- the bank's EN pulse, an OTA activation -- prints a
# POWERON or software-reset reason and none of these.
CRASH_MARKERS = (
    "Guru Meditation Error",
    "panic'ed",
    "abort() was called",
    "Backtrace:",
    "Fatal exception",
    "Exception (",
    "Soft WDT reset",
    "wdt reset",
    "BROWNOUT_RESET",
    "Rebooting...",
)

# How long a freshly flashed board may take to answer the first ``info``.
# Measured on this rig, reset to a first reply is 7 s on most families and
# 15 s on the ESP32-C5; a board that boot-loops never answers.
FIRST_INFO_TIMEOUT = 60.0


def _boot_frames(raw_lines: list[str]) -> list[dict]:
    """The ``boot`` frames in a raw serial log, in order.

    The capture already decodes frames into events, but those are consumed
    by whoever waited for them; the raw log is the record that survives.
    """
    decoder = json.JSONDecoder()
    frames = []
    for line in raw_lines:
        start = line.find("{")
        while start >= 0:
            try:
                obj, end = decoder.raw_decode(line, start)
            except json.JSONDecodeError:
                start = line.find("{", start + 1)
                continue
            if isinstance(obj, dict) and obj.get("evt") == "boot":
                frames.append(obj)
            start = line.find("{", max(end, start + 1))
    return frames


def _crash_lines(raw_lines: list[str]) -> list[str]:
    return [line for line in raw_lines if any(marker in line for marker in CRASH_MARKERS)]


def _first_info(client) -> dict | None:
    deadline = time.monotonic() + FIRST_INFO_TIMEOUT
    while time.monotonic() < deadline:
        try:
            return client.info(timeout=5.0)
        except TimeoutWaitingFor:
            time.sleep(1.0)
    return None


@pytest.mark.capability("node.boot_integrity")
def test_every_node_boots_once_without_a_crash(bank):
    """Runs on the bank alone, so a boot-looping board is named here."""
    findings = []
    for board_id, client in bank.items():
        raw = list(client.capture.raw_log)
        boots = _boot_frames(raw)
        crashes = _crash_lines(raw)
        info = _first_info(client)

        if crashes:
            findings.append(
                f"{board_id}: {len(crashes)} crash marker(s) in its serial log, "
                f"first: {crashes[0].strip()[:160]!r}"
            )
        boot_ids = {frame.get("bootId") for frame in boots}
        if info is not None:
            boot_ids.add(info.get("bootId"))
        if len(boot_ids) > 1:
            findings.append(
                f"{board_id}: {len(boots)} boot frame(s) with {len(boot_ids)} "
                f"distinct boot ids since the bank came up -- it rebooted "
                f"{len(boot_ids) - 1} time(s) when it should have booted once"
            )
        if info is None:
            findings.append(
                f"{board_id}: never answered `info` within {FIRST_INFO_TIMEOUT:.0f}s "
                f"({len(boots)} boot frame(s) seen"
                + (f", crashing" if crashes else "")
                + ")"
            )
    assert not findings, "boards that did not come up cleanly:\n  " + "\n  ".join(findings)


@pytest.mark.capability("node.listener_serving")
def test_every_node_reports_its_listener_listening(mesh):
    """Every node in the formed mesh has a listener in LISTEN, by its own account."""
    clients, _node_ids = mesh
    infos = {board_id: client.info(timeout=15.0) for board_id, client in clients.items()}
    if not any("listening" in info for info in infos.values()):
        pytest.skip(
            "the library under test has no tcpListening() accessor "
            "(painlessMesh < 2.1.1): the agent reports no `listening` field"
        )
    not_listening = [
        f"{board_id} (nodeId {info.get('nodeId')})"
        for board_id, info in infos.items()
        if info.get("listening") is not True
    ]
    assert not not_listening, (
        "nodes whose TCP listener is not in LISTEN although the mesh formed: "
        + ", ".join(not_listening)
        + " -- a peer can reach them only through connections they made outbound; "
        "nothing can join through them (painlessMesh #435, #466)"
    )
