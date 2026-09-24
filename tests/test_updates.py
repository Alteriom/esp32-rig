"""Updates on the owner's terms: a rig says what is newer and installs it
when asked -- or on its own, when the owner turned that on -- through the
update unit every rig has. Never because a farm said so."""
from __future__ import annotations

import hashlib
import json
import sys
import time
import urllib.error
from pathlib import Path

import pytest

from alteriom_hil import launcher as farm_service
from alteriom_hil import release as release_document
from alteriom_hil import updates

REPO = Path(__file__).resolve().parents[1]


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _release(version="1.0.9", commit="a" * 40):
    """A release as GitHub would carry it: the files, and the document that
    names each by digest and size."""
    files = {
        f"alteriom_hil_core-{version}-py3-none-any.whl": b"core wheel " + version.encode(),
        f"alteriom_hil-{version}-py3-none-any.whl": b"rig wheel " + version.encode(),
        f"alteriom-hil-dashboard-{version}.tar.gz": b"dashboard " + version.encode(),
        "alteriom-hil-canary-1.0.7.tar.gz": b"firmware bytes",
    }
    entry = lambda name: {"name": name, "sha256": _digest(files[name]), "bytes": len(files[name])}
    manifest = {
        "schema": release_document.SCHEMA, "version": version, "commit": commit,
        "packages": [entry(f"alteriom_hil_core-{version}-py3-none-any.whl"), entry(f"alteriom_hil-{version}-py3-none-any.whl")],
        "dashboard": {**entry(f"alteriom-hil-dashboard-{version}.tar.gz"), "contract": 1},
        "firmware": {**entry("alteriom-hil-canary-1.0.7.tar.gz"), "version": "1.0.7", "revision": "b" * 64,
                     "families": ["esp32", "esp32-c3", "esp32-c5", "esp32-c6", "esp32-s3", "esp8266"]},
    }
    files["release.json"] = json.dumps(manifest).encode()
    base = f"https://github.com/Alteriom/esp32-rig/releases/download/v{version}/"
    available = {"version": version, "tag": f"v{version}", "published_at": "2026-09-24T20:00:00Z",
                 "html_url": f"https://github.com/Alteriom/esp32-rig/releases/tag/v{version}",
                 "assets": {name: base + name for name in files}, "source": updates.SOURCE}
    return available, files


def test_newer_is_a_release_this_host_does_not_run():
    assert updates.newer("1.0.174", "1.0.175") and not updates.newer("1.0.175", "1.0.175")
    assert not updates.newer("1.0.175", "1.0.174") and updates.newer("1.0.9", "1.1.0")
    assert updates.newer("unknown", "1.0.1"), "a host that cannot say what it runs is offered the newest"
    assert not updates.newer("1.0.1", "latest"), "and never something that is not a version"
    assert updates.version_tuple("v1.0.175") == (1, 0, 175) and updates.version_tuple("1.0") is None


def test_the_newest_release_is_read_from_githubs_answer(monkeypatch):
    seen = []

    def ask(url, *, headers=None, timeout=15.0):
        seen.append((url, dict(headers or {})))
        return {"tag_name": "v1.0.175", "name": "1.0.175", "published_at": "2026-09-24T21:00:00Z",
                "html_url": "https://github.com/Alteriom/esp32-rig/releases/tag/v1.0.175",
                "assets": [{"name": "release.json", "browser_download_url": "https://example.test/release.json"},
                           {"name": "SHA256SUMS", "browser_download_url": "https://example.test/SHA256SUMS"}]}
    latest = updates.latest_release(ask=ask)
    assert latest["version"] == "1.0.175" and latest["tag"] == "v1.0.175"
    assert latest["assets"]["release.json"] == "https://example.test/release.json"
    assert seen[0][0] == "https://api.github.com/repos/Alteriom/esp32-rig/releases/latest"
    assert "Authorization" not in seen[0][1], "a public repository: asked without a token when the rig has none"
    updates.latest_release(token="github_pat_x", ask=ask)
    assert seen[1][1]["Authorization"] == "Bearer github_pat_x", "and with the rig's when it has one"
    with pytest.raises(updates.UpdateError, match="not a github.com repository"):
        updates.latest_release(source="https://example.org/x", ask=ask)

    def refused(url, *, headers=None, timeout=15.0):
        raise urllib.error.HTTPError(url, 403, "rate limited", {}, None)
    with pytest.raises(updates.UpdateError, match="rate limit"):
        updates.latest_release(ask=refused)
    with pytest.raises(updates.UpdateError, match="not a version"):
        updates.latest_release(ask=lambda url, *, headers=None, timeout=15.0: {"tag_name": "nightly"})


def test_staging_fetches_the_document_first_checks_every_file_and_then_asks_for_the_install(tmp_path):
    available, files = _release()
    fetched = []

    def fetch(url):
        name = url.rsplit("/", 1)[-1]
        fetched.append(name)
        return files[name]
    update_dir = tmp_path / "update"
    request = updates.stage(update_dir, available, fetch=fetch)
    assert fetched[0] == "release.json", "the document first: it says what else to fetch"
    assert set(fetched) == set(files)
    target = update_dir / "release-1.0.9"
    assert request == {"version": "1.0.9", "commit": "a" * 40, "release_dir": str(target), "requested_at": request["requested_at"]}
    assert json.loads((update_dir / "request.json").read_text()) == request
    assert (target / "alteriom_hil-1.0.9-py3-none-any.whl").read_bytes() == files["alteriom_hil-1.0.9-py3-none-any.whl"]
    status = updates.status(update_dir)
    assert status["state"] == "staged" and status["version"] == "1.0.9" and "alteriom-hil-update" in status["detail"]

    # A file that is not what the document says stops everything before the request exists.
    wrong = dict(files)
    wrong["alteriom_hil-1.0.9-py3-none-any.whl"] = b"not the wheel the document names"
    other = tmp_path / "other"
    with pytest.raises(updates.UpdateError, match="refusing to install"):
        updates.stage(other, available, fetch=lambda url: wrong[url.rsplit("/", 1)[-1]])
    assert not (other / "request.json").exists()
    # And a release without a document is not a rig release.
    with pytest.raises(updates.UpdateError, match="no release.json"):
        updates.stage(tmp_path / "bare", {**available, "assets": {}}, fetch=fetch)


def test_the_owners_choice_about_automatic_installs_is_kept_in_the_rigs_state(tmp_path):
    assert updates.settings(tmp_path) == {"auto": False}, "off until the owner turns it on"
    assert updates.set_auto(tmp_path, True) == {"auto": True}
    assert updates.settings(tmp_path)["auto"] is True
    assert updates.set_auto(tmp_path, False)["auto"] is False


def _rig(tmp_path, monkeypatch, installed="1.0.1"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    stamp = tmp_path / "version.json"
    stamp.write_text(json.dumps({"version": installed, "short": installed, "commit": "0" * 40}), encoding="utf-8")
    monkeypatch.setenv("ALTERIOM_HIL_VERSION_FILE", str(stamp))
    from alteriom_hil import service
    monkeypatch.setattr(service, "VERSION_FILE", stamp)
    monkeypatch.setenv("ALTERIOM_HIL_UPDATE_WATCH", "0")
    rig = farm_service.manager_for("standalone")(
        REPO, tmp_path / "state", tmp_path / "none.yaml", tmp_path / "none-map.yaml",
        Path(sys.executable), mode="standalone")
    return rig


def test_a_rig_says_what_is_newer_installs_it_when_asked_and_only_then(tmp_path, monkeypatch):
    """GET /api/v1/update says what is installed and what is newer; Install
    stages the release for the update unit; nothing happens until asked."""
    rig = _rig(tmp_path, monkeypatch)
    available, files = _release("1.0.9")
    asked = []
    rig._latest_release = lambda: asked.append(1) or dict(available)
    rig._fetch_release_file = lambda url: files[url.rsplit("/", 1)[-1]]

    view = rig.update_view()
    assert view["installed"]["version"] == "1.0.1" and view["source"] == "github"
    assert view["available"] is None and view["checked_at"] is None and view["auto"] is False and not asked, "nothing asked yet"

    view = rig.check_update({})["update"]
    assert asked == [1] and view["available"]["version"] == "1.0.9" and view["checked_at"]
    assert rig.update_view()["available"]["version"] == "1.0.9", "kept"
    assert not (tmp_path / "state" / "update" / "request.json").exists(), "seen, not installed"

    view = rig.install_update({})["update"]
    assert view["staging"] or (view["status"] or {}).get("state") in ("downloading", "staged")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not (tmp_path / "state" / "update" / "request.json").exists():
        time.sleep(0.05)
    request = json.loads((tmp_path / "state" / "update" / "request.json").read_text())
    assert request["version"] == "1.0.9" and request["release_dir"].endswith("release-1.0.9")
    assert rig.update_view()["status"]["state"] == "staged"

    # Up to date: nothing to install, and Install says so.
    current = _rig(tmp_path / "current", monkeypatch, installed="1.0.9")
    current._latest_release = lambda: dict(available)
    assert current.check_update({})["update"]["available"] is None
    with pytest.raises(ValueError, match="no newer release"):
        current.install_update({})
    # GitHub unreachable: said, not raised.
    dark = _rig(tmp_path / "dark", monkeypatch)
    dark._latest_release = lambda: (_ for _ in ()).throw(updates.UpdateError("GitHub could not be reached: timeout"))
    view = dark.check_update({})["update"]
    assert view["available"] is None and "could not be reached" in view["error"]
    with pytest.raises(ValueError, match="could not be reached"):
        dark.install_update({})


def test_automatic_installs_are_the_owners_setting_and_the_watch_obeys_it(tmp_path, monkeypatch):
    rig = _rig(tmp_path, monkeypatch)
    available, files = _release("1.0.9")
    rig._latest_release = lambda: dict(available)
    rig._fetch_release_file = lambda url: files[url.rsplit("/", 1)[-1]]
    with pytest.raises(ValueError, match="true or false"):
        rig.set_update_auto({"auto": "yes"})
    assert rig.set_update_auto({"auto": True})["update"]["auto"] is True
    assert rig.update_auto() is True
    # One turn of the watch, with automatic installs on and the rig idle: staged.
    monkeypatch.setattr(rig, "UPDATE_CHECK_DELAY", 0)
    staged = []
    monkeypatch.setattr(rig, "_stage_update", lambda avail: staged.append(avail["version"]))
    # One turn: the watch sleeps, looks, stages; the next sleep ends the test.
    class Enough(Exception):
        pass

    def stop_once_staged(seconds):
        if staged:
            raise Enough
    monkeypatch.setattr(updates.time, "sleep", stop_once_staged)
    with pytest.raises(Enough):
        rig._update_watch()
    assert staged == ["1.0.9"]
    # Off: the same turn stages nothing.
    rig.set_update_auto({"auto": False})
    staged.clear()
    rig.__dict__.pop("_update_checked", None)
    calls = {"n": 0}

    def sleep(seconds):
        calls["n"] += 1
        if calls["n"] > 1:
            raise Enough
    monkeypatch.setattr(updates.time, "sleep", sleep)
    with pytest.raises(Enough):
        rig._update_watch()
    assert staged == [] and rig.update_view()["available"]["version"] == "1.0.9", "seen and offered, not installed"


def test_the_update_routes_are_a_persons_and_the_installs_an_admins(tmp_path, monkeypatch):
    rig = _rig(tmp_path, monkeypatch)
    routes = {(route.method, route.pattern.pattern): route.audience for route in rig.api_routes()}
    assert routes[("GET", r"/api/v1/update")] == "user"
    for path in (r"/api/v1/update/check", r"/api/v1/update/install", r"/api/v1/update/auto"):
        assert routes[("POST", path)] == "admin", path
