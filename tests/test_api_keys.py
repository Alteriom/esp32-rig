"""Named API keys with a role, and a record of who changed what.

One shared token could not say who deleted a bundle, could not be taken back
from one holder without rotating it for every caller, and let anybody holding
it pause the queue. These keep what replaced it honest: a user key does what a
user may and nothing more, a route added later is admin-only until somebody
decides otherwise, the farm's own token keeps working for the CI that uses it,
and every request that could change something is recorded with its key.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import threading
import time
from argparse import Namespace
from pathlib import Path

import pytest
import yaml

from alteriom_hil.api_keys import (
    FARM_KEY_NAME,
    KEY_PREFIX,
    Identity,
    KeyStore,
    add_key,
    digest,
    keys_document,
    keys_path_for,
    load_keys,
    parse_keys,
    required_role,
)

REPO = Path(__file__).resolve().parents[1]
RUNNER = REPO / "runner"
# The service's own source: `alteriom_hil.service` in the core, which is
# where the routes and the handler live (docs/public-release-plan.md, 12c).
SERVICE = REPO / "core" / "alteriom_hil" / "service.py"
# The dashboard is the rig's bundle and the site is the portal's; a portal
# serves both from one root (docs/public-release-plan.md, step 12d).
WEB = REPO / "rig" / "web"
PORTAL_WEB = REPO / "portal" / "web"
FARM_TOKEN = "f" * 64
JOB = "0123456789abcdef0123456789abcdef"


# ---- the keys file --------------------------------------------------------------


def test_a_key_is_shown_once_and_stored_as_its_hash(tmp_path):
    entries, key = add_key([], "sparck", "user", note="laptop")
    assert key.startswith(KEY_PREFIX) and len(key) == len(KEY_PREFIX) + 64
    assert entries == [{"name": "sparck", "role": "user", "sha256": digest(key), "created_at": entries[0]["created_at"], "note": "laptop"}]
    path = tmp_path / "api-keys.yaml"
    path.write_text(keys_document(entries))
    assert key not in path.read_text(), "the file never holds a key"
    assert load_keys(path) == entries
    assert load_keys(tmp_path / "absent.yaml") == [], "no file is no named keys"
    assert keys_path_for("/etc/alteriom-hil/api-token") == Path("/etc/alteriom-hil/api-keys.yaml")


def test_a_keys_file_that_is_wrong_is_refused_with_the_reason():
    good = {"name": "sparck", "role": "user", "sha256": "a" * 64}
    cases = [
        ({"keys": [{**good, "name": FARM_KEY_NAME}]}, "the farm's own token"),
        ({"keys": [{**good, "role": "owner"}]}, "role must be one of admin, user, node"),
        ({"keys": [{**good, "sha256": "not-a-digest"}]}, "64 lowercase hex"),
        ({"keys": [{**good, "name": "Has Space"}]}, "must be 1-32"),
        ({"keys": [good, {**good, "sha256": "b" * 64}]}, "appears twice"),
        ({"keys": [good, {**good, "name": "other"}]}, "same key as another"),
        (["not", "a", "mapping"], "must be a mapping"),
    ]
    for document, message in cases:
        with pytest.raises(ValueError, match=message):
            parse_keys(document)


def test_the_store_knows_the_farm_token_and_every_named_key_and_notices_changes(tmp_path):
    path = tmp_path / "api-keys.yaml"
    store = KeyStore(FARM_TOKEN, path)
    assert store.identify(FARM_TOKEN) == Identity(FARM_KEY_NAME, "admin")
    assert store.identify("afk_" + "0" * 64) is None
    assert store.identify("") is None and store.identify(None) is None

    entries, user_key = add_key([], "sparck", "user")
    path.write_text(keys_document(entries))
    assert store.identify(user_key) == Identity("sparck", "user"), "created: honoured without a restart"

    entries, admin_key = add_key(entries, "dominic", "admin")
    path.write_text(keys_document(entries))
    os.utime(path, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
    assert store.identify(admin_key) == Identity("dominic", "admin")

    # Revoked: refused from the next request.
    path.write_text(keys_document([entry for entry in entries if entry["name"] != "sparck"]))
    os.utime(path, ns=(time.time_ns() + 2 * 10**9, time.time_ns() + 2 * 10**9))
    assert store.identify(user_key) is None

    # A broken file keeps nobody's key alive -- not even one it held before --
    # and locks nobody out: the farm's own token still works, and says why.
    path.write_text("keys: [{name: dominic, role: god, sha256: nope}]")
    os.utime(path, ns=(time.time_ns() + 3 * 10**9, time.time_ns() + 3 * 10**9))
    assert store.identify(admin_key) is None
    assert store.identify(FARM_TOKEN) == Identity(FARM_KEY_NAME, "admin")
    assert "role must be one of" in store.error

    with pytest.raises(ValueError, match="at least 32"):
        KeyStore("short")


# ---- what each role may do ---------------------------------------------------------


# Every route that changes something, and the role it needs. A route added to
# the service without a line here fails the test below: the decision is made
# on purpose, never by default.
WRITES = {
    # Where the farm's own events go is an admin's; where one rig's go is set
    # from that rig's page (alteriom_hil.webhooks, docs/webhooks.md).
    # Your own browsers: a person signs one of their own sessions out, so it
    # is a user's and not an admin's. The handler scopes it to the asking
    # session's account, and GUEST_ROUTES lets somebody not yet named do it too.
    ("POST", "/api/v1/sessions/revoke"): "user",
    ("POST", "/api/v1/webhooks"): "admin",
    ("POST", "/api/v1/webhooks/abc123"): "admin",
    ("POST", "/api/v1/webhooks/abc123/test"): "admin",
    ("DELETE", "/api/v1/webhooks/abc123"): "admin",
    # Whose rig it is, is the farm's admin to say -- a user administers the
    # rigs they own, and does not hand themselves another.
    ("POST", "/api/v1/rigs/rig02/owner"): "admin",
    # Who sees a rig is its owner's to say; the service checks ownership.
    ("POST", "/api/v1/rigs/rig02/visibility"): "user",
    ("POST", "/api/v1/rigs/rig02/webhooks"): "user",
    ("POST", "/api/v1/rigs/rig02/webhooks/abc123"): "user",
    ("POST", "/api/v1/rigs/rig02/webhooks/abc123/test"): "user",
    ("DELETE", "/api/v1/rigs/rig02/webhooks/abc123"): "user",
    ("POST", "/api/v1/inventory/register"): "admin",
    ("POST", "/api/v1/inventory/refresh"): "user",
    ("POST", "/api/v1/suites"): "user",
    ("POST", "/api/v1/health"): "user",
    ("POST", "/api/v1/artifacts"): "user",
    ("POST", "/api/v1/builds"): "admin",
    ("POST", f"/api/v1/jobs/{JOB}/cancel"): "user",
    ("POST", f"/api/v1/jobs/{JOB}/promote"): "admin",
    # The farm's own notifications: what an admin watching the fleet hears.
    ("POST", "/api/v1/farm/notify"): "admin",
    ("POST", "/api/v1/farm/notify/test"): "admin",
    ("POST", "/api/v1/queue/pause"): "admin",
    ("POST", "/api/v1/queue/resume"): "admin",
    ("POST", f"/api/v1/artifacts/{JOB}/pin"): "admin",
    ("POST", f"/api/v1/artifacts/{JOB}/unpin"): "admin",
    ("POST", "/api/v1/artifacts/prune"): "admin",
    ("POST", "/api/v1/retention/run"): "admin",
    ("DELETE", f"/api/v1/artifacts/{JOB}"): "admin",
    ("DELETE", "/api/v1/inventory/esp32-01"): "admin",
    ("POST", "/api/v1/inventory/esp32-01/reserve"): "admin",
    ("POST", "/api/v1/inventory/esp32-01/release"): "admin",
    ("POST", "/api/v1/workers/esp32-hil/hello"): "node",
    ("POST", "/api/v1/workers/esp32-hil/heartbeat"): "node",
    ("POST", "/api/v1/workers/esp32-hil/lease"): "node",
    ("POST", f"/api/v1/jobs/{JOB}/stages"): "node",
    ("POST", f"/api/v1/jobs/{JOB}/log"): "node",
    ("POST", f"/api/v1/jobs/{JOB}/evidence"): "node",
    ("POST", f"/api/v1/jobs/{JOB}/result"): "node",
    ("POST", "/api/v1/workers/esp32-hil/history/known"): "node",
    ("POST", f"/api/v1/workers/esp32-hil/history/artifacts/{JOB}"): "node",
    ("POST", f"/api/v1/workers/esp32-hil/history/jobs/{JOB}"): "node",
    ("POST", f"/api/v1/workers/esp32-hil/history/jobs/{JOB}/evidence"): "node",
    ("POST", f"/api/v1/workers/esp32-hil/history/jobs/{JOB}/log"): "node",
    ("POST", f"/api/v1/workers/esp32-hil/history/jobs/{JOB}/link"): "node",
    ("POST", f"/api/v1/workers/esp32-hil/commands/{JOB}"): "node",
    # Managing a rig is an admin's.
    ("POST", "/api/v1/workers/esp32-hil/commands"): "admin",
    ("POST", "/api/v1/workers/esp32-hil/drain"): "admin",
    ("POST", "/api/v1/workers/esp32-hil/resume"): "admin",
    ("DELETE", "/api/v1/workers/esp32-hil"): "admin",
    # Adding a rig is an admin's; the call the rig makes carries no key.
    ("POST", "/api/v1/rigs"): "admin",
    ("POST", "/api/v1/rigs/rig-2/join"): "admin",
    ("PATCH", "/api/v1/rigs/rig-2"): "admin",
    ("DELETE", "/api/v1/rigs/rig-2"): "admin",
    # What the nodes run is an admin's to say.
    ("POST", "/api/v1/releases"): "admin",
    ("POST", f"/api/v1/releases/{'c' * 40}/current"): "admin",
}


def test_each_route_that_changes_something_needs_the_role_decided_for_it():
    for (method, path), role in WRITES.items():
        assert required_role(method, path) == role, f"{method} {path}"
    # A route nobody has decided about is an admin's.
    assert required_role("POST", "/api/v1/something-new") == "admin"
    assert required_role("DELETE", "/api/v1/jobs") == "admin"
    assert required_role("POST", f"/api/v1/jobs/{JOB}/cancel/extra") == "admin"
    # Reading is a user's, except who did what, and the keys.
    assert required_role("GET", "/api/v1/status") == "user"
    assert required_role("GET", "/api/v1/inventory/esp32-01/details") == "user"
    assert required_role("GET", "/api/v1/audit") == "admin"


def test_a_worker_key_reaches_the_worker_routes_and_nothing_else():
    from alteriom_hil.api_keys import Identity, allowed

    node = Identity("esp32-hil", "node")
    admin = Identity("farm", "admin")
    user = Identity("sparck", "user")
    worker_calls = [
        ("POST", "/api/v1/workers/esp32-hil/hello"),
        ("POST", "/api/v1/workers/esp32-hil/heartbeat"),
        ("POST", "/api/v1/workers/esp32-hil/lease"),
        ("POST", f"/api/v1/jobs/{JOB}/stages"),
        ("POST", f"/api/v1/jobs/{JOB}/log"),
        ("POST", f"/api/v1/jobs/{JOB}/evidence"),
        ("POST", f"/api/v1/jobs/{JOB}/result"),
    ]
    for method, path in worker_calls:
        assert required_role(method, path) == "node", path
        assert allowed(node, method, path), path
        assert not allowed(admin, method, path), f"an admin key cannot pose as a worker: {path}"
        assert not allowed(user, method, path), path
    assert allowed(node, "GET", f"/api/v1/artifacts/{JOB}/bundle"), "the bundle it was given"
    assert allowed(node, "GET", f"/api/v1/releases/{'c' * 40}/bundle"), "the release it is told to run"
    assert not allowed(node, "GET", "/api/v1/releases"), "the index is a person's"
    assert allowed(node, "GET", "/api/v1/releases/current"), "which release, for a rig still joining"
    assert required_role("GET", "/api/v1/rigs") == "user", "what rigs there are is a user's to read"
    assert not allowed(node, "POST", "/api/v1/releases"), "a node installs releases; it does not make them"
    assert allowed(user, "GET", f"/api/v1/artifacts/{JOB}/bundle")
    for method, path in (("GET", "/api/v1/status"), ("GET", "/api/v1/jobs"), ("POST", "/api/v1/suites"),
                         ("POST", f"/api/v1/jobs/{JOB}/cancel"), ("POST", "/api/v1/queue/pause"),
                         ("GET", "/api/v1/audit"), ("POST", "/api/v1/workers/esp32-hil/other")):
        assert not allowed(node, method, path), f"a worker key reads and changes nothing else: {method} {path}"


def test_no_route_in_the_service_is_left_out_of_that_decision():
    source = SERVICE.read_text(encoding="utf-8")
    post = source.split("def _post(self, path: str, identity: Identity):", 1)[1].split("def _delete(", 1)[0]
    delete = source.split("def _delete(self, path: str, identity: Identity):", 1)[1].split("def log_message", 1)[0]
    literal = set(re.findall(r'path == "(/api/v1/[^"]+)"', post))
    literal |= set(re.findall(r'"(/api/v1/[a-z/]+)": "(?:inventory|suite)"', post))
    decided = {path for (method, path) in WRITES if method == "POST"}
    assert literal <= decided, f"routes with no decided role: {sorted(literal - decided)}"
    patterns = re.findall(r're\.fullmatch\(r"(/api/v1/[^"]+)", path\)', post + delete)
    for pattern in patterns:
        assert any(re.fullmatch(pattern, path) for (_, path) in WRITES), f"no decided role for {pattern}"


# ---- the service ---------------------------------------------------------------------


def _service():
    spec = importlib.util.spec_from_file_location("farm_service_keys", RUNNER / "farm_service.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeManager:
    """Just what the routes under test call; the store is the real one."""

    def __init__(self, farm_service, tmp_path):
        self.store = farm_service.JobStore(tmp_path / "farm.sqlite3")
        self.log = tmp_path / "x.log"
        self.paused = False

    def submit(self, kind, request, submitted_by=None):
        request = dict(request)
        if submitted_by:
            request["submitted_by"] = submitted_by
        return self.store.create(kind, request, self.log)

    def cancel(self, job_id, reason):
        if self.store.get(job_id) is None:
            raise KeyError(job_id)
        self.store.update(job_id, "cancelled", {"summary": reason})
        return self.store.get(job_id)

    def pause(self):
        self.paused = True
        return {"paused": True}

    def delete_artifact(self, bundle_id):
        raise KeyError(bundle_id)


def test_a_user_key_does_what_a_user_may_and_every_change_is_recorded(tmp_path):
    from http.server import ThreadingHTTPServer
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    farm_service = _service()
    token_file = tmp_path / "api-token"
    token_file.write_text(FARM_TOKEN)
    keys_file = keys_path_for(token_file)
    entries, user_key = add_key([], "sparck", "user")
    entries, other_key = add_key(entries, "someone", "user")
    keys_file.write_text(keys_document(entries))

    manager = FakeManager(farm_service, tmp_path)
    handler = farm_service.make_handler(manager, KeyStore(FARM_TOKEN, keys_file), tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(method, path, key=None, body=None):
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        data = json.dumps(body).encode() if body is not None else None
        if data is not None:
            headers["Content-Type"] = "application/json"
        try:
            with urlopen(Request(base + path, data=data, method=method, headers=headers), timeout=5) as response:
                return response.status, json.loads(response.read() or b"null")
        except HTTPError as error:
            return error.code, json.loads(error.read() or b"null")

    try:
        assert call("GET", "/api/v1/whoami")[0] == 401
        assert call("POST", "/api/v1/queue/pause", body={})[0] == 401
        assert call("GET", "/api/v1/whoami", user_key) == (200, {"name": "sparck", "role": "user"})
        assert call("GET", "/api/v1/whoami", FARM_TOKEN) == (200, {"name": "farm", "role": "admin"})

        # What a user may do.
        status, job = call("POST", "/api/v1/suites", user_key, {"profile": "canary"})
        assert status == 202 and job["request"]["submitted_by"] == "sparck"
        assert call("POST", f"/api/v1/jobs/{job['id']}/cancel", user_key, {})[0] == 200

        # What a user may not.
        status, refused = call("POST", "/api/v1/queue/pause", user_key, {})
        assert status == 403 and refused["error"] == "POST /api/v1/queue/pause needs an admin key; sparck is a user"
        assert manager.paused is False
        assert call("DELETE", f"/api/v1/artifacts/{JOB}", user_key)[0] == 403
        assert call("GET", "/api/v1/audit", user_key)[0] == 403
        # Somebody else's run, or the farm's, is not a user's to cancel.
        _, theirs = call("POST", "/api/v1/suites", other_key, {"profile": "canary"})
        status, refused = call("POST", f"/api/v1/jobs/{theirs['id']}/cancel", user_key, {})
        assert status == 403 and "started by someone" in refused["error"]
        _, farms = call("POST", "/api/v1/suites", FARM_TOKEN, {"profile": "canary"})
        status, refused = call("POST", f"/api/v1/jobs/{farms['id']}/cancel", user_key, {})
        assert status == 403 and "started by farm" in refused["error"]
        assert call("POST", f"/api/v1/jobs/{JOB}/cancel", user_key, {})[0] == 404

        # An admin may.
        assert call("POST", "/api/v1/queue/pause", FARM_TOKEN, {})[0] == 200 and manager.paused

        # Every request that could change something, with its key and answer;
        # a request with no key at all is nobody's and not recorded.
        status, audit = call("GET", "/api/v1/audit", FARM_TOKEN)
        assert status == 200
        rows = [(row["key_name"], row["method"], row["path"], row["status"]) for row in audit["entries"]]
        assert rows[0] == ("farm", "POST", "/api/v1/queue/pause", 200), "newest first"
        assert ("sparck", "POST", "/api/v1/queue/pause", 403) in rows
        assert ("sparck", "DELETE", f"/api/v1/artifacts/{JOB}", 403) in rows
        assert ("sparck", "POST", "/api/v1/suites", 202) in rows
        assert ("sparck", "POST", f"/api/v1/jobs/{JOB}/cancel", 404) in rows
        assert audit["total"] == len(rows) == 10
        assert all(row["role"] in ("admin", "user") and row["at"] for row in audit["entries"])

        # Revoked: refused from the next request.
        keys_file.write_text(keys_document([entry for entry in entries if entry["name"] != "sparck"]))
        os.utime(keys_file, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
        assert call("GET", "/api/v1/whoami", user_key)[0] == 401
    finally:
        server.shutdown()
        server.server_close()


def test_a_request_cannot_claim_to_be_somebody_elses():
    farm_service = _service()
    manager = farm_service.FarmManager.__new__(farm_service.FarmManager)
    with pytest.raises(ValueError, match="unknown request fields"):
        manager._validate("inventory", {"submitted_by": "admin"})


# ---- the admin CLI -----------------------------------------------------------------------


def test_the_admin_cli_creates_lists_and_revokes_keys(tmp_path, monkeypatch, capsys):
    sys.path.insert(0, str(RUNNER))
    import admin_cli

    token_file = tmp_path / "api-token"
    token_file.write_text(FARM_TOKEN)
    config = yaml.safe_load((RUNNER / "hil-config.example.yaml").read_text())
    config["service"]["token_file"] = str(token_file)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    monkeypatch.setattr(admin_cli, "require_root", lambda: None)

    assert admin_cli.command_keys_create(Namespace(config=config_path, name="sparck", role="user", note="laptop")) == 0
    printed = capsys.readouterr().out
    key = next(line for line in printed.splitlines() if line.startswith(KEY_PREFIX))
    store = KeyStore(FARM_TOKEN, keys_path_for(token_file))
    assert store.identify(key) == Identity("sparck", "user")
    assert key not in keys_path_for(token_file).read_text()

    with pytest.raises(admin_cli.hil_config.ConfigError, match="appears twice"):
        admin_cli.command_keys_create(Namespace(config=config_path, name="sparck", role="admin", note=None))

    assert admin_cli.command_keys_list(Namespace(config=config_path, json=True)) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [(entry["name"], entry["role"]) for entry in listed] == [("farm", "admin"), ("sparck", "user")]
    assert all("sha256" not in entry for entry in listed)

    with pytest.raises(admin_cli.hil_config.ConfigError, match="rotate"):
        admin_cli.command_keys_revoke(Namespace(config=config_path, name="farm"))
    assert admin_cli.command_keys_revoke(Namespace(config=config_path, name="sparck")) == 0
    assert KeyStore(FARM_TOKEN, keys_path_for(token_file)).identify(key) is None
    with pytest.raises(admin_cli.hil_config.ConfigError, match="no key named"):
        admin_cli.command_keys_revoke(Namespace(config=config_path, name="sparck"))


# ---- the dashboard ---------------------------------------------------------------------------


def test_the_dashboard_offers_a_user_key_only_what_it_may_do():
    page = (WEB / "index.html").read_text(encoding="utf-8")
    # The dashboard is the rig's pages and the portal's shell, two files.
    script = "\n".join((WEB / name if (WEB / name).is_file() else PORTAL_WEB / name).read_text(encoding="utf-8")
                      for name in ("app.js", "portal-shell.js"))
    style = (WEB / "app.css").read_text(encoding="utf-8")
    assert 'body[data-role="user"] .admin-only{display:none!important}' in style
    assert "renderYou(data.you);" in script and 'id="you"' in page
    # Each admin-only control carries the class; the service refuses them anyway.
    for marker in ("queue-toggle admin-only", "secondary admin-only unregister-board", "register-board admin-only",
                   "bundle-pin admin-only", "bundle-delete admin-only", "admin-only rig-command", "admin-only rig-drain",
                   "admin-only rig-delete", "admin-only board-read", "admin-only rig-edit", "admin-only rig-join-new"):
        assert marker in script, marker
    assert '<section class="card admin-only">' in page, "pruning is an admin's"
    # Cancel is offered for a user's own runs; reordering never.
    buttons = script.split("function jobActionButtons", 1)[1].split("\n}", 1)[0]
    assert "job.request.submitted_by === you?.name" in buttons and "promotable && isAdmin()" in buttons
    assert 'api("/api/v1/audit?limit=25")' in script and 'id="audit"' in page


def test_a_portal_manages_its_keys_file_without_a_host_configuration(tmp_path, capsys):
    from alteriom_hil import api_keys

    keys = tmp_path / "api-keys.yaml"
    assert api_keys.main(["--file", str(keys), "create", "--name", "esp32-hil", "--role", "node"]) == 0
    key = capsys.readouterr().out.strip()
    store = KeyStore("t" * 40, keys)
    assert store.identify(key).role == "node" and store.identify(key).name == "esp32-hil"
    assert "sha256" in keys.read_text() and key not in keys.read_text(), "the file holds the digest, never the key"
    assert api_keys.main(["--file", str(keys), "list"]) == 0
    assert "esp32-hil" in capsys.readouterr().out
    assert api_keys.main(["--file", str(keys), "revoke", "--name", "esp32-hil"]) == 0
    assert KeyStore("t" * 40, keys).identify(key) is None


def test_the_service_makes_a_key_once_by_name_and_only_two_calls_carry_none(tmp_path):
    from alteriom_hil.api_keys import public_route

    store = KeyStore("f" * 40, tmp_path / "api-keys.yaml")
    key = store.create("rig-2", "node", note="joined from raspberrypi")
    assert store.identify(key).name == "rig-2" and store.identify(key).role == "node"
    assert key not in (tmp_path / "api-keys.yaml").read_text(encoding="utf-8")
    for name in ("rig-2", "farm"):
        with pytest.raises(ValueError):
            store.create(name, "node")
    assert public_route("GET", "/api/v1/join.sh") and public_route("POST", "/api/v1/enroll")
    assert not public_route("POST", "/api/v1/join.sh") and not public_route("GET", "/api/v1/enroll")
    assert not public_route("POST", "/api/v1/rigs") and not public_route("GET", "/api/v1/status")
    # The world page is for anyone: the one read that carries no key, and it
    # carries nothing that is anybody's (test_portal_node).
    assert public_route("GET", "/api/v1/world") and not public_route("GET", "/api/v1/world/rig02")


def test_a_node_may_say_it_has_begun_a_command():
    """The node reports a start before doing the work and the result after.
    Both are the same command, on the same worker, and a role that may report
    one may report the other -- the route allowlist had only the result, so
    every start was refused and no command ever showed as running."""
    from alteriom_hil.api_keys import required_role

    command = "/api/v1/workers/rig02/commands/" + "a" * 32
    assert required_role("POST", command) == "node"
    assert required_role("POST", command + "/start") == "node"
    # And nothing further down that path is a node's to call.
    assert required_role("POST", command + "/start/anything") != "node"


def test_a_guest_may_say_who_they_are_and_nothing_else_and_a_key_cannot_take_a_handle():
    """A person who signed in and is on no list is a guest: `whoami` and the
    keyless world page, and a 403 for everything a user key may read. And
    the other half of the one namespace: a key is refused a name that is an
    account's handle, because the key would be that person."""
    from alteriom_hil.api_keys import Identity, add_key, allowed

    guest = Identity("stranger", "guest")
    assert allowed(guest, "GET", "/api/v1/whoami")
    for method, path in (("GET", "/api/v1/status"), ("GET", "/api/v1/jobs"), ("GET", "/api/v1/rigs/rig02"),
                         ("GET", "/api/v1/artifacts"), ("POST", "/api/v1/rigs/rig02/visibility"),
                         ("POST", "/api/v1/suite"), ("GET", "/api/v1/audit"), ("POST", "/api/v1/inventory/refresh")):
        assert not allowed(guest, method, path), (method, path)
    assert not guest.is_admin
    # Their own sessions, though: seeing where you are signed in and ending
    # one of those is about the caller and nothing of the farm's. A guest is
    # let in by GUEST_ROUTES; every other role reaches them the ordinary way,
    # and a `user` needs the USER_WRITES line -- without it the POST falls
    # through to "a route nobody decided about is an admin's" and a person
    # cannot sign their own phone out.
    for who in (guest, Identity("sparck", "user"), Identity("farm", "admin")):
        assert allowed(who, "GET", "/api/v1/sessions"), who.role
        assert allowed(who, "POST", "/api/v1/sessions/revoke"), who.role
    # A worker key is not a person and has no browsers.
    assert not allowed(Identity("esp32-hil", "node"), "POST", "/api/v1/sessions/revoke")

    entries, _ = add_key([], "ada", "user")
    with pytest.raises(ValueError, match="ada-lovelace is an account's handle"):
        add_key(entries, "ada-lovelace", "user", reserved={"ada-lovelace", "boss"})
    entries, _ = add_key(entries, "ci", "user", reserved={"ada-lovelace", "boss"})
    assert [entry["name"] for entry in entries] == ["ada", "ci"]


def test_the_cli_reads_the_farms_accounts_before_naming_a_key(tmp_path):
    """The other half of the one namespace, where keys are made: the CLI
    reads the portal's store -- read-only, and only if it exists -- and
    refuses a key named after an account's handle."""
    import sqlite3
    import sys

    sys.path.insert(0, str(REPO / "runner"))
    import admin_cli

    assert admin_cli.account_handles(tmp_path) == set(), "no store yet: nothing reserved"
    with sqlite3.connect(tmp_path / "farm.sqlite3") as db:
        db.execute("CREATE TABLE unrelated (x)")
    assert admin_cli.account_handles(tmp_path) == set(), "a store from before accounts: nothing reserved"
    with sqlite3.connect(tmp_path / "farm.sqlite3") as db:
        db.execute("CREATE TABLE accounts (id TEXT PRIMARY KEY, handle TEXT NOT NULL UNIQUE)")
        db.executemany("INSERT INTO accounts VALUES (?, ?)", [("1", "ada-lovelace"), ("2", "boss")])
    assert admin_cli.account_handles(tmp_path) == {"ada-lovelace", "boss"}
    # A store that cannot be read is not an empty one: no key is named on a
    # guess about who exists, because the guess is wrong the moment the
    # store is back.
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "farm.sqlite3").write_bytes(b"this is not a database" * 40)
    with pytest.raises(RuntimeError, match="not a readable store"):
        admin_cli.account_handles(broken)
    from alteriom_hil.api_keys import main, write_keys
    keys_file = tmp_path / "k.yaml"
    write_keys(keys_file, [])
    with pytest.raises(SystemExit):
        main(["--file", str(keys_file), "--state", str(broken), "create", "--name", "x", "--role", "user"])


def test_every_key_made_through_a_store_or_a_cli_is_refused_an_accounts_handle(tmp_path, monkeypatch, capsys):
    """Reservation is not opt-in. The KeyStore a portal enrols rigs through
    asks its `reserved` for the accounts' handles on every create; the
    handler wires that to the manager; and the container CLI reads them
    from --state. Three doors, one namespace."""
    from alteriom_hil.api_keys import KeyStore, main, write_keys

    keys_file = tmp_path / "api-keys.yaml"
    write_keys(keys_file, [])
    store = KeyStore("t" * 40, keys_file, reserved=lambda: {"ada-lovelace"})
    with pytest.raises(ValueError, match="ada-lovelace is an account's handle"):
        store.create("ada-lovelace", "node")
    assert store.create("rig09", "node"), "a name nobody has is a key"

    # The handler gives the store the manager's accounts when nobody else has.
    import sys
    sys.path.insert(0, str(REPO / "runner"))
    import farm_service
    portal = farm_service.manager_for("portal")(REPO, tmp_path / "portal", tmp_path / "none.yaml", tmp_path / "none-map.yaml",
                                                Path(sys.executable), mode="portal")
    portal.store.create_account("boss", email="boss@example.org", email_verified=True)
    bare = KeyStore("t" * 40, keys_file)
    farm_service.make_handler(portal, bare, WEB)
    assert bare.reserved() == {"boss"}
    with pytest.raises(ValueError, match="boss is an account's handle"):
        bare.create("boss", "user")

    # The container CLI, with the state directory named.
    state = tmp_path / "state"
    state.mkdir()
    import sqlite3
    with sqlite3.connect(state / "farm.sqlite3") as db:
        db.execute("CREATE TABLE accounts (id TEXT PRIMARY KEY, handle TEXT NOT NULL UNIQUE)")
        db.execute("INSERT INTO accounts VALUES ('1', 'grace')")
    with pytest.raises(SystemExit):
        main(["--file", str(keys_file), "--state", str(state), "create", "--name", "grace", "--role", "user"])
    assert "grace is an account's handle" in capsys.readouterr().err
    assert main(["--file", str(keys_file), "--state", str(state), "create", "--name", "ci", "--role", "user"]) == 0


def test_naming_a_principal_is_one_critical_section_across_processes(tmp_path):
    """The reservation is a snapshot unless the check and the write are
    held together: a file lock beside the keys file, taken by the store, by
    account creation and by both CLIs. Held here in one thread, a key
    creation in another waits for it."""
    import threading
    import time

    pytest.importorskip("fcntl")
    from alteriom_hil.api_keys import KeyStore, namespace_lock, write_keys

    keys_file = tmp_path / "api-keys.yaml"
    write_keys(keys_file, [])
    store = KeyStore("t" * 40, keys_file)
    done = threading.Event()
    order = []

    def create_later():
        store.create("waiter", "user")
        order.append(("created", time.monotonic()))
        done.set()

    with namespace_lock(keys_file):
        thread = threading.Thread(target=create_later)
        thread.start()
        time.sleep(0.3)
        order.append(("released", time.monotonic()))
        assert not done.is_set(), "the store must wait for the lock this test holds"
    assert done.wait(5)
    assert [what for what, _ in order] == ["released", "created"]
    assert (tmp_path / "api-keys.yaml.lock").exists()


def test_a_second_read_that_fails_is_not_an_empty_keys_file(tmp_path, monkeypatch):
    """entries() reads the file itself after _refresh(); when that read
    fails on a file whose stamp has not changed -- permissions gone, mtime
    and size the same -- _refresh() saw no reason to reopen it and set no
    error, and an empty listing let an account take the name of a key
    still cached and active. The error is set, the cache dropped, and the
    next look reads the file again."""
    from alteriom_hil import api_keys
    from alteriom_hil.api_keys import KeyStore, add_key, write_keys

    keys_file = tmp_path / "api-keys.yaml"
    entries, key = add_key([], "alice", "user")
    write_keys(keys_file, entries)
    store = KeyStore("f" * 64, keys_file)
    assert [entry["name"] for entry in store.entries()] == ["alice"] and store.error is None
    assert store.identify(key) is not None

    def refused(path):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(api_keys, "load_keys", refused)
    assert store.entries() == []
    assert store.error and "Permission denied" in store.error, "the failure is a fact, not an empty file"
    assert store.identify(key) is None, "and no cached key outlives it"
    monkeypatch.undo()
    assert [entry["name"] for entry in store.entries()] == ["alice"] and store.error is None
    assert store.identify(key) is not None


def test_a_store_that_cannot_be_looked_at_is_not_a_missing_one(tmp_path, monkeypatch):
    """is_file() answered False to every error and not only to "not
    there": a parent that cannot be searched, a mount that is gone, were
    "no store yet", and a key was named on that. Only an absent store is
    an empty one; any other failure names no key."""
    import sqlite3
    from pathlib import Path

    import pytest

    from alteriom_hil.api_keys import account_handles

    with sqlite3.connect(tmp_path / "farm.sqlite3") as db:
        db.execute("CREATE TABLE accounts (id TEXT PRIMARY KEY, handle TEXT NOT NULL UNIQUE)")
        db.execute("INSERT INTO accounts VALUES ('1', 'ada')")
    assert account_handles(tmp_path) == {"ada"}
    assert account_handles(tmp_path / "nowhere") == set(), "absent is empty"
    real_stat = Path.stat

    def refused(self, *args, **kwargs):
        if self.name == "farm.sqlite3":
            raise PermissionError(13, "Permission denied", str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", refused)
    with pytest.raises(RuntimeError, match="no key is named"):
        account_handles(tmp_path)


def test_key_names_raises_rather_than_answer_an_empty_set(tmp_path, monkeypatch):
    """A namespace check reads the key names through key_names(), which under
    the store's lock either returns them or raises -- never an empty set with
    the reason in a field a concurrent request could clear before the caller
    reads it. entries() keeps its lenient empty-plus-error shape, for the
    status page that only displays it."""
    from alteriom_hil import api_keys
    from alteriom_hil.api_keys import KeyReadError, KeyStore, add_key, write_keys

    keys_file = tmp_path / "api-keys.yaml"
    entries, _ = add_key([], "alice", "user")
    write_keys(keys_file, entries)
    store = KeyStore("f" * 64, keys_file)
    assert store.key_names() == {"alice"}

    def refused(path):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(api_keys, "load_keys", refused)
    with pytest.raises(KeyReadError):
        store.key_names()
    assert store.entries() == [], "the lenient reader still answers empty for display"
    assert store.error and "Permission denied" in store.error
    monkeypatch.undo()
    assert store.key_names() == {"alice"}, "readable again: named again"


def test_deleting_a_corrupt_keys_file_clears_the_read_error(tmp_path):
    """Recovering from a corrupt api-keys.yaml by deleting it -- an absent
    file is a valid empty store -- must clear the error, not stay blocked.
    The retry stamp is a sentinel, not None, so the refresh does not mistake
    the deleted file's absent-stamp for the one it forgot and skip the
    reload that clears the error."""
    from alteriom_hil.api_keys import KeyReadError, KeyStore, add_key, write_keys

    keys_file = tmp_path / "api-keys.yaml"
    entries, _ = add_key([], "alice", "user")
    write_keys(keys_file, entries)
    store = KeyStore("f" * 64, keys_file)
    assert store.key_names() == {"alice"}

    keys_file.write_text("keys: [this is: not: yaml\n", encoding="utf-8")
    import pytest
    with pytest.raises(KeyReadError):
        store.key_names()

    keys_file.unlink()  # the operator throws the broken file away
    assert store.key_names() == set(), "an absent file is an empty store, and the error is gone"
    assert store.error is None
    # And a fresh key written after that is found, not lost behind a stale error.
    assert store.create("bob", "user")
    assert store.key_names() == {"bob"}
