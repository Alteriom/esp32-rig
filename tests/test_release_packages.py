"""A release is packages, published beside the bundle a portal names today.

`runner/ci/build-release.sh` makes the files; the portal keeps them next to
the bundle and, once `release.json` has been checked against them, the
record carries them -- to the index, to the heartbeat, to a node that knows
how to install from them (docs/public-release-plan.md, step 13). A node that
does not still takes the bundle from the same record: nothing here is taken
away.

The portal and node fixture is test_portal_node.py's; this file adds the
packages to it rather than another four thousand lines.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request

import pytest

# `farm` is the portal-and-node fixture, and it must be imported under that
# name: pytest registers a fixture by the name it finds it under. The test
# parameters shadow it, which is the shape pytest asks for.
from test_portal_node import TOKEN, _release, farm  # noqa: F401


def _wheel(name: str, payload: bytes) -> tuple[str, bytes]:
    return name, payload


FIRMWARE = "alteriom-hil-canary-1.0.12.tar.gz"


def _manifest(commit: str, files: dict, dashboard: str, contract: int = 1) -> bytes:
    def entry(name):
        return {"name": name, "sha256": hashlib.sha256(files[name]).hexdigest(), "bytes": len(files[name])}
    document = {
        "schema": 1, "version": "1.0.7", "commit": commit,
        "packages": [entry(name) for name in files if name not in (dashboard, FIRMWARE)],
        "dashboard": {**entry(dashboard), "contract": contract},
    }
    if FIRMWARE in files:
        document["firmware"] = {**entry(FIRMWARE), "version": "1.0.12", "revision": "d" * 64,
                                "families": ["esp32", "esp32-c6"]}
    return json.dumps(document).encode("utf-8")


def _publish_bundle(farm, tmp_path, commits: int = 1):
    # `commits` makes the release a distinct commit: two one-commit
    # repositories with the same file and message made in the same second
    # are the same SHA, and a "newer" release that is the older one prunes
    # nothing.
    _, commit, body = _release(tmp_path, commits=commits)
    farm.portal.publish_release(commit, body, "ci")
    return commit


def _files(commit: str) -> dict:
    return {
        "alteriom_hil_core-1.0.7-py3-none-any.whl": b"PK core wheel " + commit[:8].encode(),
        "alteriom_hil-1.0.7-py3-none-any.whl": b"PK rig wheel " + commit[:8].encode(),
        "alteriom-hil-dashboard-1.0.7.tar.gz": b"\x1f\x8b dashboard " + commit[:8].encode(),
    }


def test_a_releases_packages_are_kept_beside_its_bundle_and_sealed_by_the_manifest(farm, tmp_path):
    """Files first, in any order; release.json last. Until the manifest has
    been checked against them the record says nothing about packages, so a
    node never sees a half-published release."""
    commit = _publish_bundle(farm, tmp_path)
    files = _files(commit)
    for name, payload in files.items():
        result = farm.portal.attach_release_file(commit, name, payload, "ci")
        assert result["sha256"] == hashlib.sha256(payload).hexdigest()
        assert result["packages"] is None, "no manifest yet, no packages yet"
    assert "packages" not in (farm.portal.current_release() or {})

    manifest = _manifest(commit, files, "alteriom-hil-dashboard-1.0.7.tar.gz")
    sealed = farm.portal.attach_release_file(commit, "release.json", manifest, "ci")
    assert [p["name"] for p in sealed["packages"]] == [
        "alteriom_hil_core-1.0.7-py3-none-any.whl", "alteriom_hil-1.0.7-py3-none-any.whl"]
    assert sealed["dashboard"]["contract"] == 1

    # The record carries them from here: the index, and what a node is told.
    current = farm.portal.current_release()
    assert current["commit"] == commit
    assert {p["name"] for p in current["packages"]} == set(files) - {"alteriom-hil-dashboard-1.0.7.tar.gz"}
    assert current["dashboard"]["name"] == "alteriom-hil-dashboard-1.0.7.tar.gz"
    assert current["sha256"] and current["bytes"], "and the bundle is still named, for a node that takes that"
    assert farm.portal.release_index()["releases"][0]["packages"] == current["packages"]


def test_the_manifest_is_checked_against_the_files_and_refuses_what_disagrees(farm, tmp_path):
    commit = _publish_bundle(farm, tmp_path)
    files = _files(commit)
    dashboard = "alteriom-hil-dashboard-1.0.7.tar.gz"

    # Before any file: the manifest names files that are not there.
    with pytest.raises(ValueError, match="files first, the manifest last"):
        farm.portal.attach_release_file(commit, "release.json", _manifest(commit, files, dashboard), "ci")
    for name, payload in files.items():
        farm.portal.attach_release_file(commit, name, payload, "ci")

    # A manifest of another commit, of another schema, naming a path.
    with pytest.raises(ValueError, match="is of"):
        farm.portal.attach_release_file(commit, "release.json", _manifest("e" * 40, files, dashboard), "ci")
    with pytest.raises(ValueError, match="schema-1"):
        farm.portal.attach_release_file(commit, "release.json", b'{"schema": 2}', "ci")
    with pytest.raises(ValueError, match="not JSON"):
        farm.portal.attach_release_file(commit, "release.json", b"{", "ci")

    # A file that is not what the manifest says it is.
    wrong = dict(files)
    wrong["alteriom_hil-1.0.7-py3-none-any.whl"] = b"PK a different wheel"
    with pytest.raises(ValueError, match="size or digest differ"):
        farm.portal.attach_release_file(commit, "release.json", _manifest(commit, wrong, dashboard), "ci")
    assert "packages" not in farm.portal.current_release(), "nothing was sealed by a refused manifest"

    # A name that is not a release file.
    for bad in ("../etc/passwd", ".hidden.whl", "notes.txt", "a/b.whl", ""):
        with pytest.raises(ValueError, match="plain name"):
            farm.portal.attach_release_file(commit, bad, b"x", "ci")
    # A release that has not been published.
    with pytest.raises(LookupError, match="publish its bundle first"):
        farm.portal.attach_release_file("f" * 40, "release.json", b"{}", "ci")
    # And only a portal keeps releases.
    with pytest.raises(Exception, match="not a portal"):
        farm.node.attach_release_file(commit, "release.json", b"{}", "ci")


def test_a_node_fetches_a_releases_files_with_its_key_and_a_person_publishes_them(farm, tmp_path):
    """Over HTTP, with the roles the tables give: a person with the farm's
    key publishes; a node reads what it was told to run and nothing else."""
    commit = _publish_bundle(farm, tmp_path)
    files = _files(commit)
    dashboard = "alteriom-hil-dashboard-1.0.7.tar.gz"
    for name, payload in files.items():
        status, answer = farm.call("POST", f"/api/v1/releases/{commit}/files/{name}", TOKEN, payload)
        assert status == 201 and answer["name"] == name, answer
    status, answer = farm.call("POST", f"/api/v1/releases/{commit}/files/release.json", TOKEN,
                               _manifest(commit, files, dashboard))
    assert status == 201 and len(answer["packages"]) == 2, answer

    # The node reads a package by its key, and gets exactly the bytes.
    request = urllib.request.Request(farm.url + f"/api/v1/releases/{commit}/files/alteriom_hil-1.0.7-py3-none-any.whl",
                                     headers={"Authorization": f"Bearer {farm.node_key}"})
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.read() == files["alteriom_hil-1.0.7-py3-none-any.whl"]
        assert response.headers["Content-Type"] == "application/octet-stream"
    request = urllib.request.Request(farm.url + f"/api/v1/releases/{commit}/files/release.json",
                                     headers={"Authorization": f"Bearer {farm.node_key}"})
    with urllib.request.urlopen(request, timeout=10) as response:
        assert json.loads(response.read())["commit"] == commit
        assert response.headers["Content-Type"] == "application/json"

    # A node does not publish.
    status, _ = farm.call("POST", f"/api/v1/releases/{commit}/files/SHA256SUMS", farm.node_key, b"sums")
    assert status == 403
    # A file that is not there is not there, whoever asks.
    request = urllib.request.Request(farm.url + f"/api/v1/releases/{commit}/files/SHA256SUMS",
                                     headers={"Authorization": f"Bearer {farm.node_key}"})
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(request, timeout=10)
    assert refused.value.code == 404
    # A file name with a path in it never reaches the store.
    status, _ = farm.call("POST", f"/api/v1/releases/{commit}/files/..%2Frelease.json", TOKEN, b"x")
    assert status in (400, 404)


def test_pruning_a_release_takes_its_files_with_it(farm, tmp_path, monkeypatch):
    from alteriom_hil import portal_manager

    monkeypatch.setattr(portal_manager, "RELEASES_KEPT", 1)
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    older = _publish_bundle(farm, tmp_path / "one")
    files = _files(older)
    for name, payload in files.items():
        farm.portal.attach_release_file(older, name, payload, "ci")
    farm.portal.attach_release_file(older, "release.json",
                                    _manifest(older, files, "alteriom-hil-dashboard-1.0.7.tar.gz"), "ci")
    assert farm.portal.release_files_dir(older).is_dir()
    # A newer release, made current: the older one and its files go.
    newer = _publish_bundle(farm, tmp_path / "two", commits=2)
    assert newer != older
    assert farm.portal.current_release()["commit"] == newer
    assert not farm.portal.release_files_dir(older).exists(), "the files went with the record"
    with pytest.raises(LookupError):
        farm.portal.release_file_path(older, "release.json")


def test_a_release_that_carries_the_firmware_says_so_to_a_node(farm, tmp_path):
    """The health check firmware is a file of the release like any other: it
    is published beside the wheels, checked by the same manifest, and named in
    the record a node reads -- so a rig can install the firmware that release
    was built with (docs/public-release-plan.md, step 14b)."""
    commit = _publish_bundle(farm, tmp_path)
    files = {**_files(commit), FIRMWARE: b"\x1f\x8b canary " + commit[:8].encode()}
    for name, payload in files.items():
        farm.portal.attach_release_file(commit, name, payload, "ci")
    sealed = farm.portal.attach_release_file(
        commit, "release.json", _manifest(commit, files, "alteriom-hil-dashboard-1.0.7.tar.gz"), "ci")
    assert sealed["firmware"]["name"] == FIRMWARE
    assert sealed["firmware"]["families"] == ["esp32", "esp32-c6"]

    current = farm.portal.current_release()
    assert current["firmware"]["version"] == "1.0.12"
    assert current["firmware"]["revision"] == "d" * 64

    # And a node fetches it with its key, like every other file of a release.
    request = urllib.request.Request(
        farm.url + f"/api/v1/releases/{commit}/files/{FIRMWARE}",
        headers={"Authorization": f"Bearer {farm.node_key}"})
    with urllib.request.urlopen(request, timeout=10) as answer:
        assert answer.read() == files[FIRMWARE]


def test_a_release_without_the_firmware_claims_none(farm, tmp_path):
    """A build on a host with no toolchain makes wheels, and that is a
    release. The record must not grow a firmware key out of nothing."""
    commit = _publish_bundle(farm, tmp_path)
    files = _files(commit)
    for name, payload in files.items():
        farm.portal.attach_release_file(commit, name, payload, "ci")
    sealed = farm.portal.attach_release_file(
        commit, "release.json", _manifest(commit, files, "alteriom-hil-dashboard-1.0.7.tar.gz"), "ci")
    assert sealed["firmware"] is None
    assert "firmware" not in farm.portal.current_release()
