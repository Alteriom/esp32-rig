"""Power-control HAL: availability detection and the pytest ``power`` fixture.

The rig's promise (runner/board-map.example.yaml, docs/runbook.md) is that
power control is *optional*: rigs without a uhubctl-capable hub, or boards
without power coordinates, must degrade to a no-op that tests can detect
via ``power.available`` / ``power.supports(board)`` — never an exception
mid-suite.
"""

from __future__ import annotations

import pytest

from alteriom_hil.board import Board
from alteriom_hil.power import NoopPower, UhubctlPower, power_for

POWERED = Board(id="b1", port="/dev/esp32-farm-01", power_hub="1-1", power_port=1)
UNPOWERED = Board(id="b2", port="/dev/esp32-farm-02")


class TestUhubctlAvailability:
    def test_unavailable_when_binary_missing(self):
        p = UhubctlPower(uhubctl="definitely-not-a-real-binary-xyz")
        assert p.available is False

    def test_available_when_binary_present(self):
        # every Linux/macOS box running these tests has `sh`
        assert UhubctlPower(uhubctl="sh").available is True

    def test_supports_requires_board_coordinates(self):
        p = UhubctlPower(uhubctl="sh")
        assert p.supports(POWERED) is True
        assert p.supports(UNPOWERED) is False

    def test_cycle_refuses_board_without_coordinates(self):
        p = UhubctlPower(uhubctl="sh")
        with pytest.raises(ValueError, match="no power_hub/power_port"):
            p.off(UNPOWERED)

    def test_cycle_refuses_when_binary_missing(self):
        """Better a clear error than a FileNotFoundError from subprocess."""
        p = UhubctlPower(uhubctl="definitely-not-a-real-binary-xyz")
        with pytest.raises(RuntimeError, match="not installed"):
            p.off(POWERED)


class TestNoopPower:
    def test_reports_unavailable_and_unsupported(self):
        p = NoopPower()
        assert p.available is False
        assert p.supports(POWERED) is False

    def test_operations_are_silent_noops(self):
        p = NoopPower()
        p.on(POWERED)
        p.off(POWERED)
        p.cycle(POWERED, off_seconds=0)


class TestPowerFor:
    def test_noop_when_uhubctl_missing(self):
        p = power_for([POWERED], uhubctl="definitely-not-a-real-binary-xyz")
        assert isinstance(p, NoopPower)

    def test_noop_when_no_board_has_coordinates(self):
        p = power_for([UNPOWERED], uhubctl="sh")
        assert isinstance(p, NoopPower)

    def test_uhubctl_when_binary_and_coordinates_present(self):
        p = power_for([POWERED, UNPOWERED], uhubctl="sh")
        assert isinstance(p, UhubctlPower)
        assert p.available is True
        # the unpowered board in a mixed rig is still individually unsupported
        assert p.supports(UNPOWERED) is False


class TestPowerFixture:
    """The fixture must exist and be safe in sim mode (CI has no hardware)."""

    def test_power_fixture_is_registered(self, pytestconfig):
        plugin = pytestconfig.pluginmanager.get_plugin("alteriom_hil")
        assert plugin is not None, "alteriom_hil plugin not loaded"
        assert hasattr(plugin, "power"), "no `power` fixture in the HIL plugin"

    def test_sim_mode_power_is_noop(self, monkeypatch):
        from alteriom_hil import pytest_plugin

        monkeypatch.setenv("ALTERIOM_HIL_MODE", "sim")
        p = pytest_plugin._resolve_power()
        assert isinstance(p, NoopPower)
        assert p.available is False

    def test_hardware_mode_without_board_map_is_noop(self, monkeypatch):
        from alteriom_hil import pytest_plugin

        monkeypatch.setenv("ALTERIOM_HIL_MODE", "hardware")
        monkeypatch.delenv("ALTERIOM_HIL_BOARD_MAP", raising=False)
        assert isinstance(pytest_plugin._resolve_power(), NoopPower)

    def test_hardware_mode_with_map_but_no_uhubctl_is_noop(
        self, monkeypatch, tmp_path
    ):
        """The common first-bring-up rig: real boards, dumb hub."""
        from alteriom_hil import pytest_plugin

        m = tmp_path / "board-map.yaml"
        m.write_text(
            "boards:\n"
            "  - id: esp32-01\n"
            "    port: /dev/null\n"
            '    power_hub: "1-1"\n'
            "    power_port: 1\n"
        )
        monkeypatch.setenv("ALTERIOM_HIL_MODE", "hardware")
        monkeypatch.setenv("ALTERIOM_HIL_BOARD_MAP", str(m))
        monkeypatch.setenv("PATH", str(tmp_path))  # hide uhubctl
        assert isinstance(pytest_plugin._resolve_power(), NoopPower)

    def test_unparseable_board_map_degrades_to_noop(self, monkeypatch, tmp_path):
        """A broken map must not fail collection — power is a convenience."""
        from alteriom_hil import pytest_plugin

        m = tmp_path / "bad.yaml"
        m.write_text("boards: []\n")
        monkeypatch.setenv("ALTERIOM_HIL_MODE", "hardware")
        monkeypatch.setenv("ALTERIOM_HIL_BOARD_MAP", str(m))
        assert isinstance(pytest_plugin._resolve_power(), NoopPower)


class TestBoardMapFixture:
    def test_sim_mode_has_no_board_map(self, monkeypatch):
        from alteriom_hil import pytest_plugin

        monkeypatch.setenv("ALTERIOM_HIL_MODE", "sim")
        assert pytest_plugin._load_board_map() is None

    def test_hardware_mode_loads_the_map(self, monkeypatch, tmp_path):
        from alteriom_hil import pytest_plugin

        m = tmp_path / "board-map.yaml"
        m.write_text("boards:\n  - id: esp32-01\n    port: /dev/null\n")
        monkeypatch.setenv("ALTERIOM_HIL_MODE", "hardware")
        monkeypatch.setenv("ALTERIOM_HIL_BOARD_MAP", str(m))
        rig = pytest_plugin._load_board_map()
        assert rig is not None and len(rig) == 1
