import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = Path(__file__).resolve().parents[1] / "rig" / "alteriom_hil" / "health_check.py"
SPEC = importlib.util.spec_from_file_location("health_check", MODULE_PATH)
health_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(health_check)


def test_render_exposes_overall_and_each_check():
    payload = {
        "status": "degraded",
        "timestamp": "2026-08-31T00:00:00+00:00",
        "checks": [
            {"name": "runner_service", "status": "ok", "message": "active"},
            {"name": "board_map", "status": "degraded", "message": "waiting"},
        ],
    }
    text = health_check.render(payload)
    assert "HIL service: DEGRADED" in text
    assert "runner_service: active" in text
    assert "board_map: waiting" in text


def test_atomic_status_write(tmp_path):
    path = tmp_path / "status.json"
    payload = {"status": "ok", "checks": []}
    health_check.write_atomic(path, payload)
    assert path.read_text().endswith("\n")
    assert '"status": "ok"' in path.read_text()


def test_pi_throttle_history_is_visible_but_not_a_current_failure():
    result = health_check.classify_throttling(0, "throttled=0x80000")
    assert result["status"] == "ok"
    assert result["current_flags"] == 0
    assert result["historical_flags"] == 8
    assert "latched history" in result["message"]


def test_current_pi_throttling_is_a_fault_and_says_which():
    """One check with the flag set is a fault -- named, with its bits -- and
    on its own a dip rather than a supply that is down (below)."""
    result = health_check.classify_throttling(0, "throttled=0x80008")
    assert result["status"] == "degraded"
    assert result["current_flags"] == 8
    assert "faulting_since" in result


def test_missing_hardware_is_degraded_not_unhealthy(tmp_path, monkeypatch):
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(
        health_check.shutil, "which", lambda _name, **_kwargs: "/usr/bin/tool"
    )
    monkeypatch.setattr(
        health_check.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=100, used=10),
    )

    def fake_command(*args, **_kwargs):
        if args[:2] == ("systemctl", "is-active"):
            return 0, "active"
        if args[0] == "vcgencmd":
            return 0, "throttled=0x0"
        return 1, "No compatible devices detected"

    monkeypatch.setattr(health_check, "command", fake_command)
    payload = health_check.collect_health(
        {
            "ALTERIOM_HIL_MODE": "hardware",
            "ALTERIOM_HIL_VENV": str(tmp_path / "venv"),
            "ALTERIOM_HIL_BOARD_MAP": str(tmp_path / "board-map.yaml"),
            "HIL_RUNNER_UNIT": "actions.runner.example.service",
        }
    )
    assert payload["status"] == "degraded"
    assert not [check for check in payload["checks"] if check["status"] == "unhealthy"]


def test_a_node_without_a_runner_is_not_unhealthy_for_it(monkeypatch):
    monkeypatch.setattr(health_check, "command", lambda *args, **kwargs: (1, ""))
    checks = {check["name"]: check for check in health_check.collect_health({"ALTERIOM_HIL_FARM_MODE": "node"})["checks"]}
    assert checks["runner_service"]["status"] == "ok"
    checks = {check["name"]: check for check in health_check.collect_health({})["checks"]}
    assert checks["runner_service"]["status"] == "unhealthy", "a host that is not a node still needs its runner"


def test_invalid_service_configuration_is_unhealthy(tmp_path, monkeypatch):
    monkeypatch.setattr(health_check.shutil, "which", lambda _name, **_kwargs: None)
    monkeypatch.setattr(
        health_check.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=100, used=10),
    )
    payload = health_check.collect_health({})
    assert payload["status"] == "unhealthy"


def test_single_available_board_is_degraded_for_mesh(tmp_path, monkeypatch):
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    board_map = tmp_path / "board-map.yaml"
    board_port = tmp_path / "board-port"
    board_port.touch()
    board_map.write_text(
        "boards:\n"
        f"  - {{id: esp32-01, port: {board_port}, power_hub: 1-1, power_port: 1}}\n"
    )
    monkeypatch.setattr(
        health_check.shutil, "which", lambda _name, **_kwargs: "/usr/bin/tool"
    )
    monkeypatch.setattr(health_check.stat, "S_ISCHR", lambda _mode: True)
    monkeypatch.setattr(health_check.os, "access", lambda *_args: True)
    monkeypatch.setattr(
        health_check.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=100, used=10),
    )

    def fake_command(*args, **_kwargs):
        if args[:2] == ("systemctl", "is-active"):
            return 0, "active"
        if args[0] == "vcgencmd":
            return 0, "throttled=0x0"
        return 0, "Current status for hub 1-1"

    monkeypatch.setattr(health_check, "command", fake_command)
    payload = health_check.collect_health(
        {
            "ALTERIOM_HIL_MODE": "hardware",
            "ALTERIOM_HIL_VENV": str(tmp_path / "venv"),
            "ALTERIOM_HIL_BOARD_MAP": str(board_map),
            "ALTERIOM_HIL_MINIMUM_BOARDS": "2",
            "HIL_RUNNER_UNIT": "actions.runner.example.service",
        }
    )
    board_check = next(check for check in payload["checks"] if check["name"] == "board_map")
    assert payload["status"] == "degraded"
    assert board_check["message"] == "1 board(s) available; 2 required"


def test_available_board_without_independent_power_is_healthy(tmp_path, monkeypatch):
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    board_map = tmp_path / "board-map.yaml"
    board_port_1 = tmp_path / "board-port-1"
    board_port_2 = tmp_path / "board-port-2"
    board_port_1.touch()
    board_port_2.touch()
    board_map.write_text(
        "boards:\n"
        f"  - {{id: esp32-01, port: {board_port_1}, power_hub: 1-1, power_port: 1}}\n"
        f"  - {{id: esp32-c3-01, port: {board_port_2}}}\n"
    )
    monkeypatch.setattr(
        health_check.shutil, "which", lambda _name, **_kwargs: "/usr/bin/tool"
    )
    monkeypatch.setattr(health_check.stat, "S_ISCHR", lambda _mode: True)
    monkeypatch.setattr(health_check.os, "access", lambda *_args: True)
    monkeypatch.setattr(
        health_check.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=100, used=10),
    )

    def fake_command(*args, **_kwargs):
        if args[:2] == ("systemctl", "is-active"):
            return 0, "active"
        if args[0] == "vcgencmd":
            return 0, "throttled=0x0"
        return 0, "Current status for hub 1-1"

    monkeypatch.setattr(health_check, "command", fake_command)
    payload = health_check.collect_health(
        {
            "ALTERIOM_HIL_MODE": "hardware",
            "ALTERIOM_HIL_VENV": str(tmp_path / "venv"),
            "ALTERIOM_HIL_BOARD_MAP": str(board_map),
            "ALTERIOM_HIL_MINIMUM_BOARDS": "2",
            "HIL_RUNNER_UNIT": "actions.runner.example.service",
        }
    )
    board_check = next(check for check in payload["checks"] if check["name"] == "board_map")
    assert payload["status"] == "ok"
    assert board_check["message"] == (
        "2 board(s) available; independent power unavailable for: esp32-c3-01"
    )
    assert board_check["missing_power"] == ["esp32-c3-01"]
    usb_check = next(check for check in payload["checks"] if check["name"] == "usb_power")
    assert "independent switching is reported per board" in usb_check["message"]


def test_enabled_gateway_reports_service_credentials_and_endpoint(tmp_path, monkeypatch):
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    password = tmp_path / "gateway-password"
    password.write_text("a-valid-test-password\n")

    monkeypatch.setattr(
        health_check.shutil, "which", lambda _name, **_kwargs: "/usr/bin/tool"
    )
    monkeypatch.setattr(
        health_check.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=100, used=10),
    )

    def fake_command(*args, **_kwargs):
        if args[:2] == ("systemctl", "is-active"):
            return 0, "active"
        if args[0] == "vcgencmd":
            return 0, "throttled=0x0"
        return 1, "No compatible devices detected"

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"status":"ok","features":["ledger.count","retry_after"]}'

    monkeypatch.setattr(health_check, "command", fake_command)
    monkeypatch.setattr(health_check.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    payload = health_check.collect_health(
        {
            "ALTERIOM_HIL_MODE": "hardware",
            "ALTERIOM_HIL_VENV": str(tmp_path / "venv"),
            "ALTERIOM_HIL_BOARD_MAP": str(tmp_path / "board-map.yaml"),
            "HIL_RUNNER_UNIT": "actions.runner.example.service",
            "ALTERIOM_HIL_GATEWAY_ENABLED": "1",
            "ALTERIOM_HIL_WIFI_PASSWORD_FILE": str(password),
            "ALTERIOM_HIL_GATEWAY_ENDPOINT": "http://10.42.0.1:8088",
        }
    )
    gateway_checks = {
        check["name"]: check for check in payload["checks"] if check["name"].startswith("gateway_")
    }
    assert gateway_checks["gateway_service"]["status"] == "ok"
    assert gateway_checks["gateway_credentials"]["status"] == "ok"
    assert gateway_checks["gateway_endpoint"]["status"] == "ok"
    assert "features: ledger.count, retry_after" in gateway_checks["gateway_endpoint"]["message"]


# `iw dev` as a Pi answers it: the AP interface beside a managed one.
IW_DEV = (
    "Interface wlan1\n\tifindex 4\n\ttype managed\n"
    "Interface wlan0\n\tifindex 3\n\ttype AP\n"
    "\tchannel 1 (2412 MHz), width: 20 MHz\n"
)


def test_the_gateway_ap_must_be_on_the_mesh_channel(tmp_path, monkeypatch):
    """A board promoted to bridge follows this AP's channel while the rest of
    the mesh stays where it was rooted. On two channels a run's mesh splits in
    half and a transfer spanning the split dies -- a rig fault nobody can read
    out of the suite's failure, so the rig says it here."""
    password = tmp_path / "gateway-password"
    password.write_text("a-valid-test-password\n")
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    (tmp_path / "venv" / "bin" / "python").touch()
    monkeypatch.setattr(health_check.shutil, "which", lambda _name, **_kwargs: "/usr/bin/tool")
    monkeypatch.setattr(
        health_check.shutil, "disk_usage", lambda _path: SimpleNamespace(total=100, used=10)
    )

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"status":"ok"}'

    monkeypatch.setattr(
        health_check.urllib.request, "urlopen", lambda *_args, **_kwargs: Response()
    )

    def check(iw_code, iw_output, wanted="1"):
        def fake_command(*args, **_kwargs):
            if args[:2] == ("systemctl", "is-active"):
                return 0, "active"
            if args[0] == "iw":
                return iw_code, iw_output
            return 1, "No compatible devices detected"

        monkeypatch.setattr(health_check, "command", fake_command)
        payload = health_check.collect_health({
            "ALTERIOM_HIL_MODE": "hardware",
            "ALTERIOM_HIL_VENV": str(tmp_path / "venv"),
            "ALTERIOM_HIL_BOARD_MAP": str(tmp_path / "board-map.yaml"),
            "HIL_RUNNER_UNIT": "actions.runner.example.service",
            "ALTERIOM_HIL_GATEWAY_ENABLED": "1",
            "ALTERIOM_HIL_WIFI_PASSWORD_FILE": str(password),
            "ALTERIOM_HIL_GATEWAY_ENDPOINT": "http://10.42.0.1:8088",
            "ALTERIOM_HIL_GATEWAY_CHANNEL": wanted,
        })
        return next(c for c in payload["checks"] if c["name"] == "gateway_channel")

    together = check(0, IW_DEV)
    assert together["status"] == "ok" and together["channel"] == 1

    # The rig this was written for: an AP on 6, a mesh rooted on 1.
    split = check(0, IW_DEV.replace("channel 1 (2412", "channel 6 (2437"))
    assert split["status"] == "unhealthy"
    assert "channel 6" in split["message"] and "split the mesh" in split["message"]

    # No radio to read, or no AP on it: worth saying, not worth calling the rig
    # broken -- the service and endpoint rows answer for the AP itself.
    assert check(127, "iw: command not found")["status"] == "degraded"
    assert check(0, "Interface wlan0\n\ttype managed\n")["status"] == "degraded"


def test_the_ap_channel_is_read_from_the_radio():
    assert health_check.ap_channel(IW_DEV) == 1
    # A channel on an interface that is not an AP is not the AP's channel.
    assert health_check.ap_channel("Interface wlan0\n\ttype managed\n\tchannel 11\n") is None
    assert health_check.ap_channel("") is None


def test_enabled_farm_service_reports_process_endpoint_and_tls_bootstrap(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(health_check, "NGINX_SITE_PATH", tmp_path / "missing-nginx-site")
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(
        health_check.shutil, "which", lambda _name, **_kwargs: "/usr/bin/tool"
    )
    monkeypatch.setattr(
        health_check.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=100, used=10),
    )

    def fake_command(*args, **_kwargs):
        if args[:2] == ("systemctl", "is-active"):
            return 0, "active"
        if args[0] == "vcgencmd":
            return 0, "throttled=0x0"
        return 1, "No compatible devices detected"

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(health_check, "command", fake_command)
    monkeypatch.setattr(
        health_check.urllib.request, "urlopen", lambda *_args, **_kwargs: Response()
    )
    payload = health_check.collect_health(
        {
            "ALTERIOM_HIL_MODE": "hardware",
            "ALTERIOM_HIL_VENV": str(tmp_path / "venv"),
            "ALTERIOM_HIL_BOARD_MAP": str(tmp_path / "board-map.yaml"),
            "HIL_RUNNER_UNIT": "actions.runner.example.service",
            "ALTERIOM_HIL_SERVICE_ENABLED": "1",
            "ALTERIOM_HIL_API_BIND": "127.0.0.1",
            "ALTERIOM_HIL_API_PORT": "8090",
            "ALTERIOM_HIL_PUBLIC_HOST": "hil.example.com",
        }
    )
    checks = {check["name"]: check for check in payload["checks"]}
    assert checks["farm_service"]["status"] == "ok"
    assert checks["farm_endpoint"]["status"] == "ok"
    assert checks["reverse_proxy"]["status"] == "degraded"
    assert checks["reverse_proxy"]["certificate_ready"] is False


def test_lan_reverse_proxy_is_ready_without_public_certificate(tmp_path, monkeypatch):
    site = tmp_path / "alteriom-hil"
    site.write_text(
        "allow 192.168.1.0/24;\nproxy_pass http://127.0.0.1:8090;\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(health_check, "NGINX_SITE_PATH", site)
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(
        health_check.shutil, "which", lambda _name, **_kwargs: "/usr/bin/tool"
    )
    monkeypatch.setattr(
        health_check.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=100, used=10),
    )
    monkeypatch.setattr(
        health_check,
        "command",
        lambda *_args, **_kwargs: (0, "active"),
    )

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        health_check.urllib.request, "urlopen", lambda *_args, **_kwargs: Response()
    )
    payload = health_check.collect_health(
        {
            "ALTERIOM_HIL_MODE": "hardware",
            "ALTERIOM_HIL_VENV": str(tmp_path / "venv"),
            "ALTERIOM_HIL_BOARD_MAP": str(tmp_path / "board-map.yaml"),
            "HIL_RUNNER_UNIT": "actions.runner.example.service",
            "ALTERIOM_HIL_SERVICE_ENABLED": "1",
            "ALTERIOM_HIL_PUBLIC_HOST": "hil.example.com",
        }
    )
    proxy = next(
        check for check in payload["checks"] if check["name"] == "reverse_proxy"
    )
    assert proxy["status"] == "ok"
    assert proxy["ingress_mode"] == "lan"
    assert proxy["certificate_ready"] is False


FAKE_LINK = "https://api.callmebot.com/whatsapp.php?phone=+10000000000&apikey=000000"


def _provider_health(monkeypatch, url_file, route=None):
    monkeypatch.setattr(health_check.shutil, "which", lambda _name, **_kwargs: "/usr/bin/tool")
    monkeypatch.setattr(health_check.shutil, "disk_usage", lambda _path: SimpleNamespace(total=100, used=10))
    monkeypatch.setattr(health_check, "command", lambda *args, **_kwargs: (0, "active"))
    routes = []

    def fake_route(host, port, timeout):
        routes.append((host, port, timeout))
        return route

    monkeypatch.setattr(health_check, "route_problem", fake_route)
    payload = health_check.collect_health({
        "ALTERIOM_HIL_MODE": "hardware",
        "HIL_RUNNER_UNIT": "actions.runner.example.service",
        "ALTERIOM_HIL_CALLMEBOT_URL_FILE": str(url_file),
        "ALTERIOM_HIL_CALLMEBOT_SEND": "release",
    })
    checks = {check["name"]: check for check in payload["checks"] if check["name"].startswith("provider_")}
    return checks, routes


def test_a_rigs_callmebot_link_and_route_are_reported_without_the_link(tmp_path, monkeypatch):
    import os

    link = tmp_path / "callmebot-url"
    link.write_text(FAKE_LINK + "\n")
    os.chmod(link, 0o640)
    checks, routes = _provider_health(monkeypatch, link)
    assert routes == [("api.callmebot.com", 443, 3.0)]
    assert checks["provider_callmebot"]["status"] == "ok"
    assert checks["provider_callmebot"]["message"] == (
        "https://api.callmebot.com/whatsapp.php?phone=***00&apikey=*** (send: release)"
    )
    assert checks["provider_callmebot_route"]["status"] == "ok"

    os.chmod(link, 0o644)
    checks, _ = _provider_health(monkeypatch, link, route="api.callmebot.com does not resolve: nope")
    assert checks["provider_callmebot"]["status"] == "unhealthy"
    assert "every user" in checks["provider_callmebot"]["message"]
    assert checks["provider_callmebot_route"]["status"] == "degraded", "a service's bad minute is not an outage"
    assert "does not resolve" in checks["provider_callmebot_route"]["message"]

    link.write_text(FAKE_LINK + "&text=hi\n")
    os.chmod(link, 0o640)
    checks, _ = _provider_health(monkeypatch, link)
    assert checks["provider_callmebot"]["status"] == "unhealthy"
    for check in checks.values():
        assert "000000" not in check["message"] and "10000000000" not in check["message"]

    checks, _ = _provider_health(monkeypatch, tmp_path / "missing")
    assert checks["provider_callmebot"]["status"] == "unhealthy"
    assert "does not exist" in checks["provider_callmebot"]["message"]


def test_a_rig_without_a_provider_has_no_provider_checks(monkeypatch):
    monkeypatch.setattr(health_check.shutil, "which", lambda _name, **_kwargs: "/usr/bin/tool")
    monkeypatch.setattr(health_check.shutil, "disk_usage", lambda _path: SimpleNamespace(total=100, used=10))
    monkeypatch.setattr(health_check, "command", lambda *args, **_kwargs: (0, "active"))

    def no_network(*_args):
        raise AssertionError("a rig without a provider checks no route")

    monkeypatch.setattr(health_check, "route_problem", no_network)
    payload = health_check.collect_health({"ALTERIOM_HIL_MODE": "hardware", "HIL_RUNNER_UNIT": "x.service"})
    assert not [check for check in payload["checks"] if check["name"].startswith("provider_")]


def test_a_rig_that_cannot_read_its_throttling_is_not_called_broken():
    """/dev/vcio is root:video. On a host whose rig user is not in that group
    vcgencmd cannot open it, and reporting the whole rig UNHEALTHY over it
    reads as failing hardware -- and fails the release install, because a
    release does not install onto an unhealthy host (rig02, 2026-09-16)."""
    denied = health_check.classify_throttling(
        1,
        "Can't open device file: /dev/vcio\n"
        "Try creating a device file with: sudo mknod /dev/vcio c 100 0",
    )
    assert denied["status"] == "degraded"
    assert "video group" in denied["message"]

    # Something else vcgencmd could not answer is still a fault, and so is a
    # throttling flag it did answer with.
    assert health_check.classify_throttling(1, "VCHI initialization failed")["status"] == "unhealthy"
    assert health_check.classify_throttling(0, "throttled=0x5")["status"] == "degraded"
    # And the one that could not be read starts no spell for the next check.
    assert health_check.power_fault_since({"timestamp": "2026-09-22T10:00:00+00:00",
                                           "checks": [{"name": "pi_power", **denied}]}) is None

def test_the_host_says_what_it_is_doing(tmp_path):
    """A rig is a computer in a cupboard. The farm knew whether its disk was
    full and whether the Pi had throttled, and nothing else -- so "the runs got
    slow" and "it started swapping on Tuesday" were not questions the dashboard
    could answer."""
    (tmp_path / "loadavg").write_text("1.50 1.20 0.90 2/431 1234\n", encoding="utf-8")
    (tmp_path / "meminfo").write_text(
        "MemTotal:        4077532 kB\nMemFree:          102052 kB\nMemAvailable:     407753 kB\n",
        encoding="utf-8",
    )
    (tmp_path / "uptime").write_text("788696.12 3100000.00\n", encoding="utf-8")
    (tmp_path / "temp").write_text("50700\n", encoding="utf-8")

    metrics = health_check.host_metrics(
        loadavg=str(tmp_path / "loadavg"), meminfo=str(tmp_path / "meminfo"),
        uptime=str(tmp_path / "uptime"), thermal=str(tmp_path / "temp"), cpus=4,
    )
    assert metrics["load_1"] == 1.5 and metrics["load_per_cpu"] == 0.38 and metrics["cpus"] == 4
    assert metrics["memory_used_percent"] == 90
    assert metrics["temperature_c"] == 50.7
    assert metrics["uptime_seconds"] == 788696
    assert "load 1.50 on 4 CPU(s)" in metrics["message"]
    assert "memory 90% used of 4.2 GB" in metrics["message"] and "up 9d" in metrics["message"]
    # Memory this full is where a flash or a serial capture starts failing in
    # ways that look like the boards' fault.
    assert metrics["status"] == "degraded"


def test_the_thresholds_are_a_rigs_not_a_desktops(tmp_path):
    """A Pi throttles from 80 degrees and stops being a reliable timing
    reference well before that."""
    (tmp_path / "loadavg").write_text("0.10 0.10 0.10 1/1 1\n", encoding="utf-8")
    (tmp_path / "meminfo").write_text("MemTotal: 4000000 kB\nMemAvailable: 3000000 kB\n",
                                      encoding="utf-8")

    def at(celsius):
        (tmp_path / "temp").write_text(f"{int(celsius * 1000)}\n", encoding="utf-8")
        return health_check.host_metrics(
            loadavg=str(tmp_path / "loadavg"), meminfo=str(tmp_path / "meminfo"),
            uptime=str(tmp_path / "nothing"), thermal=str(tmp_path / "temp"), cpus=4,
        )["status"]

    assert at(45) == "ok"
    assert at(72) == "degraded"
    assert at(81) == "unhealthy"

    # A host that can say nothing about itself says that, rather than zeros.
    nothing = health_check.host_metrics(
        loadavg=str(tmp_path / "gone"), meminfo=str(tmp_path / "gone"),
        uptime=str(tmp_path / "gone"), thermal=str(tmp_path / "gone"),
    )
    assert nothing["status"] == "degraded" and "no metrics" in nothing["message"]

def test_a_rig_that_cannot_take_its_lock_says_so(tmp_path):
    """Every run holds the rig lock. It lives on a tmpfs, so it is gone after
    each boot and made again by a tmpfiles rule -- and a rig where that rule is
    missing, or where the lock ended up somewhere `ProtectSystem=strict` cannot
    write, fails every run with an errno nobody reads until they open the
    journal. rig02 spent a morning that way (2026-09-16)."""
    lock = tmp_path / "alteriom-hil.lock"
    fine = health_check.rig_lock(str(lock))
    assert fine["status"] == "ok" and str(lock) in fine["message"]
    assert lock.exists(), "a lock that was not there is made, as the tmpfiles rule does"

    # Nowhere to make it: the state a rig is in when the tmpfiles rule that
    # creates the lock directory is missing, and the state the service was in
    # when it was looking in a directory it could not write.
    refused = health_check.rig_lock(str(tmp_path / "not-a-directory" / "alteriom-hil.lock"))
    assert refused["status"] == "unhealthy"
    assert "every run needs this lock" in refused["message"]


def test_the_service_and_the_cli_take_the_same_lock():
    """They took two different ones: the service `/var/lock`, everything else
    `/run/lock`. On Raspberry Pi OS the first is a symlink to the second and
    the difference never showed; on the Ubuntu image rig02 runs it is a
    directory of its own, and the service could not write to it at all."""

    # The launcher is still under runner/; the CLI and the health check are
    # modules of the rig's distribution (docs/public-release-plan.md, 12d).
    RUNNER = MODULE_PATH.parents[2] / "runner"

    def module(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        return loaded

    from alteriom_hil import farm_shared

    service = module("farm_service_for_lock", RUNNER / "farm_service.py")
    cli = module("admin_cli_for_lock", MODULE_PATH.with_name("admin_cli.py"))
    # One lock, defined once (alteriom_hil.farm_shared) and taken from there by
    # the dispatcher, a run and the CLI alike.
    assert str(service.farm_shared.RIG_LOCK_PATH) == str(cli.RIG_LOCK) == "/run/lock/alteriom-hil.lock"
    assert cli.RIG_LOCK is farm_shared.RIG_LOCK_PATH
    # And the installer makes that one, on every boot.
    installer = (RUNNER / "install-health-service.sh").read_text(encoding="utf-8")
    assert "f /run/lock/alteriom-hil.lock" in installer
    assert "ReadWritePaths=/var/lib/alteriom-hil /run/lock" in installer


def test_a_supply_that_dips_is_said_so_and_a_supply_that_stays_down_is_unhealthy():
    """The flag is a comparator against a threshold, so a supply sitting near
    it reads set in one sample and clear in the next -- esp32-hil measured
    4.63-4.79 V against 4.8 V. One glance decided by coin toss, and a rig
    that is demonstrably running was called broken every other deploy.

    Nor do three glances 0.4 s apart decide it: the same rig's kernel log has
    326 dips in 12 hours and none shorter than 2 s, so every one of them
    outlasts the samples, and the check read "down" at one check and
    "dipping" at the next for the same rail (2026-09-22, 10:07-10:47). What
    separates a dip from a supply that is down is the fault still being there
    at the next check, and that is what decides it."""
    now = datetime(2026, 9, 22, 14, 10, tzinfo=timezone.utc)
    all_three = (0, "throttled=0xf0005"), (0, "throttled=0xf0005"), (0, "throttled=0xf0005")

    # First seen now, in every reading: a dip until proven otherwise.
    first = health_check.classify_throttling(*all_three[0], *all_three[1:], now=now)
    assert first["status"] == "degraded" and first["readings_faulted"] == 3
    assert first["faulting_since"] == now.isoformat() and first["persisted_seconds"] == 0

    # Still there one health interval later: that is the supply.
    later = now + timedelta(minutes=5)
    down = health_check.classify_throttling(
        *all_three[0], *all_three[1:], since=first["faulting_since"], now=later)
    assert down["status"] == "unhealthy"
    assert "persisting for 5 min" in down["message"] and "not carrying the rig" in down["message"]
    # The spell keeps its start, so the next check reads the same way.
    assert down["faulting_since"] == first["faulting_since"] and down["persisted_seconds"] == 300
    again = health_check.classify_throttling(
        0, "throttled=0xf0005", since=down["faulting_since"], now=later + timedelta(minutes=5))
    assert again["status"] == "unhealthy" and again["persisted_seconds"] == 600

    # The deploy's own check runs seconds after the timer's. Two looks at
    # one dip are still one dip.
    soon = health_check.classify_throttling(
        *all_three[0], *all_three[1:], since=first["faulting_since"], now=now + timedelta(seconds=3))
    assert soon["status"] == "degraded" and soon["persisted_seconds"] == 3

    dips = health_check.classify_throttling(
        0, "throttled=0xf0005", (0, "throttled=0xf0000"), (0, "throttled=0xf0000"), now=now)
    assert dips["status"] == "degraded", "a rig that runs is not broken"
    assert dips["readings_faulted"] == 1 and dips["readings"] == 3
    assert "dips under load" in dips["message"] and "marginal" in dips["message"]
    # The fault is still named, and the history still carried.
    assert dips["current_flags"] == 5 and dips["historical_flags"] == 0xF

    # A dip that shows up in a later reading rather than the first is the
    # same condition, and reads the same way; and one reading in three at a
    # check five minutes into a spell is the spell, not a fresh dip.
    later_reading = health_check.classify_throttling(
        0, "throttled=0xf0000", (0, "throttled=0xf0005"), (0, "throttled=0xf0000"),
        since=first["faulting_since"], now=later)
    assert later_reading["status"] == "unhealthy" and later_reading["readings_faulted"] == 1

    # A clear reading ends the spell: no start is carried, so the next fault
    # is a new dip.
    clear = health_check.classify_throttling(
        0, "throttled=0x0", (0, "throttled=0x0"), (0, "throttled=0x0"), since=first["faulting_since"])
    assert clear["status"] == "ok" and "faulting_since" not in clear

    # A start the previous check wrote that cannot be read is not a start.
    odd = health_check.classify_throttling(0, "throttled=0x5", since="yesterday-ish", now=now)
    assert odd["status"] == "degraded" and odd["faulting_since"] == now.isoformat()


def test_the_spell_is_read_from_the_previous_snapshot():
    """The record of when the fault began is status.json, which the five-minute
    unit and the deploy's check both write and both read back."""
    snapshot = lambda check: {"timestamp": "2026-09-22T14:00:00+00:00", "checks": [
        {"name": "disk", "status": "ok", "message": "x"}, check]}
    assert health_check.power_fault_since(None) is None
    assert health_check.power_fault_since({}) is None
    assert health_check.power_fault_since(snapshot(
        {"name": "pi_power", "status": "ok", "message": "x", "current_flags": 0})) is None
    assert health_check.power_fault_since(snapshot(
        {"name": "pi_power", "status": "degraded", "message": "x", "current_flags": 5,
         "faulting_since": "2026-09-22T13:50:00+00:00"})) == "2026-09-22T13:50:00+00:00"
    # A snapshot written before the start was recorded: the spell began no
    # later than that snapshot.
    assert health_check.power_fault_since(snapshot(
        {"name": "pi_power", "status": "unhealthy", "message": "x", "current_flags": 5,
         "readings_faulted": 3})) == "2026-09-22T14:00:00+00:00"
    # A snapshot with no pi_power at all (a host without vcgencmd).
    assert health_check.power_fault_since({"timestamp": "2026-09-22T14:00:00+00:00",
                                           "checks": [{"name": "disk", "status": "ok"}]}) is None


def test_the_deploy_check_and_the_timer_share_the_spell(monkeypatch, tmp_path):
    """collect_health is handed the snapshot the last check wrote, from the
    same --output the next one will write, so the deploy's check five minutes
    into a spell reads the spell and the timer's after it reads what the
    deploy's wrote."""
    seen = {}

    def fake_collect(env=None, previous=None):
        seen["previous"] = previous
        return {"status": "ok", "timestamp": "2026-09-22T14:05:00+00:00", "checks": []}

    monkeypatch.setattr(health_check, "collect_health", fake_collect)
    output = tmp_path / "status.json"
    assert health_check.main(["--output", str(output)]) == 0
    assert seen["previous"] is None, "no snapshot yet"
    output.write_text(json.dumps({"timestamp": "2026-09-22T14:00:00+00:00", "checks": [
        {"name": "pi_power", "status": "degraded", "message": "x", "current_flags": 5,
         "faulting_since": "2026-09-22T14:00:00+00:00"}]}), encoding="utf-8")
    assert health_check.main(["--output", str(output)]) == 0
    assert health_check.power_fault_since(seen["previous"]) == "2026-09-22T14:00:00+00:00"
    # Whatever is in the file that is not a snapshot is no snapshot.
    output.write_text("[]", encoding="utf-8")
    assert health_check.main(["--output", str(output)]) == 0
    assert seen["previous"] is None
    # Without --output there is no record, so a fault is at most a dip.
    assert health_check.main([]) == 0 and seen["previous"] is None


def test_a_flapping_supply_reads_one_way_through_the_whole_check(monkeypatch, tmp_path):
    """End to end: the rail under threshold at two checks five minutes apart
    is unhealthy on the rig's own page and in its notification, and still
    not a failed install (--except-supply)."""
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(health_check.shutil, "which", lambda _name, **_kwargs: "/usr/bin/tool")
    monkeypatch.setattr(health_check.shutil, "disk_usage", lambda _path: SimpleNamespace(total=100, used=10))
    monkeypatch.setattr(health_check, "THROTTLE_SAMPLE_GAP", 0)

    def fake_command(*args, **_kwargs):
        if args[:2] == ("systemctl", "is-active"):
            return 0, "active"
        if args[0] == "vcgencmd":
            return 0, "throttled=0xf0005"
        return 1, "No compatible devices detected"

    monkeypatch.setattr(health_check, "command", fake_command)
    env = {
        "ALTERIOM_HIL_MODE": "hardware",
        "ALTERIOM_HIL_VENV": str(tmp_path / "venv"),
        "ALTERIOM_HIL_BOARD_MAP": str(tmp_path / "board-map.yaml"),
        "HIL_RUNNER_UNIT": "actions.runner.example.service",
        "ALTERIOM_HIL_RIG_LOCK": str(tmp_path / "rig.lock"),
    }
    first = health_check.collect_health(env)
    power = {check["name"]: check for check in first["checks"]}["pi_power"]
    assert power["status"] == "degraded" and power["readings_faulted"] == 3

    # Five minutes on, the previous snapshot says the fault began then.
    began = datetime.fromisoformat(power["faulting_since"]) - timedelta(minutes=5)
    power["faulting_since"] = began.isoformat()
    second = health_check.collect_health(env, previous=first)
    power = {check["name"]: check for check in second["checks"]}["pi_power"]
    assert power["status"] == "unhealthy" and second["status"] == "unhealthy"
    assert power["faulting_since"] == began.isoformat()

    # The deploy gate reads the same snapshot and still does not call the
    # release failed for it.
    monkeypatch.setattr(health_check, "collect_health", lambda env=None, previous=None: second)
    assert health_check.main(["--fail-unhealthy", "--except-supply"]) == 0
    assert health_check.main(["--fail-unhealthy"]) == 1


def test_the_supply_is_sampled_more_than_once(monkeypatch):
    seen = []

    def fake(*args, **kwargs):
        seen.append(args)
        return 0, "throttled=0x0"

    monkeypatch.setattr(health_check, "command", fake)
    monkeypatch.setattr(health_check, "THROTTLE_SAMPLE_GAP", 0)
    readings = health_check.read_throttling()
    assert len(readings) == health_check.THROTTLE_SAMPLES >= 2
    assert all(args == ("vcgencmd", "get_throttled") for args in seen)

    # A host with no vcgencmd at all is asked once, not three times.
    seen.clear()
    monkeypatch.setattr(health_check, "command", lambda *a, **k: (seen.append(a), (127, ""))[1])
    assert health_check.read_throttling() == [(127, "")] and len(seen) == 1


def test_a_power_supply_fault_does_not_say_the_release_failed_to_install(monkeypatch, tmp_path, capsys):
    """A rig's supply is the host's standing condition. Reporting it as
    "could not install the current release" is untrue, and it is the kind of
    red that hides a real one."""
    def payload(status, name):
        return {
            "status": status,
            "timestamp": "2026-09-22T00:00:00Z",
            "checks": [{"name": name, "status": status, "message": "x"}],
        }

    monkeypatch.setattr(health_check, "collect_health", lambda **_: payload("unhealthy", "pi_power"))
    assert health_check.main(["--fail-unhealthy"]) == 1, "on its own it is still a fault"
    assert health_check.main(["--fail-unhealthy", "--except-supply"]) == 0

    # Anything else unhealthy still fails the install, supply exception or not.
    monkeypatch.setattr(health_check, "collect_health", lambda **_: payload("unhealthy", "farm_service"))
    assert health_check.main(["--fail-unhealthy", "--except-supply"]) == 1
    # And --strict is unchanged: it fails on anything short of ok.
    monkeypatch.setattr(health_check, "collect_health", lambda **_: payload("degraded", "pi_power"))
    assert health_check.main(["--strict", "--except-supply"]) == 1
    assert health_check.main(["--fail-unhealthy", "--except-supply"]) == 0


def test_the_deploy_takes_the_supply_exception_and_nothing_else_does():
    update = (MODULE_PATH.parents[2] / "runner" / "update-runner.sh").read_text(encoding="utf-8")
    assert "--fail-unhealthy --except-supply" in update
    assert health_check.HOST_SUPPLY_CHECKS == frozenset({"pi_power"}), (
        "widening this is widening what a deploy may not conclude from"
    )
