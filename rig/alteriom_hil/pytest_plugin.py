"""Pytest fixtures shared by every HIL suite.

Selection is driven by environment (set by the CI workflow):

    ALTERIOM_HIL_MODE=sim|hardware      (default: sim)
    ALTERIOM_HIL_BOARD_MAP=/path/to/board-map.yaml   (hardware mode)
    ALTERIOM_HIL_SIM_BOARDS=3           (sim mode)
    ALTERIOM_HIL_SIM_INSTRUMENTS=1      (sim mode: a simulated instrument wired to the boards)

The ``bank`` fixture yields ``dict[board_id -> BoardClient]``; suites are
written against BoardClient only, so the same test file runs in both modes.
``instruments`` yields ``dict[instrument_id -> InstrumentClient]`` for the
instruments wired to this run's boards, and ``wiring`` the wires themselves.
"""

from __future__ import annotations

import json
import os

import pytest

from .board import BoardMap
from .power import NoopPower, power_for
from .protocol import BoardClient, ProtocolError, TimeoutWaitingFor
from .providers import Redactor
from .run_record import (
    FAILURE_CLASSES,
    HIL_ONLY_REASONS,
    RunRecord,
    write_record,
)
from .serial_capture import SerialCapture


def hil_mode() -> str:
    return os.environ.get("ALTERIOM_HIL_MODE", "sim")


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "hil_only(reason): mark a test that compile-only CI cannot exercise. "
        f"reason must be one of: {sorted(HIL_ONLY_REASONS)}. Failures of "
        "these tests contribute to the bug-catch delta.",
    )
    config.addinivalue_line(
        "markers",
        "failure_class(cls): declare the class a failure of this test should "
        f"be counted as. One of: {sorted(FAILURE_CLASSES)}. If unset, defaults "
        "to 'real_bug' (or 'infra' when the failure is in test setup).",
    )
    config.addinivalue_line(
        "markers",
        "capability(*names): library capabilities for which this test provides evidence.",
    )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    _recover_board_named_by(report, call)
    if report.when == "call" and report.passed:
        _SESSION_HAS_PASSED_A_TEST.add(True)
    # Whether to stop the session is decided before, and independently of,
    # whether run records are being written.
    if (
        report.failed
        and report.when == "setup"
        and hil_mode() == "hardware"
        and not _SESSION_HAS_PASSED_A_TEST
    ):
        # A session hardware fixture failure otherwise fans out into one
        # identical error per collected capability and creates a false
        # impression that the library itself failed everywhere. Only before
        # anything has passed, though: once tests have run, the hardware is
        # up, and a *module* fixture failing — one board losing one
        # role-change command — should cost that module, not every
        # capability after it. It cost the soak measurement in a run that
        # had 21 passes already.
        item.session.shouldstop = "hardware setup failed; remaining capabilities were not validated"
    log_path = os.environ.get("ALTERIOM_HIL_RUN_LOG")
    if not log_path:
        return
    # Emit exactly once per test: at teardown for passing tests, at the phase
    # that failed otherwise. Never re-emit for the same nodeid.
    if report.when == "call" and report.passed:
        _emit(item, report, verdict="passed")
    elif report.failed:
        verdict = "error" if report.when in ("setup", "teardown") else "failed"
        _emit(item, report, verdict=verdict)
    elif report.skipped and report.when in ("setup", "call"):
        _emit(item, report, verdict="skipped")


def _recover_board_named_by(report, call) -> None:
    """Reset the board a failed test named, if it has actually stopped.

    Folded into the existing report hook rather than declared as a second
    ``pytest_runtest_makereport``: two functions of that name in one module
    means the later one silently replaces the earlier, and the earlier is
    what writes every run record the metrics report is built from.
    """
    if report.when != "call" or not report.failed or not _LIVE_CLIENTS:
        return
    exc = getattr(call, "excinfo", None)
    board_id = getattr(exc.value, "board_id", None) if exc else None
    if not board_id:
        return
    note = _recover_if_wedged(_LIVE_CLIENTS, board_id)
    if note:
        report.sections.append(("board recovery", note))


def _emit(item, report, verdict: str) -> None:
    log_path = os.environ.get("ALTERIOM_HIL_RUN_LOG")
    if not log_path:
        return
    hil_only_reason = _marker_arg(item, "hil_only", "reason")
    failure_class = None
    if verdict in ("failed", "error"):
        failure_class = _marker_arg(item, "failure_class", 0)
        if failure_class is None:
            failure_class = (
                "infra"
                if report.when == "setup" or _looks_like_transport_failure(report)
                else "real_bug"
            )
    suite = item.nodeid.split("/")[1] if "/" in item.nodeid else "unknown"
    extra = {"capabilities": _marker_args(item, "capability")}
    if os.environ.get("HIL_AGENT_SHA"):
        extra["hil_agent_sha"] = os.environ["HIL_AGENT_SHA"]
    if hil_mode() == "hardware":
        extra["inventory"] = _hardware_inventory()
        if report.when == "call":
            extra["board_heap"] = _sample_board_heap(_LIVE_CLIENTS)
    rec = RunRecord.now(
        suite=suite,
        test=item.nodeid,
        verdict=verdict,
        duration_s=round(getattr(report, "duration", 0.0), 4),
        mode=hil_mode(),
        boards=int(os.environ.get("ALTERIOM_HIL_SIM_BOARDS", "0") or 0)
        if hil_mode() == "sim"
        else _hardware_board_count(),
        firmware_sha=os.environ.get("HIL_FIRMWARE_SHA")
        or os.environ.get("PAINLESSMESH_SHA"),
        workflow_run_id=os.environ.get("GITHUB_RUN_ID"),
        failure_class=failure_class,
        hil_only_reason=hil_only_reason,
        message=str(report.longrepr)[:2000] if report.failed else None,
        extra=extra,
    )
    write_record(log_path, rec)


def _looks_like_transport_failure(report) -> bool:
    message = str(getattr(report, "longrepr", "")).lower()
    return any(
        clue in message
        for clue in (
            "input/output error",
            "serialexception",
            "portnotopen",
            "could not open port",
            "device reports readiness",
            "no such file or directory: '/dev/tty",
            "no such file or directory: \"/dev/tty",
        )
    )


def _marker_arg(item, name: str, key):
    m = item.get_closest_marker(name)
    if m is None:
        return None
    if isinstance(key, int):
        return m.args[key] if len(m.args) > key else m.kwargs.get("reason")
    return m.kwargs.get(key) or (m.args[0] if m.args else None)


def _marker_args(item, name: str) -> list[str]:
    if hasattr(item, "iter_markers"):
        markers = item.iter_markers(name=name)
    else:
        marker = item.get_closest_marker(name)
        markers = [marker] if marker is not None else []
    return sorted({str(arg) for marker in markers for arg in marker.args})


def _hardware_board_count() -> int:
    path = os.environ.get("ALTERIOM_HIL_BOARD_MAP")
    if not path or not os.path.exists(path):
        return 0
    try:
        return len(BoardMap.load(path))
    except Exception:
        return 0


def _sample_board_heap(clients: dict, timeout: float = 3.0) -> dict:
    """Free heap per board, taken after each test's call phase.

    board-health.json gives the suite's two endpoints; the ESP8266 loses
    half its heap between them while every ESP32 holds within a few
    percent, and the endpoints cannot say *which tests* take it. One
    `info` per board per test costs about 30 ms each on a settled mesh and
    puts the curve in the run records. A board that does not answer is
    recorded as such; a sample is never allowed to fail the test.
    """
    heap = {}
    for board_id, client in clients.items():
        try:
            heap[board_id] = int(client.info(timeout=timeout)["freeHeap"])
        except (TimeoutWaitingFor, ProtocolError, KeyError, ValueError, TypeError):
            heap[board_id] = None
    return heap


def _hardware_inventory() -> list[dict[str, str]]:
    board_map = _load_board_map()
    if board_map is None:
        return []
    return [
        {"id": board.id, "target": board.target, "chip": board.chip}
        for board in board_map
    ]


@pytest.fixture(scope="session")
def board_client_class():
    """The class a suite's boards are driven through.

    `BoardClient` is the transport and the few things any agent can be asked.
    A suite whose firmware can be asked more overrides this fixture in its
    conftest with a subclass that says what, and `bank` builds every board as
    that. The rig knows the transport; a firmware's verbs are its suite's.
    """
    return BoardClient


@pytest.fixture(scope="session")
def sim_firmware():
    """What a simulated board runs beyond the common part, in sim mode:
    a `SimFirmware` subclass, or None for a board that runs the health
    check's firmware and nothing else. A suite whose agent has verbs of its
    own overrides this with the simulation it keeps beside the suite."""
    return None


@pytest.fixture(scope="session")
def bank(board_client_class, sim_firmware):
    if hil_mode() == "hardware":
        yield from _hardware_bank(board_client_class)
    else:
        yield from _sim_bank(board_client_class, sim_firmware)


# The mesh fixture is session-scoped, so a board that hangs partway through a
# suite is still hung for every test after it. Those tests then fail for their
# own apparent reasons and the run reads as broadly broken rather than as one
# board that stopped. Populated by the hardware bank; empty in sim mode.
_LIVE_CLIENTS: dict = {}
# Non-empty once any test's call phase has passed this session — i.e. the
# hardware bank came up. A set rather than a bool so the hook can update it
# without a global statement.
_SESSION_HAS_PASSED_A_TEST: set = set()


def _recover_if_wedged(clients: dict, board_id: str, probe_timeout: float = 5.0):
    """Reset a board that has stopped answering. Returns a note, or None.

    Only a board that fails to answer ``info`` is reset. A test can time out
    waiting for a mesh event for reasons that say nothing about the board's
    health — a peer never forwarded, an election never ran — and resetting a
    healthy board would turn one honest failure into several invented ones.
    """
    client = clients.get(board_id)
    if client is None:
        return None
    try:
        client.info(timeout=probe_timeout)
        return None
    except TimeoutWaitingFor:
        pass
    try:
        client.hard_reset()
        client.info(timeout=60.0)
        return f"{board_id} stopped answering and was reset; it is back"
    except (TimeoutWaitingFor, ProtocolError) as exc:
        return (
            f"{board_id} stopped answering and did not come back from a "
            f"reset ({exc.__class__.__name__})"
        )


def _board_health(clients: dict, start_info: dict) -> dict:
    """Per-board free heap and reboots across the suite.

    The ESP8266's free heap was seen falling from 18.5 K at boot to 8 K by
    the end of a suite, and it is the board whose deliveries then go
    unacknowledged. That was only ever visible by grepping serial logs
    afterwards. Recording it per run makes a degrading board a farm signal
    instead of a discovery.

    A board that has stopped answering is recorded as such rather than
    omitted: that is the most important row in the file.
    """
    health = {}
    for board_id, client in clients.items():
        began = start_info.get(board_id) or {}
        entry = {
            "target": began.get("target"),
            "free_heap_start": began.get("freeHeap"),
            "boot_id_start": began.get("bootId"),
        }
        try:
            ended = client.info(timeout=10.0)
            entry["free_heap_end"] = ended.get("freeHeap")
            entry["boot_id_end"] = ended.get("bootId")
            entry["responsive_at_end"] = True
            start_heap, end_heap = entry["free_heap_start"], entry["free_heap_end"]
            if isinstance(start_heap, int) and isinstance(end_heap, int) and start_heap:
                entry["free_heap_delta_percent"] = round(
                    100.0 * (end_heap - start_heap) / start_heap, 1
                )
            if entry["boot_id_start"] is not None:
                entry["rebooted_during_suite"] = (
                    entry["boot_id_end"] != entry["boot_id_start"]
                )
        except (TimeoutWaitingFor, ProtocolError) as exc:
            entry["responsive_at_end"] = False
            entry["error"] = exc.__class__.__name__
        health[board_id] = entry
    return health


def _dump_board_health(clients: dict, start_info: dict):
    log_dir = os.environ.get("ALTERIOM_HIL_LOG_DIR")
    if not log_dir:
        return
    from pathlib import Path

    out = Path(log_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "board-health.json").write_text(
        json.dumps(_board_health(clients, start_info), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _dump_logs(clients: dict):
    """Write each board's raw serial log to ALTERIOM_HIL_LOG_DIR (if set)
    so CI can upload them as artifacts.

    Scrubbed of the rig's provider secrets first: a board that relayed a real
    CallMeBot request logs the URL, and the service's reply echoes the
    number (alteriom_hil.providers)."""
    log_dir = os.environ.get("ALTERIOM_HIL_LOG_DIR")
    if not log_dir:
        return
    from pathlib import Path

    out = Path(log_dir)
    out.mkdir(parents=True, exist_ok=True)
    redactor = Redactor.from_env()
    for board_id, client in clients.items():
        (out / f"{board_id}.serial.log").write_text(
            redactor.scrub("\n".join(client.capture.raw_log) + "\n")
        )


# The simulator the sim bank made, for the instruments fixture to wire to.
_SIM_HUB: list = []


def _sim_bank(client_class=BoardClient, firmware=None):
    from .sim import SimHub

    n = int(os.environ.get("ALTERIOM_HIL_SIM_BOARDS", "3"))
    hub = SimHub(n, instruments=os.environ.get("ALTERIOM_HIL_SIM_INSTRUMENTS", "1") != "0",
                 firmware=firmware)
    _SIM_HUB[:] = [hub]
    clients = {}
    captures = []
    for board in hub.boards:
        cap = SerialCapture(board.open_host_stream).start()
        captures.append(cap)
        clients[f"sim-{board.node_id}"] = client_class(
            f"sim-{board.node_id}", cap
        )
    try:
        yield clients
    finally:
        _dump_logs(clients)
        for cap in captures:
            cap.stop()


def _hardware_bank(client_class=BoardClient):
    import serial  # pyserial — only needed in hardware mode

    map_path = os.environ.get("ALTERIOM_HIL_BOARD_MAP")
    if not map_path:
        pytest.skip("ALTERIOM_HIL_BOARD_MAP not set — no hardware attached")
    board_map = BoardMap.load(map_path)
    clients = {}
    captures = []
    for board in board_map:
        opener = _serial_opener(serial, board)
        cap = SerialCapture(opener).start()
        captures.append(cap)
        clients[board.id] = client_class(board.id, cap)
    # A board that hard-hung during an earlier suite is still hung: silent,
    # port open, no watchdog. Every run after it fails somewhere different
    # depending on which test needed that board, which reads like flakiness
    # and is not. Reset the silent ones here, once, before anything is
    # measured — and name any that stay dead rather than letting a fixture
    # deeper in report it as a mesh problem.
    wedged = []
    start_info = {}
    for board_id, client in clients.items():
        try:
            start_info[board_id] = client.ensure_responsive()
        except (TimeoutWaitingFor, ProtocolError) as exc:
            wedged.append(f"{board_id} ({exc.__class__.__name__})")
    if wedged:
        for cap in captures:
            cap.stop()
        pytest.fail(
            "boards did not answer even after a reset: " + ", ".join(wedged)
        )
    _LIVE_CLIENTS.update(clients)
    try:
        yield clients
    finally:
        _LIVE_CLIENTS.clear()
        _dump_board_health(clients, start_info)
        _dump_logs(clients)
        for cap in captures:
            cap.stop()


# ---- instruments -----------------------------------------------------------------
# Test equipment wired to the boards (alteriom_hil.instrument). In hardware mode
# they come from the run's board map, beside its boards; in sim mode from the
# simulator the bank made. A suite that asks for neither never opens one.


@pytest.fixture(scope="session")
def instruments(bank):
    if hil_mode() == "hardware":
        yield from _hardware_instruments(set(bank))
    else:
        yield from _sim_instruments()


@pytest.fixture(scope="session")
def wiring(instruments, bank) -> list:
    """Every wire to a board of this run: ``[(instrument_id, Wire), ...]``."""
    from .instrument import Wire

    if hil_mode() != "hardware":
        hub = _SIM_HUB[0] if _SIM_HUB else None
        if hub is None:
            return []
        return [
            (instrument.id, Wire(channel=channel, board=hub.board_id(board), pin=pin))
            for instrument, channel, board, pin in hub.wires
            if hub.board_id(board) in bank
        ]
    return [
        (item["id"], Wire(**wire))
        for item in _map_instruments()
        if item["id"] in instruments
        for wire in item.get("wiring") or []
        if wire.get("board") in bank
    ]


def _map_instruments() -> list:
    import yaml

    map_path = os.environ.get("ALTERIOM_HIL_BOARD_MAP")
    if not map_path or not os.path.exists(map_path):
        return []
    with open(map_path, encoding="utf-8") as stream:
        document = yaml.safe_load(stream) or {}
    return [item for item in document.get("instruments") or [] if isinstance(item, dict) and item.get("id")]


def _sim_instruments():
    from .instrument import InstrumentClient

    hub = _SIM_HUB[0] if _SIM_HUB else None
    clients, captures = {}, []
    for instrument in (hub.instruments if hub else []):
        capture = SerialCapture(instrument.open_host_stream).start()
        captures.append(capture)
        clients[instrument.id] = InstrumentClient(instrument.id, capture, kind=instrument.kind.name)
    try:
        yield clients
    finally:
        _release_instruments(clients)
        for capture in captures:
            capture.stop()


def _hardware_instruments(board_ids: set):
    import serial

    from .board import Board
    from .instrument import InstrumentClient

    clients, captures = {}, []
    for item in _map_instruments():
        # Only an instrument wired to a board this run was given: one wired
        # to somebody else's allocation is not this run's to drive.
        if not any(wire.get("board") in board_ids for wire in item.get("wiring") or []):
            continue
        port_holder = Board(id=item["id"], port=item["port"], chip=item.get("chip", "esp32"),
                            target=item.get("target", "esp32"))
        capture = SerialCapture(_serial_opener(serial, port_holder)).start()
        captures.append(capture)
        client = InstrumentClient(item["id"], capture, kind=item["kind"])
        try:
            client.info(timeout=15)
        except (TimeoutWaitingFor, ProtocolError) as exc:
            for started in captures:
                started.stop()
            pytest.fail(f"instrument {item['id']} on {item['port']} is not answering as one: {exc}")
        clients[item["id"]] = client
    try:
        yield clients
    finally:
        _release_instruments(clients)
        for capture in captures:
            capture.stop()


def _release_instruments(clients: dict):
    """Every channel back to a plain input, and each instrument's log kept
    beside the boards' -- `<id>.serial.log`, which the farm serves."""
    log_dir = os.environ.get("ALTERIOM_HIL_LOG_DIR")
    for instrument_id, client in clients.items():
        try:
            client.release()
        except (TimeoutWaitingFor, ProtocolError):
            pass
        if log_dir:
            from pathlib import Path

            out = Path(log_dir)
            out.mkdir(parents=True, exist_ok=True)
            (out / f"{instrument_id}.serial.log").write_text(
                Redactor.from_env().scrub("\n".join(client._link.capture.raw_log) + "\n"), encoding="utf-8"
            )


def _serial_opener(serial_mod, board):
    def open_port():
        stream = serial_mod.Serial(
            board.port,
            board.baud,
            timeout=0.1,
            rtscts=False,
            dsrdtr=False,
        )
        # CP210x/CH340 ESP32 dev boards wire RTS/DTR to EN/BOOT. pyserial's
        # initial modem-line state can otherwise hold a freshly flashed board
        # in reset or provoke a reset loop as capture starts. Always release
        # both lines after open, including SerialCapture.reopen().
        stream.rts = False
        stream.dtr = False
        return stream

    return open_port


def _load_board_map():
    """The rig's BoardMap, or None when there is no hardware map."""
    if hil_mode() != "hardware":
        return None
    map_path = os.environ.get("ALTERIOM_HIL_BOARD_MAP")
    if not map_path or not os.path.exists(map_path):
        return None
    try:
        return BoardMap.load(map_path)
    except Exception:
        return None


@pytest.fixture(scope="session")
def board_map():
    """The rig's BoardMap in hardware mode, else None (sim has no boards).

    Tests that need physical board attributes — power coordinates, serial
    port paths — take this and skip when it is None.
    """
    return _load_board_map()


def _resolve_power():
    """Pick a power controller from the environment.

    Sim mode and hardware rigs with no switchable hub both get NoopPower, so
    power-dependent tests skip rather than error. Never raises: a broken or
    missing board map degrades to no-op, since power control is a recovery
    convenience, not a reason to fail collection.
    """
    rig = _load_board_map()
    if rig is None:
        return NoopPower()
    return power_for(rig)


@pytest.fixture(scope="session")
def power():
    """Per-board power control for the rig (see alteriom_hil.power).

    Always yields a controller — check ``power.supports(board)`` before
    using it and skip when False.
    """
    return _resolve_power()


@pytest.fixture()
def any_two(bank):
    """Two distinct BoardClients (skip if the rig has fewer than two)."""
    clients = list(bank.values())
    if len(clients) < 2:
        pytest.skip("suite needs at least two boards")
    return clients[0], clients[1]
