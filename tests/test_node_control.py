"""runner/node-control.sh: what a portal asks a node for that needs sudo.

The script runs as the runner user through alteriom-hil-control.service; here
sudo, systemctl, journalctl and the admin CLI are stubs that record what they
were asked, and the outcome file is what the node agent reports.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / "runner" / "node-control.sh"
ID = "c" * 32

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="runs node-control.sh with POSIX stubs")


def _run(tmp_path: Path, action: str, args: dict | None = None, admin_exit: int = 0):
    update = tmp_path / "update"
    update.mkdir(exist_ok=True)
    (update / "control.json").write_text(json.dumps({"id": ID, "action": action, "args": args or {}}))
    stubs = tmp_path / "bin"
    stubs.mkdir(exist_ok=True)
    calls = tmp_path / "calls"
    # sudo runs what it is given, as this user, through the stubs below.
    (stubs / "sudo").write_text('#!/bin/sh\nexec "$@"\n')
    (stubs / "systemctl").write_text(f'#!/bin/sh\necho "systemctl $*" >> {calls}\n')
    (stubs / "journalctl").write_text(f'#!/bin/sh\necho "journalctl $*" >> {calls}\necho "Sep 14 farm-api: GET /healthz"\n')
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    (venv / "bin" / "alteriom-hil-admin").write_text(
        f'#!/bin/sh\necho "admin $*" >> {calls}\necho \'{{"queue.concurrency": 2}}\'\nexit {admin_exit}\n')
    for stub in [*stubs.iterdir(), venv / "bin" / "alteriom-hil-admin"]:
        stub.chmod(0o755)
    result = subprocess.run(
        ["bash", str(CONTROL)], capture_output=True, text=True,
        env={**os.environ, "PATH": f"{stubs}:{os.environ.get('PATH', '')}", "ALTERIOM_HIL_UPDATE_DIR": str(update),
             "ALTERIOM_HIL_VENV": str(venv)},
    )
    outcome_path = update / f"control-result-{ID}.json"
    outcome = json.loads(outcome_path.read_text()) if outcome_path.exists() else None
    recorded = calls.read_text().splitlines() if calls.exists() else []
    return result, outcome, recorded, update


def test_a_restart_says_so_before_it_restarts_the_node(tmp_path):
    result, outcome, calls, update = _run(tmp_path, "restart")
    assert result.returncode == 0, result.stderr
    assert outcome["status"] == "done" and outcome["id"] == ID
    assert calls == ["systemctl restart alteriom-hil-farm.service"]
    assert not (update / "control.json").exists() and not (update / "control.taken.json").exists()


def test_logs_are_the_tail_of_the_nodes_units(tmp_path):
    result, outcome, calls, _ = _run(tmp_path, "logs", {"lines": 50})
    assert result.returncode == 0, result.stderr
    assert "farm-api: GET /healthz" in outcome["result"]["log"]
    assert calls[0].startswith("journalctl -u alteriom-hil-farm.service") and "-n 50" in calls[0]


def test_settings_are_applied_by_the_admin_cli_and_then_the_node_restarts(tmp_path):
    result, outcome, calls, _ = _run(tmp_path, "configure", {"settings": {"queue.concurrency": 2}})
    assert result.returncode == 0, result.stderr
    assert outcome["status"] == "done" and outcome["result"] == {"applied": {"queue.concurrency": 2}}
    assert calls[0].startswith("admin config set-many --file ")
    assert calls[1] == "systemctl restart alteriom-hil-farm.service"


def test_settings_the_host_refuses_are_reported_and_nothing_restarts(tmp_path):
    result, outcome, calls, _ = _run(tmp_path, "configure", {"settings": {"queue.concurrency": 99}}, admin_exit=2)
    assert outcome["status"] == "failed"
    assert not [call for call in calls if call.startswith("systemctl")]


def test_an_action_the_unit_does_not_know_is_refused(tmp_path):
    result, outcome, calls, _ = _run(tmp_path, "reboot")
    assert outcome["status"] == "failed" and "does not know reboot" in outcome["detail"] and calls == []


# ---- a provider's link, sealed on the portal's page --------------------------------------------
# Real openssl and a real RSA-3072 key; the admin CLI is a stub that records
# what reached its stdin, which is where the decrypted link must go and the
# only place it may.

LINK = "https://api.callmebot.com/whatsapp.php?phone=+15550009876&apikey=246813"
SECRETS = ("246813", "15550009876")
OAEP = ["-pkeyopt", "rsa_padding_mode:oaep", "-pkeyopt", "rsa_oaep_md:sha256", "-pkeyopt", "rsa_mgf1_md:sha256"]

needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl is not installed")


def _openssl(*args, data=None):
    return subprocess.run(["openssl", *args], input=data, capture_output=True, check=True).stdout


@pytest.fixture(scope="module")
def seal_keys(tmp_path_factory):
    if shutil.which("openssl") is None:
        pytest.skip("openssl is not installed")
    keys = {}
    for name in ("rig", "other"):
        folder = tmp_path_factory.mktemp(name)
        private = folder / "provider-seal.key"
        _openssl("genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:3072", "-out", str(private))
        der = _openssl("pkey", "-in", str(private), "-pubout", "-outform", "DER")
        public = folder / "provider-seal.der"
        public.write_bytes(der)
        keys[name] = (private, public, hashlib.sha256(der).hexdigest())
    return keys


def _seal(public: Path, text: str) -> str:
    sealed = _openssl("pkeyutl", "-encrypt", "-pubin", "-keyform", "DER", "-inkey", str(public), *OAEP,
                      data=text.encode())
    return base64.b64encode(sealed).decode()


def _run_provider(tmp_path: Path, action: str, args: dict, private_key: Path, fingerprint: str,
                  admin_exit: int = 0, admin_says: str = ""):
    update = tmp_path / "update"
    update.mkdir(exist_ok=True)
    (update / "control.json").write_text(json.dumps({"id": ID, "action": action, "args": args}))
    stubs = tmp_path / "bin"
    stubs.mkdir(exist_ok=True)
    calls, received = tmp_path / "calls", tmp_path / "received"
    (stubs / "sudo").write_text('#!/bin/sh\nexec "$@"\n')
    (stubs / "systemctl").write_text(f'#!/bin/sh\necho "systemctl $*" >> {calls}\n')
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    said = admin_says or 'stored https://api.callmebot.com/whatsapp.php?phone=***76&apikey=*** in /etc/alteriom-hil/providers/callmebot-url'
    (venv / "bin" / "alteriom-hil-admin").write_text(
        "#!/bin/sh\n"
        f'echo "admin $*" >> {calls}\n'
        'case "$*" in\n'
        f'  *"providers seal-key --fingerprint"*) echo "{fingerprint}"; exit 0 ;;\n'
        f'  *"providers set callmebot"*) cat > {received}; echo "{said}" ;;\n'
        '  *"providers remove callmebot"*) echo "removed /etc/alteriom-hil/providers/callmebot-url; the rig sends no CallMeBot message until a link is stored again" ;;\n'
        f'  *"notify set"*) cat > {received}; echo "{said}" ;;\n'
        "esac\n"
        f"exit {admin_exit}\n")
    for stub in [*stubs.iterdir(), venv / "bin" / "alteriom-hil-admin"]:
        stub.chmod(0o755)
    result = subprocess.run(
        ["bash", str(CONTROL)], capture_output=True, text=True,
        env={**os.environ, "PATH": f"{stubs}:{os.environ.get('PATH', '')}", "ALTERIOM_HIL_UPDATE_DIR": str(update),
             "ALTERIOM_HIL_VENV": str(venv), "ALTERIOM_HIL_SEAL_KEY": str(private_key)},
    )
    outcome_path = update / f"control-result-{ID}.json"
    outcome = json.loads(outcome_path.read_text()) if outcome_path.exists() else None
    recorded = calls.read_text().splitlines() if calls.exists() else []
    return result, outcome, recorded, update, (received.read_text() if received.exists() else None)


def _nothing_leaks(result, update: Path):
    left = result.stdout + result.stderr + "".join(path.read_text(errors="replace") for path in update.rglob("*") if path.is_file())
    for secret in SECRETS:
        assert secret not in left, secret
    assert not (update / "control.json").exists() and not (update / "control.taken.json").exists()


@needs_openssl
def test_a_sealed_link_is_opened_with_the_rigs_key_straight_into_providers_set(tmp_path, seal_keys):
    private, public, fingerprint = seal_keys["rig"]
    args = {"provider": "callmebot", "sealed": _seal(public, LINK), "fingerprint": fingerprint}
    result, outcome, calls, update, received = _run_provider(tmp_path, "provider_set", args, private, fingerprint)
    assert result.returncode == 0, result.stderr
    assert received == LINK, "the admin CLI read the link from its stdin"
    assert outcome["status"] == "done" and "phone=***76&apikey=***" in outcome["detail"]
    assert any(call.endswith("providers seal-key --fingerprint") for call in calls)
    assert any(call.endswith("providers set callmebot") for call in calls)
    assert calls[-1] == "systemctl restart alteriom-hil-farm.service"
    # Never an argument: the command lines the stubs saw do not carry it.
    assert not [call for call in calls if any(secret in call for secret in SECRETS)]
    _nothing_leaks(result, update)


@needs_openssl
def test_a_link_sealed_for_another_key_is_refused_before_anything_is_decrypted(tmp_path, seal_keys):
    private, public, fingerprint = seal_keys["rig"]
    _, other_public, other_fingerprint = seal_keys["other"]
    args = {"provider": "callmebot", "sealed": _seal(other_public, LINK), "fingerprint": other_fingerprint}
    result, outcome, calls, update, received = _run_provider(tmp_path, "provider_set", args, private, fingerprint)
    assert outcome["status"] == "failed" and "reload the rig's page" in outcome["detail"]
    assert received is None and not [call for call in calls if "providers set" in call or call.startswith("systemctl")]
    _nothing_leaks(result, update)


@needs_openssl
def test_a_ciphertext_the_rigs_key_cannot_open_changes_nothing(tmp_path, seal_keys):
    """The fingerprint claims this rig's key, the ciphertext is for another."""
    private, _, fingerprint = seal_keys["rig"]
    _, other_public, _ = seal_keys["other"]
    args = {"provider": "callmebot", "sealed": _seal(other_public, LINK), "fingerprint": fingerprint}
    result, outcome, calls, update, received = _run_provider(tmp_path, "provider_set", args, private, fingerprint)
    assert outcome["status"] == "failed" and "could not open the sealed link" in outcome["detail"]
    assert not received, "nothing decrypted reached the admin CLI"
    assert not [call for call in calls if call.startswith("systemctl")]
    _nothing_leaks(result, update)


@needs_openssl
def test_a_link_the_admin_cli_refuses_is_reported_by_its_error_alone(tmp_path, seal_keys):
    private, public, fingerprint = seal_keys["rig"]
    args = {"provider": "callmebot", "sealed": _seal(public, LINK + "&text=hi"), "fingerprint": fingerprint}
    result, outcome, calls, update, received = _run_provider(
        tmp_path, "provider_set", args, private, fingerprint, admin_exit=2,
        admin_says="error: the CallMeBot link must not include text=; the validation adds its own message")
    assert outcome["status"] == "failed" and outcome["detail"].startswith("error: the CallMeBot link must not include text=")
    assert not [call for call in calls if call.startswith("systemctl")]
    _nothing_leaks(result, update)


@needs_openssl
def test_a_provider_command_for_anything_but_callmebot_or_a_short_ciphertext_is_refused(tmp_path, seal_keys):
    private, public, fingerprint = seal_keys["rig"]
    for args, says in (({"provider": "telegram", "sealed": _seal(public, LINK), "fingerprint": fingerprint}, "provider telegram"),
                       ({"provider": "callmebot", "sealed": base64.b64encode(b"x" * 100).decode(), "fingerprint": fingerprint}, "not an RSA-3072")):
        result, outcome, calls, update, received = _run_provider(tmp_path, "provider_set", args, private, fingerprint)
        assert outcome["status"] == "failed" and says in outcome["detail"], outcome
        assert received is None and not [call for call in calls if call.startswith("systemctl")]
        _nothing_leaks(result, update)
        (update / f"control-result-{ID}.json").unlink()


def test_a_removed_link_is_deleted_by_the_admin_cli_and_the_node_restarts(tmp_path):
    result, outcome, calls, update, _ = _run_provider(tmp_path, "provider_remove", {"provider": "callmebot"},
                                                      tmp_path / "none.key", "0" * 64)
    assert result.returncode == 0, result.stderr
    assert outcome["status"] == "done" and outcome["detail"].startswith("removed /etc/alteriom-hil/providers/callmebot-url")
    assert calls == ["admin providers remove callmebot",
                     "systemctl restart alteriom-hil-farm.service"]
    assert not (update / "control.taken.json").exists()


# ---- a test message ------------------------------------------------------------------------------


def _run_test_message(tmp_path: Path, args: dict, said: str, admin_exit: int = 0):
    update = tmp_path / "update"
    update.mkdir(exist_ok=True)
    (update / "control.json").write_text(json.dumps({"id": ID, "action": "provider_test", "args": args}))
    stubs = tmp_path / "bin"
    stubs.mkdir(exist_ok=True)
    calls, said_file = tmp_path / "calls", tmp_path / "said"
    said_file.write_text(said)
    (stubs / "sudo").write_text('#!/bin/sh\necho "sudo" >> ' + str(calls) + '\nexec "$@"\n')
    (stubs / "systemctl").write_text(f'#!/bin/sh\necho "systemctl $*" >> {calls}\n')
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    # The admin CLI's whole output, noise included; only its result line may be reported.
    (venv / "bin" / "alteriom-hil-admin").write_text(
        f'#!/bin/sh\necho "admin $*" >> {calls}\ncat {said_file}\nexit {admin_exit}\n')
    for stub in [*stubs.iterdir(), venv / "bin" / "alteriom-hil-admin"]:
        stub.chmod(0o755)
    result = subprocess.run(
        ["bash", str(CONTROL)], capture_output=True, text=True,
        env={**os.environ, "PATH": f"{stubs}:{os.environ.get('PATH', '')}", "ALTERIOM_HIL_UPDATE_DIR": str(update),
             "ALTERIOM_HIL_VENV": str(venv)},
    )
    outcome_path = update / f"control-result-{ID}.json"
    outcome = json.loads(outcome_path.read_text()) if outcome_path.exists() else None
    return result, outcome, (calls.read_text().splitlines() if calls.exists() else []), update


NOISE = ("Traceback (most recent call last):\n"
         "  urllib reached https://api.callmebot.com/whatsapp.php?phone=+15550009876&apikey=246813\n")


def test_a_test_message_reports_only_the_admin_clis_result_line_and_restarts_nothing(tmp_path):
    result, outcome, calls, update = _run_test_message(
        tmp_path, {"provider": "callmebot"}, NOISE + "test message queued by CallMeBot (HTTP 200)\n")
    assert result.returncode == 0, result.stderr
    assert outcome == {**outcome, "id": ID, "status": "done", "detail": "test message queued by CallMeBot (HTTP 200)"}
    assert "result" not in outcome
    assert calls == ["sudo", "admin providers test callmebot"]
    assert not [call for call in calls if call.startswith("systemctl")], "nothing restarts"
    _nothing_leaks(result, update)


def test_a_refused_or_failed_test_message_is_reported_by_its_line_alone(tmp_path):
    for said, code, detail in (
        ("CallMeBot refused: rate limited (HTTP 203)\n", 1, "CallMeBot refused: rate limited (HTTP 203)"),
        ("error: today's CallMeBot budget on this rig is used (5 of 5); nothing was sent\n", 2,
         "error: today's CallMeBot budget on this rig is used (5 of 5); nothing was sent"),
        ("could not reach api.callmebot.com: [Errno -3] Temporary failure in name resolution\n", 1,
         "could not reach api.callmebot.com: [Errno -3] Temporary failure in name resolution"),
        (NOISE, 1, "the test message command failed (status 1)"),
    ):
        result, outcome, calls, update = _run_test_message(tmp_path, {"provider": "callmebot"}, NOISE + said, code)
        assert result.returncode == 0, result.stderr
        assert outcome["status"] == "failed" and outcome["detail"] == detail, outcome
        assert not [call for call in calls if call.startswith("systemctl")]
        _nothing_leaks(result, update)
        (update / f"control-result-{ID}.json").unlink()
        (tmp_path / "calls").unlink()


def test_a_test_message_for_another_provider_runs_nothing(tmp_path):
    result, outcome, calls, update = _run_test_message(tmp_path, {"provider": "telegram"}, "test message queued\n")
    assert outcome["status"] == "failed" and "provider telegram" in outcome["detail"]
    assert calls == [] and not (update / "control.taken.json").exists()


@needs_openssl
def test_a_sealed_bot_token_is_opened_with_the_rigs_key_straight_into_notify_set(tmp_path, seal_keys):
    """A bot token is the same kind of thing as a provider's link -- a
    credential the portal must relay and must not be able to read -- so it
    travels the same path rather than a second one."""
    private, public, fingerprint = seal_keys["rig"]
    token = "123456:AA-the-bot-token"
    args = {"channel": "telegram", "sealed": _seal(public, token), "fingerprint": fingerprint,
            "settings": {"chat_id": "-1001234567890"}}
    result, outcome, calls, update, received = _run_provider(
        tmp_path, "notify_set", args, private, fingerprint,
        admin_says="notifications go to telegram; send one with: alteriom-hil-admin notify test")
    assert result.returncode == 0, result.stderr
    assert received == token, "the admin CLI read the token from its stdin"
    assert outcome["status"] == "done"
    assert any(call.endswith("notify set --channel telegram --chat-id -1001234567890") for call in calls)
    # Never an argument: the command lines the stubs saw do not carry it.
    assert not [call for call in calls if token in call]


@needs_openssl
def test_a_channel_this_node_does_not_know_is_refused_before_anything_is_opened(tmp_path, seal_keys):
    private, public, fingerprint = seal_keys["rig"]
    args = {"channel": "carrier-pigeon", "sealed": _seal(public, "x"), "fingerprint": fingerprint,
            "settings": {}}
    result, outcome, calls, update, received = _run_provider(
        tmp_path, "notify_set", args, private, fingerprint)
    assert outcome["status"] == "failed" and "cannot set the channel" in outcome["detail"]
    assert received is None and not [call for call in calls if "notify set" in call]
