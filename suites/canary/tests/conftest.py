"""The Rig Health Check suite (the farm's `canary` profile).

One verdict per board per check, from firmware the farm owns. Nothing here
knows anything about painlessMesh or about a consumer's product: a red
canary is an ESP, a cable, a hub, or the rig's own AP, broker or uplink.

The report separates the two kinds of red, which is the whole reason the
checks are per-board: a check failing on **every** board is the farm, a
check failing on **one** board is that board.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from canary_client import CanaryClient

# Session-unique, so a key left behind by an interrupted run is never read
# as this run's write, and a queue check's message can be told from the last
# one's in the broker's capture.
RUN_TAG = os.environ.get("ALTERIOM_HIL_RUN_TAG") or os.urandom(4).hex()


def flashed_version() -> str | None:
    """The firmware version the bundle this run flashed says it is.

    None off the rig, and for a bundle built before versions were stamped:
    there is nothing to hold a board to then, and a health check must not
    fail a good board over a bundle's age.
    """
    if os.environ.get("ALTERIOM_HIL_MODE") != "hardware":
        return None
    folder = os.environ.get("ALTERIOM_HIL_ARTIFACT_DIR")
    if not folder:
        return None
    try:
        manifest = json.loads((Path(folder) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = manifest.get("version")
    return version if isinstance(version, str) and version else None


@pytest.fixture(scope="session")
def canaries(bank) -> dict[str, CanaryClient]:
    """Every board the run was given, each answering the canary protocol.

    A board that does not answer `info` at all is not skipped quietly: it
    is the first thing a health check is for, so it is left in the mapping
    and the boot check reports it.
    """
    return {board_id: CanaryClient(client) for board_id, client in bank.items()}


def pytest_generate_tests(metafunc):
    """Run every per-board check once per board, named by the board.

    Parametrising here rather than looping inside the tests is what makes
    the report a board x check matrix: one result per board per check, so
    "the radio join failed on esp32-c5-01" is a row of its own rather than
    a sentence buried in one test's output -- and it is what lets the report
    tell a check that failed everywhere (the farm) from one that failed on
    a single board (that board).
    """
    if "canary" not in metafunc.fixturenames:
        return
    board_ids = _board_ids()
    if not board_ids:
        metafunc.parametrize(
            "canary",
            [pytest.param("none", marks=pytest.mark.skip(reason="the run was given no boards"))],
        )
        return
    metafunc.parametrize("canary", board_ids, ids=board_ids)


def _board_ids() -> list[str]:
    """The boards this run will have, known at collection time.

    Collection happens before any fixture runs, so this reads the board map
    the farm scoped for this run rather than the bank -- the same file the
    bank itself is built from. In sim mode the names are the simulator's.
    """
    if os.environ.get("ALTERIOM_HIL_MODE") != "hardware":
        count = int(os.environ.get("ALTERIOM_HIL_SIM_BOARDS", "3"))
        return [f"sim-{1000 + index}" for index in range(count)]
    from alteriom_hil.board import BoardMap

    path = os.environ.get("ALTERIOM_HIL_BOARD_MAP")
    if not path:
        return []
    return [board.id for board in BoardMap.load(path)]


@pytest.fixture()
def board(canary, canaries) -> CanaryClient:
    """The client for the board this check is running against."""
    client = canaries.get(canary)
    if client is None:
        pytest.fail(f"board {canary} was collected but the bank has no client for it")
    client.board.clear_pending()
    return client


# ---- what the rig is -------------------------------------------------------
# The canary checks the rig as well as the board, so it needs to know what
# the rig is. These are the names the farm already exports to every suite
# (farm_service._suite_env), so a canary run needs no settings of its own.


@pytest.fixture(scope="session")
def rig_wifi() -> tuple[str, str]:
    ssid = os.environ.get("ALTERIOM_HIL_WIFI_SSID")
    password_file = os.environ.get("ALTERIOM_HIL_WIFI_PASSWORD_FILE")
    if not ssid:
        pytest.skip("the rig has no access point configured (gateway.enabled)")
    password = ""
    if password_file:
        try:
            password = Path(password_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            pytest.skip(f"cannot read the rig Wi-Fi password: {exc}")
    return ssid, password


@pytest.fixture(scope="session")
def rig_uplink() -> str:
    endpoint = os.environ.get("ALTERIOM_HIL_GATEWAY_ENDPOINT")
    if not endpoint:
        pytest.skip("the rig has no uplink probe configured (gateway.endpoint)")
    return endpoint.rstrip("/")


@pytest.fixture(scope="session")
def rig_broker() -> tuple[str, int]:
    url = os.environ.get("ALTERIOM_HIL_MQTT_URL")
    if not url:
        pytest.skip("the rig has no broker configured (mqtt.enabled)")
    authority = url.split("://", 1)[-1].split("/", 1)[0]
    host, _, port = authority.partition(":")
    if not host:
        pytest.skip(f"the rig broker url names no host: {url!r}")
    return host, int(port or 1883)
