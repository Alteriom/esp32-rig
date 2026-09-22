"""End-to-end test that the plugin's makereport hook writes JSONL rows.

Drives the ``_emit`` function directly with stub Item/Report objects so
we don't need to spawn a nested pytest session (avoids double-registering
the auto-loaded plugin under pytester).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from alteriom_hil import pytest_plugin as plug


class FakeMarker:
    def __init__(self, args=(), kwargs=None):
        self.args = args
        self.kwargs = kwargs or {}


class FakeItem:
    def __init__(self, nodeid: str, markers: dict[str, FakeMarker] | None = None):
        self.nodeid = nodeid
        self._markers = markers or {}

    def get_closest_marker(self, name: str):
        return self._markers.get(name)


def _report(when: str, outcome: str, longrepr=None, duration=0.1):
    r = SimpleNamespace(
        when=when,
        outcome=outcome,
        passed=outcome == "passed",
        failed=outcome == "failed",
        skipped=outcome == "skipped",
        duration=duration,
        longrepr=longrepr,
    )
    return r


def _read(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_emit_passed_writes_record(tmp_path, monkeypatch):
    log = tmp_path / "runs.jsonl"
    monkeypatch.setenv("ALTERIOM_HIL_RUN_LOG", str(log))
    monkeypatch.setenv("ALTERIOM_HIL_MODE", "sim")
    monkeypatch.setenv("ALTERIOM_HIL_SIM_BOARDS", "3")
    monkeypatch.setenv("HIL_FIRMWARE_SHA", "cafef00d")

    item = FakeItem(
        "suites/painlessmesh/tests/test_x.py::test_pass",
        {
            "hil_only": FakeMarker(kwargs={"reason": "radio_timing"}),
            "capability": FakeMarker(args=("mesh.formation", "mesh.mixed_mcu")),
        },
    )
    plug._emit(item, _report("call", "passed"), "passed")
    rows = _read(log)
    assert len(rows) == 1
    r = rows[0]
    assert r["verdict"] == "passed"
    assert r["hil_only_reason"] == "radio_timing"
    assert r["firmware_sha"] == "cafef00d"
    assert r["suite"] == "painlessmesh"
    assert r["mode"] == "sim"
    assert r["boards"] == 3
    assert r["extra"]["capabilities"] == ["mesh.formation", "mesh.mixed_mcu"]
    assert "failure_class" not in r


def test_emit_failed_defaults_class_to_real_bug(tmp_path, monkeypatch):
    log = tmp_path / "runs.jsonl"
    monkeypatch.setenv("ALTERIOM_HIL_RUN_LOG", str(log))
    item = FakeItem(
        "suites/painlessmesh/tests/test_x.py::test_fail",
        {"hil_only": FakeMarker(kwargs={"reason": "ota"})},
    )
    plug._emit(item, _report("call", "failed", longrepr="AssertionError"), "failed")
    r = _read(log)[0]
    assert r["verdict"] == "failed"
    assert r["failure_class"] == "real_bug"
    assert r["hil_only_reason"] == "ota"
    assert "AssertionError" in r["message"]


def test_emit_setup_failure_classified_as_infra(tmp_path, monkeypatch):
    log = tmp_path / "runs.jsonl"
    monkeypatch.setenv("ALTERIOM_HIL_RUN_LOG", str(log))
    item = FakeItem("s/painlessmesh/tests/test_x.py::test_setup_dies")
    plug._emit(item, _report("setup", "failed", longrepr="RuntimeError"), "error")
    r = _read(log)[0]
    assert r["verdict"] == "error"
    assert r["failure_class"] == "infra"


def test_emit_honors_failure_class_marker(tmp_path, monkeypatch):
    log = tmp_path / "runs.jsonl"
    monkeypatch.setenv("ALTERIOM_HIL_RUN_LOG", str(log))
    item = FakeItem(
        "s/painlessmesh/tests/test_x.py::test_jitter",
        {
            "hil_only": FakeMarker(kwargs={"reason": "power"}),
            "failure_class": FakeMarker(args=("flaky_hardware",)),
        },
    )
    plug._emit(item, _report("call", "failed", longrepr="jitter"), "failed")
    assert _read(log)[0]["failure_class"] == "flaky_hardware"


def test_emit_call_transport_failure_is_infra(tmp_path, monkeypatch):
    log = tmp_path / "runs.jsonl"
    monkeypatch.setenv("ALTERIOM_HIL_RUN_LOG", str(log))
    item = FakeItem("s/painlessmesh/tests/test_x.py::test_transport")
    plug._emit(
        item,
        _report("call", "failed", longrepr="OSError: [Errno 5] Input/output error"),
        "failed",
    )
    assert _read(log)[0]["failure_class"] == "infra"


def test_emit_noop_when_log_env_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("ALTERIOM_HIL_RUN_LOG", raising=False)
    item = FakeItem("s/painlessmesh/tests/test_x.py::test_x")
    plug._emit(item, _report("call", "passed"), "passed")
    assert not list(tmp_path.iterdir())


def test_markers_registered_on_configure():
    # Call pytest_configure with a stub config to make sure it doesn't crash
    # and all markers appear in the recorded ini lines.
    seen: list[str] = []

    class StubConfig:
        def addinivalue_line(self, name, line):
            seen.append(line)

    plug.pytest_configure(StubConfig())
    assert any("hil_only" in s for s in seen)
    assert any("failure_class" in s for s in seen)
    assert any("capability" in s for s in seen)
