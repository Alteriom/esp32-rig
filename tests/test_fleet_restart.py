"""Restarting the whole bank into the regular mesh together, not in turn."""

from __future__ import annotations

import threading
import time

import pytest

# The mesh verbs are painlessMesh's agent's, and live beside its suite.
from suites.painlessmesh.meshclient import MeshBoardClient as BoardClient


class _Board:
    """A stand-in for one board's client: a restart that takes a while."""

    def __init__(self, board_id, seconds=0.3, fail=None):
        self.board_id, self.seconds, self.fail = board_id, seconds, fail
        self.started_at = None

    def start_regular_mesh(self, timeout):
        self.started_at = time.monotonic()
        time.sleep(self.seconds)
        if self.fail:
            raise self.fail
        return {"evt": "mesh_started", "board": self.board_id, "timeout": timeout}


def test_every_board_is_restarted_at_the_same_time():
    # One at a time, six boards at up to 35 s each left the mesh split
    # across two channels for minutes — long enough for painlessMesh to
    # follow a node still in gateway mode as if it were a bridge that had
    # moved. Together, the mixed state lasts as long as one restart.
    boards = {f"b{i}": _Board(f"b{i}", seconds=0.3) for i in range(6)}
    began = time.monotonic()
    results = BoardClient.restart_all_regular(boards, timeout=35)
    took = time.monotonic() - began
    assert set(results) == set(boards)
    assert all(r["timeout"] == 35 for r in results.values())
    assert took < 0.3 * 3, f"restarts ran in turn: {took:.2f}s for six boards"
    starts = [b.started_at for b in boards.values()]
    assert max(starts) - min(starts) < 0.2, "every board was told at once"


def test_one_failing_board_does_not_stop_the_others_and_is_reported():
    boards = {
        "ok1": _Board("ok1"),
        "bad": _Board("bad", fail=RuntimeError("no restart acknowledgement")),
        "ok2": _Board("ok2"),
    }
    with pytest.raises(RuntimeError, match="no restart acknowledgement"):
        BoardClient.restart_all_regular(boards, timeout=5)
    assert all(b.started_at is not None for b in boards.values()), (
        "every board was still given its restart"
    )


def test_an_empty_bank_is_a_no_op():
    assert BoardClient.restart_all_regular({}, timeout=5) == {}
