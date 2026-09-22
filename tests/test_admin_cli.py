import json
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest
import yaml


RUNNER = Path(__file__).resolve().parents[1] / "runner"
# The rig's own scripts, examples and schemas, which are beside its package
# now (docs/public-release-plan.md, step 12f).
RIG = Path(__file__).resolve().parents[1] / "rig"
# The admin CLI is a module of the rig's distribution now, with a console
# script (docs/public-release-plan.md, step 12d); run here as a module so
# the test does not need the entry point installed.
CLI = Path(__file__).resolve().parents[1] / "rig" / "alteriom_hil" / "admin_cli.py"
EXAMPLE = RIG / "hil-config.example.yaml"


def invoke(*args):
    return subprocess.run(
        [sys.executable, str(CLI), "--config", str(EXAMPLE), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_admin_cli_validates_example():
    result = invoke("config", "validate")
    assert result.returncode == 0
    assert "valid:" in result.stdout


def test_admin_cli_shows_machine_readable_config():
    result = invoke("config", "show", "--json")
    assert result.returncode == 0
    assert '"schema": 2' in result.stdout


def test_admin_cli_has_structured_commands():
    result = subprocess.run(
        [sys.executable, str(CLI), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "alteriom-hil-admin" in result.stdout
    assert "{status,config,health,service,keys,notify,backup,providers,boards,instruments}" in result.stdout


def test_board_add_default_tag_does_not_duplicate_explicit_mesh():
    sys.path.insert(0, str(RUNNER))
    from alteriom_hil import admin_cli

    implicit = admin_cli.parser().parse_args(
        [
            "boards",
            "add",
            "--id",
            "one",
            "--port",
            "/dev/one",
            "--target",
            "esp32",
        ]
    )
    explicit = admin_cli.parser().parse_args(
        [
            "boards",
            "add",
            "--id",
            "two",
            "--port",
            "/dev/two",
            "--target",
            "esp32",
            "--tag",
            "mesh",
        ]
    )
    assert implicit.tag == []
    assert explicit.tag == ["mesh"]


def test_admin_cli_adds_validated_board_atomically(tmp_path, monkeypatch):
    sys.path.insert(0, str(RUNNER))
    from alteriom_hil import admin_cli

    board_map = tmp_path / "board-map.yaml"
    config = yaml.safe_load(EXAMPLE.read_text())
    config["paths"]["board_map"] = str(board_map)
    config["paths"]["inventory"] = str(board_map)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    monkeypatch.setattr(admin_cli, "require_root", lambda: None)
    monkeypatch.setattr(admin_cli, "refresh_health", lambda: None)
    monkeypatch.setattr(admin_cli, "publish_after_change", lambda payload: None)
    args = Namespace(
        config=config_path,
        id="esp32-01",
        port="/dev/esp32-farm-01",
        target="esp32",
        power_hub="1-1",
        power_port=1,
        baud=115200,
        flash_baud=460800,
        tag=["mesh"],
    )
    assert admin_cli.command_boards_add(args) == 0
    boards = yaml.safe_load(board_map.read_text())["boards"]
    assert boards[0]["chip"] == "esp32"
    assert boards[0]["target"] == "esp32"


def test_admin_cli_allows_replacement_board_on_stale_registry_port(tmp_path, monkeypatch):
    sys.path.insert(0, str(RUNNER))
    from alteriom_hil import admin_cli

    inventory = tmp_path / "inventory.yaml"
    inventory.write_text(
        "boards:\n"
        "- id: c3\n"
        "  port: /dev/ttyACM0\n"
        "  chip: esp32c3\n"
        "  target: esp32-c3\n"
        "  mac: aa:bb:cc:dd:ee:01\n"
    )
    config = yaml.safe_load(EXAMPLE.read_text())
    config["paths"]["inventory"] = str(inventory)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    monkeypatch.setattr(admin_cli, "require_root", lambda: None)
    monkeypatch.setattr(admin_cli, "refresh_health", lambda: None)
    monkeypatch.setattr(admin_cli, "publish_after_change", lambda payload: None)
    args = Namespace(
        config=config_path,
        id="s3",
        port="/dev/ttyACM0",
        target="esp32-s3",
        mac="aa:bb:cc:dd:ee:02",
        power_hub=None,
        power_port=None,
        baud=115200,
        flash_baud=460800,
        tag=["mesh"],
    )
    assert admin_cli.command_boards_add(args) == 0
    boards = yaml.safe_load(inventory.read_text())["boards"]
    assert [board["id"] for board in boards] == ["c3", "s3"]
    assert boards[0]["port"] == boards[1]["port"] == "/dev/ttyACM0"


def _registry_config(tmp_path):
    inventory = tmp_path / "inventory.yaml"
    inventory.write_text(
        "boards:\n- {id: esp32-01, port: /dev/ttyUSB0, chip: esp32, target: esp32, mac: 'aa:bb:cc:dd:ee:01'}\n"
    )
    config = yaml.safe_load(EXAMPLE.read_text())
    # Off by default here: most of these tests are about what discovery says,
    # and a board that registers itself mid-test changes the subject. The one
    # test that is about registering turns it on.
    config["inventory"] = {"auto_register": False}
    config["paths"]["inventory"] = str(inventory)
    config["paths"]["board_map"] = str(tmp_path / "board-map.active.yaml")
    config["paths"]["state"] = str(tmp_path / "state")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    return config_path, inventory


def test_boards_add_republishes_the_fleet_so_the_dashboard_sees_it(tmp_path, monkeypatch):
    """Registering a board from the CLI must not wait for the next service
    discovery: the active map and the dashboard snapshot are refreshed now."""
    import json
    from contextlib import nullcontext

    sys.path.insert(0, str(RUNNER))
    from alteriom_hil import admin_cli
    from alteriom_hil.inventory import DetectedDevice

    config_path, inventory = _registry_config(tmp_path)
    found = {
        "/dev/ttyUSB0": DetectedDevice("/dev/ttyUSB0", "esp32", "esp32", "aa:bb:cc:dd:ee:01"),
        "/dev/ttyACM2": DetectedDevice("/dev/ttyACM2", "esp32c6", "esp32-c6", "40:4c:ca:41:0f:7c", transport="usb-serial-jtag"),
    }
    monkeypatch.setattr("alteriom_hil.inventory.serial_ports", lambda: sorted(found))
    monkeypatch.setattr("alteriom_hil.inventory.probe_port", lambda port: found[port])
    monkeypatch.setattr(admin_cli, "rig_lock", nullcontext)
    monkeypatch.setattr(admin_cli, "require_root", lambda: None)
    monkeypatch.setattr(admin_cli, "refresh_health", lambda: None)
    args = Namespace(config=config_path, id="esp32-c6-01", port="/dev/ttyACM2", target="esp32-c6",
                     mac="40:4c:ca:41:0f:7c", power_hub=None, power_port=None, baud=115200,
                     flash_baud=460800, tag=None)
    assert admin_cli.command_boards_add(args) == 0

    active = yaml.safe_load((tmp_path / "board-map.active.yaml").read_text())["boards"]
    assert [b["id"] for b in active] == ["esp32-01", "esp32-c6-01"]
    snapshot = json.loads((tmp_path / "state" / "inventory.json").read_text())
    assert [b["id"] for b in snapshot["boards"]] == ["esp32-01", "esp32-c6-01"]
    assert snapshot["unregistered"] == [] and snapshot["missing"] == []

    # and removal republishes too: the device is back to unregistered
    assert admin_cli.command_boards_remove(Namespace(config=config_path, id="esp32-c6-01")) == 0
    snapshot = json.loads((tmp_path / "state" / "inventory.json").read_text())
    assert [d["mac"] for d in snapshot["unregistered"]] == ["40:4c:ca:41:0f:7c"]


def test_boards_add_survives_a_failed_republish(tmp_path, monkeypatch, capsys):
    sys.path.insert(0, str(RUNNER))
    from alteriom_hil import admin_cli

    config_path, inventory = _registry_config(tmp_path)

    def broken_lock():
        raise OSError("no rig lock on this host")

    monkeypatch.setattr(admin_cli, "rig_lock", broken_lock)
    monkeypatch.setattr(admin_cli, "require_root", lambda: None)
    monkeypatch.setattr(admin_cli, "refresh_health", lambda: None)
    args = Namespace(config=config_path, id="esp8266-01", port="/dev/ttyUSB3", target="esp8266",
                     mac="5c:cf:7f:11:22:33", power_hub=None, power_port=None, baud=115200,
                     flash_baud=460800, tag=None)
    assert admin_cli.command_boards_add(args) == 0
    assert "esp8266-01" in inventory.read_text()
    assert "fleet not republished" in capsys.readouterr().err


def test_boards_discover_reports_missing_and_the_wrong_connector(tmp_path, monkeypatch, capsys):
    from contextlib import nullcontext

    sys.path.insert(0, str(RUNNER))
    from alteriom_hil import admin_cli
    from alteriom_hil.inventory import DetectedDevice

    config_path, inventory = _registry_config(tmp_path)
    c5 = DetectedDevice("/dev/ttyUSB2", "esp32c5", "esp32-c5", "60:55:f9:12:34:56", transport="uart-bridge")
    monkeypatch.setattr("alteriom_hil.inventory.serial_ports", lambda: ["/dev/ttyUSB2"])
    monkeypatch.setattr("alteriom_hil.inventory.probe_port", lambda port: c5)
    monkeypatch.setattr(admin_cli, "rig_lock", nullcontext)
    assert admin_cli.command_boards_discover(Namespace(config=config_path, json=False)) == 0
    out = capsys.readouterr().out
    assert "connected: 0; missing: 1; unregistered: 1" in out
    assert "esp32-01             MISSING" in out
    assert "--mac 60:55:f9:12:34:56 --port /dev/ttyUSB2 --target esp32-c5" in out
    assert "UART bridge on /dev/ttyUSB2" in out


def test_discovery_from_the_command_line_registers_what_it_finds(tmp_path, monkeypatch, capsys):
    """Discovery from the command line and discovery from the portal are the
    same discovery. They were not: the service registered what it found and
    the CLI listed it as unregistered, so a rig where someone had run
    `boards discover` looked like a rig with four boards and none of them
    known (rig02, 2026-09-16)."""
    from contextlib import nullcontext

    sys.path.insert(0, str(RUNNER))
    from alteriom_hil import admin_cli
    from alteriom_hil.inventory import DetectedDevice, load_registry

    config_path, inventory = _registry_config(tmp_path)
    config = yaml.safe_load(config_path.read_text())
    config["inventory"] = {"auto_register": True}
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))

    c5 = DetectedDevice("/dev/ttyUSB2", "esp32c5", "esp32-c5", "60:55:f9:12:34:56", transport="uart-bridge")
    monkeypatch.setattr("alteriom_hil.inventory.serial_ports", lambda: ["/dev/ttyUSB2"])
    monkeypatch.setattr("alteriom_hil.inventory.probe_port", lambda port: c5)
    monkeypatch.setattr(admin_cli, "rig_lock", nullcontext)

    assert admin_cli.command_boards_discover(Namespace(config=config_path, json=False)) == 0
    out = capsys.readouterr().out
    assert "connected: 1; missing: 1; unregistered: 0" in out
    assert "esp32-c5-3456" in [board.id for board in load_registry(inventory)]
    assert "esp32-c5-3456" in out


# ---- providers --------------------------------------------------------------------------------

FAKE_LINK = "https://api.callmebot.com/whatsapp.php?phone=+10000000000&apikey=000000"


def _provider_config(tmp_path, providers=None):
    config = yaml.safe_load(EXAMPLE.read_text())
    token = tmp_path / "etc" / "api-token"
    token.parent.mkdir(parents=True)
    token.write_text("token")
    config["service"]["token_file"] = str(token)
    config["paths"]["state"] = str(tmp_path / "state")
    if providers is not None:
        config["providers"] = providers
    path = tmp_path / "etc" / "config.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    return path


class _Stdin:
    def __init__(self, text, tty=False):
        self._text, self._tty = text, tty

    def isatty(self):
        return self._tty

    def readline(self):
        return self._text


def _assert_no_secret(text):
    assert "000000" not in text and "10000000000" not in text, text


def test_providers_set_reads_the_link_from_stdin_and_stores_it_privately(tmp_path, monkeypatch, capsys):
    sys.path.insert(0, str(RUNNER))
    from alteriom_hil import admin_cli

    url_file = tmp_path / "etc" / "providers" / "callmebot-url"
    config_path = _provider_config(tmp_path, {"callmebot": {"url_file": str(url_file), "send": "never"}})
    monkeypatch.setattr(admin_cli, "require_root", lambda: None)
    applied = []
    monkeypatch.setattr(admin_cli, "apply", lambda payload, restart=True: applied.append(payload))
    monkeypatch.setattr(admin_cli.sys, "stdin", _Stdin(FAKE_LINK + "\n"))

    assert admin_cli.command_providers_set(Namespace(config=config_path, provider="callmebot")) == 0
    printed = capsys.readouterr()
    _assert_no_secret(printed.out + printed.err)
    assert "phone=***00&apikey=***" in printed.out
    assert url_file.read_text() == "https://api.callmebot.com/whatsapp.php?phone=%2B10000000000&apikey=000000\n"
    assert url_file.stat().st_mode & 0o777 == 0o640
    assert url_file.parent.stat().st_mode & 0o777 == 0o750
    assert applied == [], "the section was already configured: nothing else changes"

    # An invalid link is refused, and the refusal does not repeat it.
    monkeypatch.setattr(admin_cli.sys, "stdin", _Stdin(FAKE_LINK + "&text=hello\n"))
    with pytest.raises(admin_cli.hil_config.ConfigError) as caught:
        admin_cli.command_providers_set(Namespace(config=config_path, provider="callmebot"))
    _assert_no_secret(str(caught.value))
    assert "000000" in url_file.read_text(), "the stored link is untouched by a refused one"

    # At a terminal the link is asked for without echo.
    monkeypatch.setattr(admin_cli.sys, "stdin", _Stdin("", tty=True))
    monkeypatch.setattr("getpass.getpass", lambda prompt: FAKE_LINK.replace("000000", "111111"))
    assert admin_cli.command_providers_set(Namespace(config=config_path, provider="callmebot")) == 0
    assert "apikey=111111" in url_file.read_text()


def test_providers_set_adds_the_section_a_configuration_lacks(tmp_path, monkeypatch, capsys):
    sys.path.insert(0, str(RUNNER))
    from alteriom_hil import admin_cli

    config_path = _provider_config(tmp_path)
    url_file = tmp_path / "etc" / "providers" / "callmebot-url"
    monkeypatch.setattr(admin_cli.hil_config, "DEFAULT_CALLMEBOT",
                        {**admin_cli.hil_config.DEFAULT_CALLMEBOT, "url_file": str(url_file)})
    monkeypatch.setattr(admin_cli, "require_root", lambda: None)
    applied = []
    monkeypatch.setattr(admin_cli, "apply", lambda payload, restart=True: applied.append(restart))
    monkeypatch.setattr(admin_cli.sys, "stdin", _Stdin(FAKE_LINK))
    assert admin_cli.command_providers_set(Namespace(config=config_path, provider="callmebot")) == 0
    stored = yaml.safe_load(config_path.read_text())["providers"]["callmebot"]
    assert stored == {"url_file": str(url_file), "send": "release", "max_per_day": 5}
    assert applied == [False], "applied without restarting anything a run may be using"
    assert "restart the farm service when it is idle" in capsys.readouterr().out
    assert url_file.is_file()


def test_the_link_is_never_an_argument():
    sys.path.insert(0, str(RUNNER))
    from alteriom_hil import admin_cli

    with pytest.raises(SystemExit):
        admin_cli.parser().parse_args(["providers", "set", "callmebot", FAKE_LINK])


def test_providers_show_check_and_remove(tmp_path, monkeypatch, capsys):
    sys.path.insert(0, str(RUNNER))
    import os

    from alteriom_hil import admin_cli
    from alteriom_hil import providers

    url_file = tmp_path / "etc" / "providers" / "callmebot-url"
    config_path = _provider_config(tmp_path, {"callmebot": {"url_file": str(url_file), "max_per_day": 3}})

    assert admin_cli.command_providers_show(Namespace(config=config_path, json=True)) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["configured"] is True and report["link"] is None and "does not exist" in report["problem"]

    url_file.parent.mkdir(parents=True)
    url_file.write_text(FAKE_LINK + "\n")
    os.chmod(url_file, 0o640)
    providers.consume_budget(tmp_path / "state" / "callmebot-budget.json", 3)
    assert admin_cli.command_providers_show(Namespace(config=config_path, json=False)) == 0
    out = capsys.readouterr().out
    _assert_no_secret(out)
    assert "phone=***00&apikey=***" in out and "send:        release" in out
    assert "1 of 3 used today" in out

    routes = []
    monkeypatch.setattr(admin_cli.providers, "route_problem",
                        lambda timeout: routes.append(timeout) or None)
    assert admin_cli.command_providers_check(Namespace(config=config_path, provider="callmebot", timeout=2.0)) == 0
    out = capsys.readouterr().out
    _assert_no_secret(out)
    assert routes == [2.0] and "ok    link:" in out and "ok    route:" in out

    os.chmod(url_file, 0o644)
    monkeypatch.setattr(admin_cli.providers, "route_problem", lambda timeout: "api.callmebot.com does not resolve")
    assert admin_cli.command_providers_check(Namespace(config=config_path, provider="callmebot", timeout=2.0)) == 1
    out = capsys.readouterr().out
    _assert_no_secret(out)
    assert "FAIL  link:" in out and "every user" in out and "FAIL  route:" in out

    monkeypatch.setattr(admin_cli, "require_root", lambda: None)
    assert admin_cli.command_providers_remove(Namespace(config=config_path, provider="callmebot")) == 0
    assert not url_file.exists()
    assert admin_cli.command_providers_remove(Namespace(config=config_path, provider="callmebot")) == 0
    assert "nothing stored" in capsys.readouterr().out

    unconfigured = _provider_config(tmp_path / "bare")
    assert admin_cli.command_providers_show(Namespace(config=unconfigured, json=False)) == 0
    assert "not configured" in capsys.readouterr().out


class _Reply:
    def __init__(self, status, body):
        self.status, self._body, self.asked = status, body, []

    def read(self, limit=-1):
        self.asked.append(limit)
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _test_command(tmp_path, monkeypatch, max_per_day=2):
    sys.path.insert(0, str(RUNNER))
    import os

    from alteriom_hil import admin_cli

    url_file = tmp_path / "etc" / "providers" / "callmebot-url"
    config_path = _provider_config(tmp_path, {"callmebot": {"url_file": str(url_file), "send": "release",
                                                            "max_per_day": max_per_day}})
    url_file.parent.mkdir(parents=True, exist_ok=True)
    url_file.write_text(FAKE_LINK + "\n")
    os.chmod(url_file, 0o640)
    return admin_cli, config_path


def _run_test(admin_cli, config_path, capsys):
    try:
        code = admin_cli.main(["--config", str(config_path), "providers", "test", "callmebot"])
    finally:
        printed = capsys.readouterr()
    return code, printed.out + printed.err


def test_providers_test_sends_one_formatted_message_and_prints_one_redacted_line(tmp_path, monkeypatch, capsys):
    from urllib.parse import unquote

    admin_cli, config_path = _test_command(tmp_path, monkeypatch)
    sent = []
    reply = _Reply(200, b"Message to: +10000000000 Text to send: ... Message queued. You will receive it in a few seconds.")

    def fake(request, timeout=None):
        sent.append((request.full_url, request.get_method(), timeout))
        return reply

    monkeypatch.setattr(admin_cli, "urlopen", fake)
    code, out = _run_test(admin_cli, config_path, capsys)
    assert code == 0
    assert out == "test message queued by CallMeBot (HTTP 200)\n", "exactly one line"
    (url, method, timeout), = sent
    assert method == "GET" and timeout == admin_cli.TEST_TIMEOUT_SECONDS
    assert url.startswith("https://api.callmebot.com/whatsapp.php?phone=%2B10000000000&apikey=000000&text=")
    text = unquote(url.split("&text=", 1)[1])
    assert text.startswith("🔔 Alteriom ESP32 farm\nCallMeBot test from rig ")
    assert "Sent directly by the rig host (not through the mesh)\n" in text
    assert "Policy: send=release, budget 1/2 today\n" in text
    assert reply.asked == [admin_cli.TEST_REPLY_LIMIT], "at most 64 KB of the reply is read"
    from alteriom_hil import providers

    assert providers.budget_used(tmp_path / "state" / "callmebot-budget.json") == 1


@pytest.mark.parametrize(
    "answer, code, line",
    [
        ((203, b"<h1>Oops! Too many requests...</h1> for +10000000000"), 1, "CallMeBot refused: rate limited (HTTP 203)\n"),
        ((200, b"+10000000000 Your Account is Paused, send the word 'resume'"), 1,
         "CallMeBot refused: the account is paused (HTTP 200)\n"),
        ((208, b"Message queued"), 1, "CallMeBot refused: not delivered (HTTP 208)\n"),
        ((500, b"apikey 000000 broke"), 1, "CallMeBot did not queue it: an unrecognized reply (HTTP 500)\n"),
    ],
)
def test_providers_test_reports_a_refusal_in_one_line(tmp_path, monkeypatch, capsys, answer, code, line):
    import io
    from urllib.error import HTTPError

    admin_cli, config_path = _test_command(tmp_path, monkeypatch)
    status, body = answer

    def fake(request, timeout=None):
        if status >= 300:
            raise HTTPError(request.full_url, status, "Error", {}, io.BytesIO(body))
        return _Reply(status, body)

    monkeypatch.setattr(admin_cli, "urlopen", fake)
    assert _run_test(admin_cli, config_path, capsys) == (code, line)


def test_providers_test_that_cannot_reach_the_service_scrubs_the_reason(tmp_path, monkeypatch, capsys):
    from urllib.error import URLError

    admin_cli, config_path = _test_command(tmp_path, monkeypatch)

    def unreachable(request, timeout=None):
        raise URLError(OSError(f"Name or service not known for {request.full_url}"))

    monkeypatch.setattr(admin_cli, "urlopen", unreachable)
    code, out = _run_test(admin_cli, config_path, capsys)
    assert code == 1 and out.startswith("could not reach api.callmebot.com: ") and out.count("\n") == 1
    _assert_no_secret(out)

    def slow(request, timeout=None):
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(admin_cli, "urlopen", slow)
    code, out = _run_test(admin_cli, config_path, capsys)
    assert code == 1 and out.startswith("no reply from api.callmebot.com within 20s") and "may still arrive" in out


def test_providers_test_refuses_when_todays_budget_is_spent_or_no_link_is_stored(tmp_path, monkeypatch, capsys):
    from alteriom_hil import providers

    admin_cli, config_path = _test_command(tmp_path, monkeypatch, max_per_day=1)
    providers.consume_budget(tmp_path / "state" / "callmebot-budget.json", 1)
    monkeypatch.setattr(admin_cli, "urlopen", lambda *a, **k: pytest.fail("nothing may be sent"))
    code, out = _run_test(admin_cli, config_path, capsys)
    assert code != 0 and out.startswith("error: today's CallMeBot budget on this rig is used (1 of 1)")

    (tmp_path / "etc" / "providers" / "callmebot-url").unlink()
    code, out = _run_test(admin_cli, config_path, capsys)
    assert code != 0 and out.startswith("error: ") and "does not exist" in out
    _assert_no_secret(out)


def test_node_control_relays_every_line_providers_test_can_end_with():
    """rig/node-control.sh keeps only a line starting with one of these."""
    import re

    sys.path.insert(0, str(RUNNER))
    from alteriom_hil import admin_cli

    script = (RIG / "node-control.sh").read_text()
    pattern = script.split("provider_test)", 1)[1].split("grep -E '", 1)[1].split("'", 1)[0]
    for prefix in admin_cli.TEST_RESULT_PREFIXES:
        assert re.match(pattern, prefix + "x"), prefix

def test_notify_set_writes_the_setting_down_and_tells_the_service(tmp_path, monkeypatch):
    """`notify set` printed "send one with: notify test", and that very test
    reloaded the file and found the old channel: the payload was applied and
    never saved. The service that sends builds its notifier once at startup,
    so it is restarted too -- when nothing is running."""
    sys.path.insert(0, str(RUNNER))
    from alteriom_hil import admin_cli

    config_path, _ = _registry_config(tmp_path)
    monkeypatch.setattr(admin_cli, "require_root", lambda: None)
    monkeypatch.setattr(admin_cli, "read_secret_line", lambda prompt: "123456:AA-token")
    written = {}
    monkeypatch.setattr(admin_cli, "write_secret_file",
                        lambda payload, path, text: written.__setitem__(str(path), text))
    applied = []
    monkeypatch.setattr(admin_cli, "apply", lambda payload, restart=True: applied.append(payload))
    restarted = []
    monkeypatch.setattr(admin_cli, "restart_sending_service", lambda: restarted.append(True) or True)

    args = Namespace(config=config_path, channel="telegram", chat_id="-1001234567890",
                     format=None, id=None)
    assert admin_cli.command_notify_set(args) == 0

    # Setting a channel adds one. A host that had a webhook and gains a
    # Telegram bot has both: this used to drop whatever was there, which is
    # not what "set up Telegram as well" ever meant.
    saved = hil_config_module().notify_channels(yaml.safe_load(config_path.read_text()))
    telegram = [item for item in saved if item.get("channel") == "telegram"]
    assert len(telegram) == 1
    assert telegram[0]["chat_id"] == "-1001234567890"
    assert telegram[0]["enabled"] is True
    assert telegram[0]["token_file"].endswith("providers/telegram-token")
    assert len(saved) == 2 and {item["id"] for item in saved} == {"1", "2"}
    # The token is in its own file, never in the configuration.
    assert "123456:AA-token" not in config_path.read_text()
    assert written and applied and restarted

    # Named, it replaces that one rather than adding a third.
    again = Namespace(config=config_path, channel="telegram", chat_id="-1009999999999",
                      format=None, id=telegram[0]["id"])
    assert admin_cli.command_notify_set(again) == 0
    saved = hil_config_module().notify_channels(yaml.safe_load(config_path.read_text()))
    assert len(saved) == 2
    assert [item for item in saved if item.get("channel") == "telegram"][0]["chat_id"] == "-1009999999999"

    # And removed, one at a time.
    assert admin_cli.command_notify_remove(Namespace(config=config_path, id=telegram[0]["id"])) == 0
    saved = hil_config_module().notify_channels(yaml.safe_load(config_path.read_text()))
    assert [item.get("channel", "webhook") for item in saved] == ["webhook"]


def hil_config_module():
    from alteriom_hil import admin_cli

    return admin_cli.hil_config


def test_a_telegram_only_configuration_satisfies_the_published_schema(tmp_path):
    """The schema asked every notify section for a webhook URL, so a config
    the documented Telegram command writes was accepted by the CLI and
    rejected by the schema other tooling validates with."""
    import jsonschema

    schema = json.loads((RIG / "hil-config.schema.json").read_text())
    payload = yaml.safe_load((RIG / "hil-config.example.yaml").read_text())
    payload["notify"] = {
        "enabled": True, "channel": "telegram",
        "token_file": "/etc/alteriom-hil/providers/telegram-token", "chat_id": "42",
    }
    jsonschema.validate(payload, schema)

    payload["notify"] = {"enabled": True, "webhook_url_file": "/etc/alteriom-hil/notify-webhook"}
    jsonschema.validate(payload, schema)

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**payload, "notify": {"enabled": True, "channel": "telegram"}}, schema)


def test_a_callmebot_channel_is_tested_through_the_link_it_notifies_with(tmp_path, monkeypatch):
    """`notify test` read `notify.webhook_url_file` for every channel that was
    not Telegram, so the CallMeBot channel -- whose whole point is that it has
    no second credential -- was refused as an unconfigured webhook by the one
    button meant to prove it works."""
    sys.path.insert(0, str(RUNNER))
    from alteriom_hil import admin_cli

    config_path, _ = _registry_config(tmp_path)
    link = tmp_path / "callmebot-url"
    link.write_text("https://api.callmebot.com/whatsapp.php?phone=+15550009876&apikey=246813\n",
                    encoding="utf-8")
    config = yaml.safe_load(config_path.read_text())
    config["providers"] = {"callmebot": {"url_file": str(link), "send": "release", "max_per_day": 5}}
    config["notify"] = {"enabled": True, "channel": "callmebot"}
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))

    sent = {}

    class Recorder:
        def __init__(self, secret_file, **kwargs):
            sent["secret_file"] = str(secret_file)
            sent["channel"] = kwargs.get("channel")

        def link(self, fragment):
            return None

        def where(self):
            return "api.callmebot.com"

        def send(self, notification):
            sent["notification"] = notification
            return {"ok": True, "status": 200}

    import alteriom_hil.notify as notify_module
    monkeypatch.setattr(notify_module, "Notifier", Recorder)

    assert admin_cli.command_notify_test(Namespace(config=config_path)) == 0
    assert sent["channel"] == "callmebot"
    assert sent["secret_file"] == str(link), "the link it validates with, not a webhook it does not have"

    # And a channel with nothing stored says which channel, rather than
    # claiming the configuration has no notify section at all.
    config["providers"] = {}
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    try:
        admin_cli.command_notify_test(Namespace(config=config_path))
    except admin_cli.hil_config.ConfigError as exc:
        assert "callmebot" in str(exc)
    else:
        raise AssertionError("a channel with no credential is not testable")
