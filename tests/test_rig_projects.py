"""A rig manages its own projects, and shows the farm it could join.

A fresh rig ships two profiles -- the Rig Health Check and the painlessMesh
reference -- and a person who just installed it has neither of their own.
Settings -> Projects is where they add one: the rig writes the profile
document under `<state>/profiles/`, beside nothing the release owns, so an
upgrade never touches it and a mistake never stops the service starting.
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
import yaml

from alteriom_hil import launcher as farm_service
from alteriom_hil import profiles, rig_manager
from alteriom_hil.api_keys import KeyStore, add_key, keys_document, keys_path_for

REPO = Path(__file__).resolve().parents[1]


def _rig(tmp_path, mode="standalone"):
    return farm_service.manager_for(mode)(
        REPO, tmp_path / "state", tmp_path / "none.yaml", tmp_path / "none-map.yaml",
        Path(sys.executable), mode=mode)


def _serve(manager, tmp_path, token="t" * 40, keys=None):
    server = ThreadingHTTPServer(("127.0.0.1", 0), farm_service.make_handler(manager, keys or token, tmp_path))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(method, path, body=None, bearer=token):
        data = json.dumps(body).encode() if body is not None else None
        request = Request(base + path, data=data, method=method, headers={
            "Authorization": f"Bearer {bearer}", "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    return server, call


MINE = {"name": "my-sensor", "label": "My sensor firmware",
        "repo": "https://github.com/example/my-sensor", "default_ref": "main",
        "suite_path": "hil/tests", "families": ["esp32", "esp32-c3"]}


# ---- what a rig has, and what it can be given -----------------------------------------

def test_a_fresh_rig_lists_what_it_ships_and_owns_nothing_yet(tmp_path):
    rig = _rig(tmp_path)
    view = rig.projects_view()
    names = {row["name"]: row for row in view["projects"]}
    assert {"canary", "painlessmesh"} <= set(names)
    assert all(row["shipped"] for row in view["projects"]), "the release's, all of them"
    assert view["directory"] == str(tmp_path / "state" / "profiles"), "where the operator's own will be"
    assert view["default_profile"] in names


def test_the_operators_projects_are_read_beside_the_shipped_ones(tmp_path):
    """A document under <state>/profiles/ is a profile like any other: the
    service loads it at start, the run form offers it, and its name cannot
    be one the release already uses -- a clash is said at start, naming the
    file, rather than one document silently winning."""
    local = tmp_path / "state" / "profiles"
    local.mkdir(parents=True)
    doc = {"schema": 1, "name": "mine", "label": "Mine",
           "source": {"location": "consumer", "repo": "https://example.invalid/mine.git"},
           "build": {"revision_key": "mine_sha"},
           "supply": {"repo": "https://example.invalid/mine.git", "workflow": ".github/workflows/hil.yml"},
           "flash": {"command": ["{python}", "-m", "alteriom_hil.flash_artifacts", "--artifacts", "{artifact_dir}"]},
           "suite": {"path": "tests"}}
    (local / "mine.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    rig = _rig(tmp_path)
    assert "mine" in rig.profiles and "canary" in rig.profiles
    assert rig.shipped_profiles == frozenset(profiles.load_profiles(REPO))
    assert "mine" not in rig.shipped_profiles
    assert rig.configuration()["build"]["profile_details"]["mine"]["label"] == "Mine"

    (local / "canary.yaml").write_text(yaml.safe_dump({**doc, "name": "canary"}), encoding="utf-8")
    with pytest.raises(profiles.ProfileError, match="canary is shipped with the rig"):
        _rig(tmp_path)


def test_a_project_is_added_from_a_few_facts_and_is_a_whole_profile(tmp_path):
    """The person knows their repository, where the suite is and which
    families it wants. The rig fills in the shape every project shares:
    checked out per run, flashed by the rig's own flasher from the bundle
    the project's CI built, reported under its label."""
    rig = _rig(tmp_path)
    answer = rig.create_project(MINE)
    path = tmp_path / "state" / "profiles" / "my-sensor.yaml"
    assert Path(answer["path"]) == path and path.is_file()
    written = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert written["source"] == {"location": "consumer", "repo": MINE["repo"], "default_ref": "main"}
    assert written["build"] == {"revision_key": "my_sensor_sha"}
    assert written["supply"] == {"repo": MINE["repo"], "workflow": ".github/workflows/hil.yml", "artifact": "hil-artifacts"}
    assert written["flash"]["command"][:3] == ["{python}", "-m", "alteriom_hil.flash_artifacts"]
    assert "--revision-key" in written["flash"]["command"] and written["flash"]["command"][-1] == "my_sensor_sha"
    assert written["needs"] == [{"target": "esp32", "count": 1}, {"target": "esp32-c3", "count": 1}]
    # Families named: the run takes one of each and leaves the bench free
    # for another; none named would take the whole bench.
    assert written["suite"] == {"path": "hil/tests", "min_boards": 1, "exclusive": False, "timeout_seconds": 1800}
    # And it is live: no restart between adding a project and running it.
    assert rig.profiles["my-sensor"].label == "My sensor firmware"
    assert rig.profiles["my-sensor"].runs_in_consumer_repo
    row = next(r for r in answer["projects"] if r["name"] == "my-sensor")
    assert row["shipped"] is False and row["needs"] == written["needs"]
    # It loads as any profile does, from the file alone.
    assert profiles.load_local_profiles(tmp_path / "state")["my-sensor"].suite_path == "hil/tests"


def test_what_is_refused_and_why(tmp_path):
    rig = _rig(tmp_path)
    with pytest.raises(ValueError, match="lowercase letters, digits and dashes"):
        rig.create_project({**MINE, "name": "My Sensor"})
    with pytest.raises(ValueError, match="https:// repository URL"):
        rig.create_project({**MINE, "repo": "git@github.com:example/my-sensor.git"})
    with pytest.raises(ValueError, match="not a chip family this rig knows"):
        rig.create_project({**MINE, "families": ["esp32", "stm32"]})
    with pytest.raises(ValueError, match="canary is already a project on this rig, shipped with it"):
        rig.create_project({**MINE, "name": "canary"})
    # What the profile schema refuses is refused here, by its own words.
    with pytest.raises(ValueError, match="supply.workflow must be a workflow path"):
        rig.create_project({**MINE, "supply_workflow": "hil.yml"})
    with pytest.raises(ValueError, match="suite.path must be a relative path"):
        rig.create_project({**MINE, "suite_path": "../elsewhere"})
    assert not (tmp_path / "state" / "profiles").exists(), "nothing refused was written"

    rig.create_project(MINE)
    with pytest.raises(ValueError, match="already a project on this rig; choose another"):
        rig.create_project(MINE)
    with pytest.raises(LookupError, match="no project named nope"):
        rig.update_project({"label": "x"}, "nope")
    with pytest.raises(ValueError, match="painlessmesh is shipped with the rig; it is not changed from here"):
        rig.delete_project(None, "painlessmesh")
    with pytest.raises(ValueError, match="shipped with the rig"):
        rig.update_project({**MINE}, "canary")
    assert "painlessmesh" in rig.profiles and "canary" in rig.profiles


def test_a_project_is_changed_and_removed_in_place(tmp_path):
    rig = _rig(tmp_path)
    rig.create_project(MINE)
    changed = rig.update_project({**MINE, "label": "Sensor v2", "default_ref": "release/2",
                                  "min_boards": 2, "timeout_seconds": 600, "families": []}, "my-sensor")
    assert changed["project"]["label"] == "Sensor v2" and changed["project"]["default_ref"] == "release/2"
    assert rig.profiles["my-sensor"].min_boards == 2 and rig.profiles["my-sensor"].suite_timeout == 600
    assert rig.profiles["my-sensor"].needs == () and rig.profiles["my-sensor"].exclusive is True
    gone = rig.delete_project(None, "my-sensor")
    assert gone["removed"] == "my-sensor"
    assert "my-sensor" not in rig.profiles
    assert not (tmp_path / "state" / "profiles" / "my-sensor.yaml").exists()
    assert {row["name"] for row in gone["projects"]} == set(profiles.load_profiles(REPO))


def test_a_document_the_service_could_not_start_on_is_taken_back(tmp_path, monkeypatch):
    """Between writing the file and reloading, the file is what the next
    start reads. If reloading refuses it, it is removed rather than left
    to stop the service coming up."""
    rig = _rig(tmp_path)
    real = rig.reload_profiles
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise profiles.ProfileError("pretend the disk lied")
        return real()

    monkeypatch.setattr(rig, "reload_profiles", flaky)
    with pytest.raises(profiles.ProfileError, match="disk lied"):
        rig.create_project(MINE)
    assert not (tmp_path / "state" / "profiles" / "my-sensor.yaml").exists()
    assert "my-sensor" not in rig.profiles


# ---- over HTTP: the rig's routes, and who may use them --------------------------------

def test_the_projects_routes_are_the_rigs_and_an_admin_key_manages_them(tmp_path):
    rig = _rig(tmp_path)
    server, call = _serve(rig, tmp_path)
    try:
        status, body = call("GET", "/api/v1/projects")
        assert status == 200 and {"canary", "painlessmesh"} <= {r["name"] for r in body["projects"]}
        status, body = call("POST", "/api/v1/projects", MINE)
        assert status == 200 and body["project"]["name"] == "my-sensor", body
        # The run form's picker reads the status: the new project is there.
        status, body = call("GET", "/api/v1/status")
        assert status == 200 and "my-sensor" in body["profiles"]
        status, body = call("POST", "/api/v1/projects/my-sensor", {**MINE, "label": "Renamed"})
        assert status == 200 and body["project"]["label"] == "Renamed"
        status, body = call("POST", "/api/v1/projects", {**MINE, "name": "Bad Name"})
        assert status == 400 and "lowercase" in body["error"]
        status, body = call("POST", "/api/v1/projects/canary/delete", {})
        assert status == 400 and "shipped with the rig" in body["error"]
        status, body = call("POST", "/api/v1/projects/nope/delete", {})
        assert status == 404
        status, body = call("POST", "/api/v1/projects/my-sensor/delete", {})
        assert status == 200 and body["removed"] == "my-sensor"
    finally:
        server.shutdown()


def test_a_user_key_reads_the_projects_and_changes_none(tmp_path):
    """Reading what the rig runs is any key's; adding to it is the rig's
    admin's, like every other change to what the host does."""
    rig = _rig(tmp_path)
    token = "t" * 40
    token_file = tmp_path / "api-token"
    token_file.write_text(token)
    entries, user_token = add_key([], "reader", "user")
    keys_path_for(token_file).write_text(keys_document(entries))
    server, call = _serve(rig, tmp_path, token=token, keys=KeyStore(token, keys_path_for(token_file)))
    try:
        status, body = call("GET", "/api/v1/projects", bearer=user_token)
        assert status == 200 and body["projects"]
        status, body = call("GET", "/api/v1/farm/public", bearer=user_token)
        assert status == 200 and "ok" in body
        status, body = call("POST", "/api/v1/projects", MINE, bearer=user_token)
        assert status == 403
        assert not (tmp_path / "state" / "profiles").exists()
    finally:
        server.shutdown()


def test_the_farm_and_the_portal_do_not_lose_each_others_routes(tmp_path):
    """A standalone farm is the rig's half and the portal's on one base; the
    rig's routes are declared cooperatively so the portal's still answer."""
    rig = _rig(tmp_path)
    paths = {(route.method, route.pattern.pattern) for route in rig.api_routes()}
    assert ("GET", r"/api/v1/projects") in paths
    if farm_service.MANAGERS.get("portal") is not None:
        assert ("GET", r"/api/v1/fleet") in paths, "the portal's routes, after the rig's"
    node = _rig(tmp_path, mode="node")
    assert ("POST", r"/api/v1/projects") in {(r.method, r.pattern.pattern) for r in node.api_routes()}


# ---- the farm a rig shows ------------------------------------------------------------

WORLD = {"rigs": [{"name": "esp32-hil", "description": "the painlessMesh rig", "location": "Quebec",
                   "online": True, "version": "1.0.162", "health": "ok", "boards": 7,
                   "families": {"esp32": 2, "esp32-c3": 1}, "runs": {"runs": 12, "passed": 11},
                   "setup": ["mqtt"], "secret": "never shown"}],
         "stats": {"rigs": 2, "online": 1, "boards": 11, "families": {"esp32": 4}, "runs": 20,
                   "passed": 18, "pass_rate": 0.9, "workspaces": 1, "shown": 1},
         "window_days": 7, "software": {"rig": {"version": "1.0.162"}}}


def test_a_rig_shows_the_public_farm_and_asks_it_rarely(tmp_path, monkeypatch):
    rig = _rig(tmp_path)
    reads = []

    def fake(url):
        reads.append(url)
        return _trim(WORLD)

    monkeypatch.delenv("ALTERIOM_HIL_FARM_PUBLIC_URL", raising=False)
    monkeypatch.setattr(rig, "_read_public_farm", fake)
    first = rig.farm_public_view()
    assert first["ok"] and first["url"] == "https://espfarm.alteriom.net", "the farm the rig software comes from"
    assert first["world"]["stats"]["rigs"] == 2 and first["world"]["rigs"][0]["name"] == "esp32-hil"
    assert "secret" not in first["world"]["rigs"][0], "only the fields the overview shows"
    second = rig.farm_public_view()
    assert second is first and reads == ["https://espfarm.alteriom.net"], "held, not re-read"

    # Pointed elsewhere, or off, from the host configuration.
    monkeypatch.setenv("ALTERIOM_HIL_FARM_PUBLIC_URL", "https://farm.example.org/")
    assert rig.farm_public_view()["url"] == "https://farm.example.org"
    monkeypatch.setenv("ALTERIOM_HIL_FARM_PUBLIC_URL", "off")
    assert rig.farm_public_view() == {"url": None, "ok": False, "error": None, "fetched_at": None, "world": None}


def _trim(world):
    keep = ("name", "description", "location", "online", "version", "health", "boards", "families", "runs", "setup")
    return {"rigs": [{k: r.get(k) for k in keep} for r in world["rigs"]], "stats": world["stats"],
            "window_days": world["window_days"], "software": world["software"]}


def test_a_farm_that_cannot_be_reached_is_said_so_with_the_last_good_answer(tmp_path, monkeypatch):
    rig = _rig(tmp_path)
    monkeypatch.delenv("ALTERIOM_HIL_FARM_PUBLIC_URL", raising=False)
    monkeypatch.setattr(rig, "_read_public_farm", lambda url: _trim(WORLD))
    good = rig.farm_public_view()
    assert good["ok"]
    rig._public_farm_cache["https://espfarm.alteriom.net"]["at"] = 0   # long enough ago
    monkeypatch.setattr(rig, "_read_public_farm", lambda url: (_ for _ in ()).throw(OSError("no route to host")))
    bad = rig.farm_public_view()
    assert bad["ok"] is False and "no route to host" in bad["error"]
    assert bad["world"] == good["world"], "what it last knew, marked stale by ok=false"
    # A node shows the portal it reports to, not the default.
    node = _rig(tmp_path, mode="node")
    monkeypatch.setenv("ALTERIOM_HIL_PORTAL_URL", "https://portal.example.org")
    assert node._public_farm_url() == "https://portal.example.org"


def test_the_public_farm_route_is_any_keys_read(tmp_path, monkeypatch):
    rig = _rig(tmp_path)
    monkeypatch.delenv("ALTERIOM_HIL_FARM_PUBLIC_URL", raising=False)
    monkeypatch.setattr(rig, "_read_public_farm", lambda url: _trim(WORLD))
    server, call = _serve(rig, tmp_path)
    try:
        status, body = call("GET", "/api/v1/farm/public")
        assert status == 200 and body["ok"] and body["world"]["stats"]["boards"] == 11
    finally:
        server.shutdown()


def test_the_host_configuration_names_the_farm_a_rig_shows(tmp_path):
    """`farm.public_url` is a setting like the rest: validated with the
    document, reaching the service through runtime.env, settable with
    `alteriom-hil-admin config set farm.public_url off`."""
    from alteriom_hil import hil_config

    def config(public_url):
        return {
            "schema": 2, "mode": "hardware",
            "runner": {"unit": "actions.runner.Alteriom.farm.service"},
            "paths": {"venv": str(tmp_path / "venv"), "board_map": str(tmp_path / "board-map.yaml")},
            "health": {"interval_minutes": 5, "minimum_boards": 2, "disk_warn_percent": 85, "disk_critical_percent": 95},
            "gateway": {"enabled": True, "ssid": "Alteriom-HIL", "password_file": str(tmp_path / "gateway-password"),
                        "endpoint": "http://10.42.0.1:8088", "channel": 1},
            "mqtt": {"enabled": True, "url": "mqtt://10.42.0.1:1883"},
            "service": {"enabled": True, "bind": "127.0.0.1", "port": 8090,
                        "token_file": str(tmp_path / "api-token"), "public_host": "hil.example.com"},
            "farm": {"mode": "standalone", "public_url": public_url},
        }

    for fine in ("https://farm.example.org", "off"):
        errors = hil_config.validate_config(config(fine))
        assert not [e for e in errors if "public_url" in e], errors
    errors = hil_config.validate_config(config("http://farm.example.org"))
    assert any("farm.public_url must be a farm's https:// URL, or off" in e for e in errors), errors
    assert "ALTERIOM_HIL_FARM_PUBLIC_URL=off" in hil_config.runtime_env(config("off"))
    assert "ALTERIOM_HIL_FARM_PUBLIC_URL" not in hil_config.runtime_env(config(None))
    assert hil_config.set_value(config("off"), "farm.public_url", "https://farm.example.org")["farm"]["public_url"] == "https://farm.example.org"
