"""What keeps an unattended farm a farm: it says when it breaks, it can be
put back after the SD card dies, and its disk does not fill up forever.

docs/product-gaps.md named all three as the difference between "a rig that
works while someone watches it" and "a service that survives a week of nobody
watching it". Each is tested here against the failure it exists for: a
notification that should have gone and did not, a backup that would not
restore, retention that deleted what a run found.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tarfile
import threading
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from alteriom_hil import backup
from alteriom_hil.notify import EVENTS, Notification, Notifier, check_url

REPO = Path(__file__).resolve().parents[1]
RUNNER = REPO / "runner"
sys.path.insert(0, str(RUNNER))

import hil_config  # noqa: E402


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---- notifications ---------------------------------------------------------------


class Hook:
    """A webhook that records what it was sent and answers `status`."""

    def __init__(self, status=200):
        received, answer = [], status

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(answer)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.received = received
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/hook"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_a_notification_reaches_the_webhook_in_the_shape_it_takes(tmp_path):
    hook = Hook()
    url_file = tmp_path / "notify-webhook"
    url_file.write_text(hook.url + "\n")
    try:
        note = Notification("queue_paused", "The farm paused its own queue", "the canary failed on every board",
                            link="https://hil.example.com/app#overview")
        slack = Notifier(url_file, "slack").send(note)
        assert slack["ok"] and slack["status"] == 200
        assert hook.received[-1]["text"].startswith("🔴 The farm paused its own queue\nthe canary failed")
        assert "<https://hil.example.com/app#overview|Open in the farm dashboard>" in hook.received[-1]["text"]
        Notifier(url_file, "discord").send(note)
        assert set(hook.received[-1]) == {"content"}
        Notifier(url_file, "json").send(note)
        assert hook.received[-1]["event"] == "queue_paused" and hook.received[-1]["link"].endswith("#overview")
        # An event the host did not ask for is not sent; a test always is.
        only = Notifier(url_file, events=["host_unhealthy"])
        assert only.send(note) is None
        assert only.send(Notification("test", "hello"))["ok"]
        assert len(hook.received) == 4
    finally:
        hook.close()


def test_a_delivery_that_fails_says_why_and_never_raises(tmp_path):
    url_file = tmp_path / "notify-webhook"
    note = Notification("board_red", "Canary red on esp32-01")
    # No file, a plain-http URL off loopback, a webhook that refuses, one
    # that is not there: each is an outcome, and the work carries on.
    assert Notifier(url_file).send(note)["error"], "no file is an outcome, not an exception"
    url_file.write_text("http://hooks.example.com/abc")
    assert Notifier(url_file).send(note)["error"] == "the webhook URL must be https (http only to loopback)"
    refusing = Hook(status=500)
    try:
        url_file.write_text(refusing.url)
        outcome = Notifier(url_file).send(note)
        assert outcome["ok"] is False and outcome["status"] == 500 and "500" in outcome["error"]
    finally:
        refusing.close()
    url_file.write_text("http://127.0.0.1:9/nothing-listens")
    notifier = Notifier(url_file)
    assert notifier.send(note)["ok"] is False and notifier.last["error"]
    with pytest.raises(ValueError):
        check_url("ftp://example.com/x")
    # The URL is a credential: it is never part of what is reported back.
    url_file.write_text("https://hooks.slack.com/services/T000/B000/SECRET")
    failing = Notifier(url_file, post=lambda url, body: (_ for _ in ()).throw(OSError("refused")))
    assert "SECRET" not in json.dumps(failing.send(note))


def test_the_runtime_environment_turns_notifications_on_and_off():
    assert Notifier.from_env({}) is None
    notifier = Notifier.from_env({
        "ALTERIOM_HIL_NOTIFY_WEBHOOK_FILE": "/etc/alteriom-hil/notify-webhook",
        "ALTERIOM_HIL_NOTIFY_FORMAT": "discord",
        "ALTERIOM_HIL_NOTIFY_EVENTS": "board_red,host_unhealthy",
        "ALTERIOM_HIL_PUBLIC_HOST": "hil.example.com",
    })
    assert notifier.format == "discord" and notifier.events == {"board_red", "host_unhealthy"}
    assert notifier.link("#run/abc") == "https://hil.example.com/app#run/abc"

    config = yaml.safe_load((RUNNER / "hil-config.example.yaml").read_text())
    config["notify"]["enabled"] = True
    env = dict(line.split("=", 1) for line in hil_config.runtime_env(config).splitlines())
    assert env["ALTERIOM_HIL_NOTIFY_WEBHOOK_FILE"] == "/etc/alteriom-hil/notify-webhook"
    assert env["ALTERIOM_HIL_NOTIFY_EVENTS"] == ",".join(EVENTS)
    config["notify"]["enabled"] = False
    assert "NOTIFY" not in hil_config.runtime_env(config)


class Recorder:
    def __init__(self):
        self.sent = []

    def send_later(self, note):
        self.sent.append(note)

    def send(self, note):
        self.sent.append(note)
        return {"ok": True, "event": note.event}

    def link(self, fragment):
        return f"https://hil.example.com/app{fragment}"


def test_the_farm_pausing_itself_is_sent_once_and_an_operator_pausing_it_is_not(tmp_path):
    farm_service = _module("farm_service_ops", RUNNER / "farm_service.py")
    manager = farm_service.FarmManager.__new__(farm_service.FarmManager)
    import queue

    manager.__dict__.update(paused=False, paused_since=None, paused_reason=None, notifiers=[Recorder()],
                            store=farm_service.JobStore(tmp_path / "farm.sqlite3"), pending=queue.Queue())
    manager.pause()
    manager.resume()
    assert manager.notifiers[0].sent == [], "an operator knows they paused it"
    manager.pause("the canary failed on every board: wifi_join (run 01234567)")
    manager.pause("again, while already paused")
    assert [note.event for note in manager.notifiers[0].sent] == ["queue_paused"]
    assert "wifi_join" in manager.notifiers[0].sent[0].detail
    assert manager.notifiers[0].sent[0].link == "https://hil.example.com/app#overview"


def test_boards_red_on_their_own_are_sent_and_farm_wide_red_is_left_to_the_pause(tmp_path):
    farm_service = _module("farm_service_ops_red", RUNNER / "farm_service.py")
    manager = farm_service.FarmManager.__new__(farm_service.FarmManager)
    manager.state = tmp_path
    manager.notifiers = [Recorder()]
    (tmp_path / "board-health.json").write_text(json.dumps({
        "esp32-01": {"verdict": "failed", "failed": ["mqtt_publish", "wifi_join"], "farm_wide": ["mqtt_publish"]},
        "esp32-02": {"verdict": "failed", "failed": ["mqtt_publish"], "farm_wide": ["mqtt_publish"]},
        "esp32-03": {"verdict": "passed", "failed": [], "farm_wide": []},
    }))
    manager._notify_boards_red("a" * 32, {"boards": {"esp32-01": "failed", "esp32-02": "failed", "esp32-03": "passed"}})
    [note] = manager.notifiers[0].sent
    assert note.event == "board_red" and note.title == "Rig Health Check red on 1 board: esp32-01"
    assert note.detail == f"esp32-01: wifi_join (run {'a' * 8})" and note.link.endswith(f"#run/{'a' * 32}")
    # Only what failed everywhere: nothing of a board's own to report.
    manager.notifiers[0].sent.clear()
    manager._notify_boards_red("b" * 32, {"boards": {"esp32-02": "failed"}})
    assert manager.notifiers[0].sent == []


def test_the_host_going_unhealthy_and_recovering_is_sent_on_the_transition_only():
    health_check = _module("health_check_ops", RUNNER / "health_check.py")
    notifier = Recorder()
    broken = {"status": "unhealthy", "hostname": "esp32-hil", "checks": [
        {"name": "disk", "status": "unhealthy", "message": "97% used"},
        {"name": "tools", "status": "ok", "message": "fine"},
    ]}
    assert health_check.notify_transition({"status": "ok"}, broken, notifier)["ok"]
    assert notifier.sent[-1].title == "The farm host esp32-hil is unhealthy"
    assert notifier.sent[-1].detail == "disk: 97% used"
    assert health_check.notify_transition(broken, broken, notifier) is None, "still broken is not news"
    assert health_check.notify_transition(broken, {"status": "degraded", "hostname": "esp32-hil"}, notifier)["ok"]
    assert notifier.sent[-1].tone == "good" and "recovered" in notifier.sent[-1].title
    assert health_check.notify_transition(None, {"status": "ok"}, notifier) is None
    assert health_check.notify_transition({"status": "ok"}, broken, None) is None


def test_the_host_is_not_required_to_have_platformio(tmp_path):
    """The farm builds nothing; a correctly provisioned host was marked
    unhealthy for lacking the toolchain it no longer installs."""
    health_check = _module("health_check_tools", RUNNER / "health_check.py")
    source = (RUNNER / "health_check.py").read_text(encoding="utf-8")
    tools = source.split('"tools",', 1)[0].rsplit("missing = [", 1)[1]
    assert '"pio"' not in tools and '"esptool", "esptool.py"' in tools
    assert health_check.SEVERITY["unhealthy"] > health_check.SEVERITY["degraded"]


# ---- backups -----------------------------------------------------------------------


def _farm_state(tmp_path):
    state, etc = tmp_path / "state", tmp_path / "etc"
    (state / "artifacts" / ("c" * 32) / "esp32").mkdir(parents=True)
    (state / "artifacts" / ("c" * 32) / "esp32" / "flash-image.bin").write_bytes(b"\xe9canary")
    (state / "artifacts" / ("c" * 32) / "manifest.json").write_text("{}")
    (state / "artifacts" / ("d" * 32)).mkdir()
    (state / "artifacts" / ("d" * 32) / "manifest.json").write_text("{}")
    db = sqlite3.connect(state / "farm.sqlite3")
    db.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, status TEXT)")
    db.execute("CREATE TABLE artifact_records (id TEXT PRIMARY KEY, pinned_at TEXT, removed_at TEXT)")
    db.execute("INSERT INTO jobs VALUES ('job-1', 'passed')")
    db.execute("INSERT INTO artifact_records VALUES (?, '2026-09-13', NULL)", ("c" * 32,))
    db.commit()
    db.close()
    (state / "inventory.yaml").write_text("boards: []\n")
    (state / "board-health.json").write_text("{}")
    etc.mkdir()
    (etc / "config.yaml").write_text("schema: 2\n")
    for secret in backup.SECRETS:
        (etc / secret).parent.mkdir(parents=True, exist_ok=True)
        (etc / secret).write_text("do-not-copy")
    return state, etc


def test_a_backup_holds_the_farm_and_no_secret_and_restores(tmp_path):
    state, etc = _farm_state(tmp_path)
    pushed = []
    outcome = backup.create_backup(state, etc, tmp_path / "backups", keep=2, target="sparck@alteriom03:/srv/b",
                                   push=lambda archive, target: pushed.append((archive, target)) or {"target": target, "ok": True, "error": None})
    archive = Path(outcome["archive"])
    assert pushed == [(archive, "sparck@alteriom03:/srv/b")]
    assert outcome["pinned_bundles"] == 1 and backup.last_backup(tmp_path / "backups")["archive"] == str(archive)
    with tarfile.open(archive) as tar:
        names = set(tar.getnames())
        everything = b"".join(tar.extractfile(member).read() for member in tar.getmembers())
    assert {"backup.json", "state/farm.sqlite3", "state/inventory.yaml", "state/board-health.json", "etc/config.yaml",
            f"state/artifacts/{'c' * 32}/esp32/flash-image.bin"} <= names
    assert not any("d" * 32 in name for name in names), "an unpinned bundle is not the farm's to keep"
    assert b"do-not-copy" not in everything, "never a secret"
    manifest, _ = backup.read_backup(archive)
    assert set(manifest["excluded_secrets"]) == {str(etc / name) for name in backup.SECRETS}

    # Restore into a new host: first the plan, then the writes.
    fresh_state, fresh_etc = tmp_path / "new-state", tmp_path / "new-etc"
    preview = backup.restore_backup(archive, fresh_state, fresh_etc)
    assert not fresh_state.exists(), "a preview writes nothing"
    assert {step["action"] for step in preview["plan"]} == {"write"}
    backup.restore_backup(archive, fresh_state, fresh_etc, apply=True)
    rows = sqlite3.connect(fresh_state / "farm.sqlite3").execute("SELECT id, status FROM jobs").fetchall()
    assert rows == [("job-1", "passed")]
    assert (fresh_etc / "config.yaml").read_text() == "schema: 2\n"
    assert (fresh_state / "artifacts" / ("c" * 32) / "esp32" / "flash-image.bin").read_bytes() == b"\xe9canary"

    # Restored over a live farm: the database is set aside, never lost, and a
    # bundle already there is kept as it is.
    (state / "artifacts" / ("c" * 32) / "esp32" / "flash-image.bin").write_bytes(b"\xe9newer")
    result = backup.restore_backup(archive, state, etc, apply=True)
    assert list(state.glob("farm.sqlite3.before-restore-*"))
    assert (state / "artifacts" / ("c" * 32) / "esp32" / "flash-image.bin").read_bytes() == b"\xe9newer"
    assert any(step["action"] == "keep existing" for step in result["plan"])


def test_a_damaged_backup_is_refused_before_anything_is_written(tmp_path):
    state, etc = _farm_state(tmp_path)
    archive = Path(backup.create_backup(state, etc, tmp_path / "backups")["archive"])
    damaged = tmp_path / "damaged.tar.gz"
    with tarfile.open(archive) as source, tarfile.open(damaged, "w:gz") as target:
        for member in source.getmembers():
            data = source.extractfile(member).read()
            if member.name == "etc/config.yaml":
                data = b"schema: 99\n"
                member.size = len(data)
            import io

            target.addfile(member, io.BytesIO(data))
    with pytest.raises(ValueError, match="checksum mismatch"):
        backup.restore_backup(damaged, tmp_path / "x", tmp_path / "y", apply=True)
    assert not (tmp_path / "x").exists()


def test_old_backups_go_and_a_stale_or_uncopied_one_is_reported(tmp_path):
    state, etc = _farm_state(tmp_path)
    directory = tmp_path / "backups"
    for stamp in ("20260901T000000Z", "20260902T000000Z", "20260903T000000Z"):
        (directory / f"{backup.PREFIX}{stamp}.tar.gz").parent.mkdir(exist_ok=True)
        (directory / f"{backup.PREFIX}{stamp}.tar.gz").write_bytes(b"old")
    outcome = backup.create_backup(state, etc, directory, keep=2)
    assert len(backup.backups(directory)) == 2 and len(outcome["removed"]) == 2
    assert backup.staleness(directory) is None
    backup.record(directory, {**outcome, "created_at": (datetime.now(timezone.utc) - timedelta(hours=50)).isoformat()})
    assert backup.staleness(directory) == "the last backup is 50 h old"
    backup.record(directory, {**outcome, "pushed": {"target": "x@y:/z", "ok": False, "error": "Permission denied"}})
    assert "not copied to x@y:/z: Permission denied" in backup.staleness(directory)
    assert backup.staleness(tmp_path / "nowhere") == "no backup has been made"


# ---- retention ---------------------------------------------------------------------------


def test_retention_removes_what_is_bulky_and_old_and_keeps_what_a_run_found(tmp_path):
    farm_service = _module("farm_service_retention", RUNNER / "farm_service.py")
    manager = farm_service.FarmManager.__new__(farm_service.FarmManager)
    manager.state = tmp_path
    (tmp_path / "logs").mkdir()
    manager.store = farm_service.JobStore(tmp_path / "farm.sqlite3")
    now = datetime.now(timezone.utc)

    def run(days_ago, status="passed"):
        job = manager.store.create("suite", {"profile": "canary"}, tmp_path / "x.log")
        if status != "queued":
            manager.store.update(job["id"], status, {})
        with manager.store.connect() as db:
            db.execute("UPDATE jobs SET created_at=? WHERE id=?", ((now - timedelta(days=days_ago)).isoformat(), job["id"]))
        serial = tmp_path / "runs" / job["id"] / "serial"
        (serial / "mqtt").mkdir(parents=True)
        (serial / "esp32-01.serial.log").write_text("boot\n" * 1000)
        (serial / "mqtt" / "queue.jsonl").write_text("{}\n")
        (serial / "board-health.json").write_text("{}")
        (tmp_path / "runs" / job["id"] / "metrics").mkdir()
        (tmp_path / "runs" / job["id"] / "metrics" / "report.json").write_text("{}")
        (tmp_path / "runs" / job["id"] / "results.xml").write_text("<testsuites/>")
        (tmp_path / "logs" / f"{job['id']}.log").write_text("log\n")
        return job["id"]

    ancient, old, newest = run(400), run(200), run(1)
    waiting = run(300, status="queued")
    stale = tmp_path / "workspaces" / "leftover"
    stale.mkdir(parents=True)
    os.utime(stale, (now.timestamp() - 5 * 86400, now.timestamp() - 5 * 86400))
    settings = {"enabled": True, "run_evidence_days": 90, "log_days": 300, "keep_newest_runs": 1, "workspace_days": 2}
    manager.retention_settings = lambda: settings

    plan = manager.retention_sweep(dry_run=True)
    assert {item["id"] for item in plan["runs"]} == {ancient, old}, "never the newest, never a queued run"
    assert [item["id"] for item in plan["logs"]] == [ancient], "logs have their own age"
    assert [item["name"] for item in plan["workspaces"]] == ["leftover"]
    assert (tmp_path / "runs" / ancient / "serial" / "esp32-01.serial.log").exists(), "a preview deletes nothing"

    done = manager.retention_sweep(dry_run=False)
    assert done["last"]["removed"] == {"runs": 2, "logs": 1, "workspaces": 1}
    for job_id in (ancient, old):
        run_dir = tmp_path / "runs" / job_id
        assert not (run_dir / "serial" / "esp32-01.serial.log").exists() and not (run_dir / "serial" / "mqtt").exists()
        # What the run found stays.
        assert (run_dir / "serial" / "board-health.json").exists()
        assert (run_dir / "metrics" / "report.json").exists() and (run_dir / "results.xml").exists()
    assert not (tmp_path / "logs" / f"{ancient}.log").exists() and (tmp_path / "logs" / f"{old}.log").exists()
    assert (tmp_path / "runs" / newest / "serial" / "esp32-01.serial.log").exists()
    assert (tmp_path / "runs" / waiting / "serial" / "esp32-01.serial.log").exists()
    assert not stale.exists()
    # The job rows stay, and a run's page says what went.
    assert manager.store.get(ancient)["status"] == "passed"
    removed = manager.store.evidence_removals(ancient)
    assert set(removed) == {"serial", "log"} and removed["serial"]["removed_at"]
    # Nothing is removed twice.
    again = manager.retention_sweep(dry_run=True)
    assert again["runs"] == [] and again["logs"] == [] and again["workspaces"] == []


# ---- configuration ----------------------------------------------------------------------------


def test_the_new_settings_are_validated_like_the_rest():
    config = yaml.safe_load((RUNNER / "hil-config.example.yaml").read_text())
    assert hil_config.validate_config(config) == []
    cases = [
        (("notify", "format"), "teams", "notify.format must be one of"),
        (("notify", "events"), ["everything"], "notify.events must be a non-empty list"),
        (("notify", "webhook_url_file"), "relative/path", "notify.webhook_url_file must be an absolute path"),
        (("backup", "keep"), 0, "backup.keep must be an integer from 1 to 365"),
        (("backup", "target"), "not a target; rm -rf /", "backup.target must be user@host:/absolute/path"),
        (("retention", "log_days"), 0, "retention.log_days must be an integer from 1 to 3650"),
        (("retention", "enabled"), "yes", "retention.enabled must be a boolean"),
    ]
    for (section, key), value, message in cases:
        broken = json.loads(json.dumps(config))
        broken[section][key] = value
        assert any(message in error for error in hil_config.validate_config(broken)), (section, key)
    # A host configured before these existed gets the safe defaults: backups
    # on, retention off.
    minimal = {key: value for key, value in config.items() if key not in ("notify", "backup", "retention")}
    assert hil_config.validate_config(minimal) == []


def test_the_nightly_backup_is_installed_as_the_runner_user():
    installer = (RUNNER / "install-health-service.sh").read_text(encoding="utf-8")
    unit = installer.split("alteriom-hil-backup.service >/dev/null <<EOF", 1)[1].split("EOF", 1)[0]
    assert "User=$RUN_USER" in unit and "admin_cli.py backup create" in unit
    assert "OnCalendar=*-*-* 03:30:00" in installer and "systemctl enable --now alteriom-hil-backup.timer" in installer


def test_telegram_is_a_channel_like_any_other(tmp_path):
    """A bot token and a chat, and the farm says there what it says anywhere.
    The declaration is what lets a page offer a channel and a configuration
    validate one without either knowing what Telegram is."""
    from alteriom_hil.notify import CHANNELS, Notification, Notifier

    spec = CHANNELS["telegram"]
    assert spec.secret_setting == "token_file" and spec.settings == ("chat_id",)
    assert spec.notifies and "BotFather" in spec.how

    token = tmp_path / "telegram-token"
    token.write_text("123456:AA-the-bot-token\n", encoding="utf-8")
    sent = []
    notifier = Notifier(
        token, channel="telegram", chat_id="-1001234567890",
        post=lambda url, body: sent.append((url, json.loads(body))) or 200,
    )
    outcome = notifier.send(Notification(
        "board_red", "Rig Health Check red on 1 board: esp32-01",
        "esp32-01: the radio would not join", link="https://espfarm.example/#rig/rig02", tone="bad",
    ))
    assert outcome["ok"] and outcome["channel"] == "telegram"
    url, body = sent[0]
    assert url == "https://api.telegram.org/bot123456:AA-the-bot-token/sendMessage"
    assert body["chat_id"] == "-1001234567890"
    assert body["text"].startswith("\U0001f534 Rig Health Check red on 1 board: esp32-01")
    assert "the radio would not join" in body["text"]
    assert "https://espfarm.example/#rig/rig02" in body["text"]
    # Plain text, deliberately: a board id carries underscores, and Markdown
    # would either mangle the message or refuse to send it.
    assert "parse_mode" not in body


def test_the_bot_token_is_never_in_what_a_failure_says(tmp_path):
    """Telegram puts the token in the path, so a refusal, a timeout and a
    redirect all name it -- and those messages are kept for the configuration
    page and sent on to the portal."""
    from alteriom_hil.notify import Notification, Notifier

    token = tmp_path / "telegram-token"
    token.write_text("123456:AA-the-bot-token\n", encoding="utf-8")

    def refuse(url, body):
        raise OSError(f"failed to reach {url}: connection refused")

    notifier = Notifier(token, channel="telegram", chat_id="42", post=refuse)
    outcome = notifier.send(Notification("test", "A test from the farm"))
    assert not outcome["ok"]
    assert "123456:AA-the-bot-token" not in outcome["error"]
    assert "***" in outcome["error"]


def test_a_telegram_channel_needs_somewhere_to_send(tmp_path):
    from alteriom_hil.notify import Notifier

    with pytest.raises(ValueError, match="chat id"):
        Notifier(tmp_path / "token", channel="telegram")
    with pytest.raises(ValueError, match="channel must be one of"):
        Notifier(tmp_path / "token", channel="carrier-pigeon")


def test_the_environment_says_which_channel_the_farm_has(tmp_path):
    """Both halves of the farm -- the service and the health timer -- build
    their notifier from the runtime environment, so the channel travels there
    like everything else the rig decides."""
    from alteriom_hil.notify import Notifier

    telegram = Notifier.from_env({
        "ALTERIOM_HIL_NOTIFY_CHANNEL": "telegram",
        "ALTERIOM_HIL_NOTIFY_TOKEN_FILE": str(tmp_path / "token"),
        "ALTERIOM_HIL_NOTIFY_CHAT_ID": "42",
    })
    assert telegram is not None and telegram.channel == "telegram" and telegram.chat_id == "42"

    webhook = Notifier.from_env({"ALTERIOM_HIL_NOTIFY_WEBHOOK_FILE": str(tmp_path / "hook")})
    assert webhook is not None and webhook.channel == "webhook"

    # A Telegram channel with no token file is no notifier at all, rather than
    # one that fails on its first message.
    assert Notifier.from_env({"ALTERIOM_HIL_NOTIFY_CHANNEL": "telegram"}) is None


def test_callmebot_carries_a_notification_with_the_request_a_board_uses(tmp_path):
    """A provider proves a board can reach a real service; a notifier tells a
    person something happened. One shape -- a credential in a file, a message,
    a budget -- so the link a rig already validates with can carry a message
    to its owner, and nothing new has to be stored or rotated."""
    from alteriom_hil.notify import CHANNELS, Notification, Notifier

    spec = CHANNELS["callmebot"]
    assert spec.notifies and spec.validates, "it does both, which is the point"
    assert spec.secret_setting == "url_file" and spec.settings == ()

    link = tmp_path / "callmebot-url"
    link.write_text("https://api.callmebot.com/whatsapp.php?phone=+15550009876&apikey=246813\n",
                    encoding="utf-8")
    fetched = []
    notifier = Notifier(link, channel="callmebot", get=lambda url: fetched.append(url) or 200)
    outcome = notifier.send(Notification(
        "board_red", "Rig Health Check red on 1 board: esp32-01",
        "esp32-01: the radio would not join", tone="bad",
    ))
    assert outcome["ok"] and outcome["channel"] == "callmebot"
    sent = fetched[0]
    assert sent.startswith("https://api.callmebot.com/whatsapp.php?")
    assert "phone=%2B15550009876" in sent and "apikey=246813" in sent
    # The whole message in `text=`, percent-encoded: a newline is %0A.
    assert "text=" in sent and "%0A" in sent
    assert "esp32-01" in sent.replace("%2D", "-")


def test_a_failed_callmebot_notification_names_neither_the_key_nor_the_number(tmp_path):
    """The URL is the message, so every failure quotes it -- and those
    messages are kept for the configuration page and sent to a portal."""
    from alteriom_hil.notify import Notification, Notifier

    link = tmp_path / "callmebot-url"
    link.write_text("https://api.callmebot.com/whatsapp.php?phone=+15550009876&apikey=246813\n",
                    encoding="utf-8")

    def refuse(url):
        raise OSError(f"failed to reach {url}: connection refused")

    notifier = Notifier(link, channel="callmebot", get=refuse)
    outcome = notifier.send(Notification("test", "A test from the farm"))
    assert not outcome["ok"]
    assert "246813" not in outcome["error"] and "15550009876" not in outcome["error"]
    assert "***" in outcome["error"]


def test_telegram_is_declared_as_something_a_board_can_send_through():
    """Telegram is an extension of the CallMeBot idea, not a second one: an
    https GET with the text in the query, which is what sendToInternet() does.
    The declaration is what a suite will read to know that."""
    from alteriom_hil.notify import CHANNELS

    assert CHANNELS["telegram"].validates and CHANNELS["telegram"].notifies
    assert not CHANNELS["webhook"].validates, "a webhook is the farm's voice, not a board's"
