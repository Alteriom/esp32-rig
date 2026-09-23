"""A rig installs a release: `alteriom-hil-admin upgrade`.

Two things are held here. First `alteriom_hil.release`, the one description
of what a release is -- the portal checks a manifest against the files
published to it and a rig checks one against the files it downloaded, and
they must agree or a release means two things. Then the command itself: what
it installs, and everything it refuses to install
(docs/public-release-plan.md, step 13).

Nothing here runs pip. What `upgrade` does when the files are good is one
subprocess call, and it is checked by reading the call; what it does when
they are not is the interesting half, and that is checked all the way.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
import yaml

from alteriom_hil import admin_cli, release


# ---- a release, as a fixture makes one ---------------------------------------

FILES = {
    "alteriom_hil_core-1.0.7-py3-none-any.whl": b"PK core wheel",
    "alteriom_hil-1.0.7-py3-none-any.whl": b"PK rig wheel",
    "alteriom-hil-dashboard-1.0.7.tar.gz": None,  # a real tarball, made below
}
FIRMWARE = "alteriom-hil-canary-1.0.12.tar.gz"
COMMIT = "a" * 40


def _dashboard(payload: dict) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, body in payload.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


def _entry(name: str, body: bytes) -> dict:
    return {"name": name, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}


def _built(tmp_path: Path, dashboard: dict | None = None, commit: str = COMMIT,
           firmware: bool = False) -> Path:
    """A directory shaped like `build-release.sh --out`."""
    out = tmp_path / "dist"
    out.mkdir(parents=True, exist_ok=True)
    files = dict(FILES)
    if firmware:
        files[FIRMWARE] = b"\x1f\x8b the health check firmware"
    files["alteriom-hil-dashboard-1.0.7.tar.gz"] = _dashboard(
        dashboard if dashboard is not None else {"web/app.js": b"// the dashboard", "web/index.html": b"<html>"})
    for name, body in files.items():
        (out / name).write_bytes(body)
    manifest = {
        "schema": 1, "version": "1.0.7", "commit": commit,
        "packages": [_entry(name, files[name]) for name in files if name.endswith(".whl")],
        "dashboard": {**_entry("alteriom-hil-dashboard-1.0.7.tar.gz",
                               files["alteriom-hil-dashboard-1.0.7.tar.gz"]), "contract": 1},
    }
    if firmware:
        manifest["firmware"] = {**_entry(FIRMWARE, files[FIRMWARE]), "version": "1.0.12",
                                "revision": "d" * 64, "families": ["esp32", "esp32-c6"]}
    (out / "release.json").write_text(json.dumps(manifest), encoding="utf-8")
    return out


EXAMPLE = Path(__file__).resolve().parents[1] / "rig" / "hil-config.example.yaml"


def _here(value, tmp_path: Path):
    """Every absolute path in the shipped example, moved under tmp_path.

    The example is the real document a rig owner copies, so the test uses it
    rather than a hand-written subset -- but its paths are POSIX, and a POSIX
    path is not absolute on Windows, which the configuration insists on. This
    keeps the document and moves the paths.
    """
    if isinstance(value, dict):
        return {key: _here(item, tmp_path) for key, item in value.items()}
    if isinstance(value, list):
        return [_here(item, tmp_path) for item in value]
    if isinstance(value, str) and value.startswith("/"):
        return str(tmp_path / value.lstrip("/"))
    return value


def _config(tmp_path: Path) -> Path:
    """The shipped example, with this test's paths and a venv that exists."""
    document = _here(yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")), tmp_path)
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    (venv / "bin" / "python").write_text("#!/bin/sh", encoding="utf-8")  # only its existence is read
    document["paths"]["venv"] = str(venv)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def _args(tmp_path: Path, **over) -> argparse.Namespace:
    return argparse.Namespace(**{
        "config": _config(tmp_path), "source": None, "portal": None, "commit": None,
        "token_file": None, "web_root": None, "dry_run": False, **over})


@pytest.fixture
def pip(monkeypatch):
    """What pip was asked to do, without asking it."""
    calls = []

    class Done:
        returncode = 0
        stdout = stderr = ""

    monkeypatch.setattr(admin_cli.subprocess, "run",
                        lambda command, **kwargs: calls.append(command) or Done())
    return calls


# ---- the description ----------------------------------------------------------

def test_a_manifest_is_believed_only_when_it_says_something_checkable():
    good = json.dumps({"schema": 1, "version": "1.0.7", "commit": COMMIT,
                       "packages": [_entry("a-1.0.7-py3-none-any.whl", b"a")],
                       "dashboard": _entry("alteriom-hil-dashboard-1.0.7.tar.gz", b"d")}).encode()
    assert release.parse_manifest(good, COMMIT)["version"] == "1.0.7"
    with pytest.raises(release.ReleaseError, match="is of"):
        release.parse_manifest(good, "b" * 40)
    with pytest.raises(release.ReleaseError, match="not JSON"):
        release.parse_manifest(b"{")
    with pytest.raises(release.ReleaseError, match="schema-1"):
        release.parse_manifest(b'{"schema": 9}')
    with pytest.raises(release.ReleaseError, match="names no packages"):
        release.parse_manifest(b'{"schema": 1, "packages": [], "dashboard": {}}')
    # A name with a path in it is not a name.
    for bad in ("../etc/passwd", "a/b.whl", "notes.txt", ".hidden.whl"):
        body = json.dumps({"schema": 1, "packages": [{"name": bad, "sha256": "0" * 64, "bytes": 1}],
                           "dashboard": _entry("d-1.0.tar.gz", b"d")}).encode()
        with pytest.raises(release.ReleaseError, match="names a file it may not"):
            release.parse_manifest(body)
    # And an entry that says nothing checkable about itself.
    body = json.dumps({"schema": 1, "packages": [{"name": "a-1.0-py3-none-any.whl"}],
                       "dashboard": _entry("d-1.0.tar.gz", b"d")}).encode()
    with pytest.raises(release.ReleaseError, match="nothing checkable"):
        release.parse_manifest(body)


def test_a_release_may_carry_the_health_check_firmware_and_must_mean_it():
    """A release built without a toolchain is a release: the firmware is
    optional, and absent is not malformed. But a manifest that names one says
    which build and which boards, or a rig cannot tell one bundle from another
    (docs/public-release-plan.md, step 14b)."""
    base = {"schema": 1, "version": "1.0.7", "commit": COMMIT,
            "packages": [_entry("a-1.0.7-py3-none-any.whl", b"a")],
            "dashboard": _entry("alteriom-hil-dashboard-1.0.7.tar.gz", b"d")}
    # Absent: a release, and one that names no firmware.
    plain = release.parse_manifest(json.dumps(base).encode(), COMMIT)
    assert release.firmware(plain) is None
    assert len(release.entries(plain)) == 2

    good = {**_entry("alteriom-hil-canary-1.0.12.tar.gz", b"bundle"),
            "version": "1.0.12", "revision": "d" * 64, "families": ["esp32", "esp32-c6"]}
    carried = release.parse_manifest(json.dumps({**base, "firmware": good}).encode(), COMMIT)
    assert release.firmware(carried)["version"] == "1.0.12"
    # Named here is checked: `check` walks the firmware like any other file.
    assert [entry["name"] for entry in release.entries(carried)][-1] == good["name"]
    assert release.summary(carried)["firmware"]["families"] == ["esp32", "esp32-c6"]
    with pytest.raises(release.ReleaseError, match="is not the file release.json describes"):
        release.check(carried, {**{name: b"" for name in ("a-1.0.7-py3-none-any.whl",)},
                                good["name"]: b"tampered"}.get)

    for bad, says in (
        ({**good, "version": None}, "no version or no revision"),
        ({**good, "revision": None}, "no version or no revision"),
        ({**good, "families": []}, "for no board family"),
        ({**good, "families": "esp32"}, "for no board family"),
        ({**good, "name": "hil-canary/flash-image.bin"}, "names a file it may not"),
        ({**good, "sha256": None}, "nothing checkable"),
    ):
        with pytest.raises(release.ReleaseError, match=says):
            release.parse_manifest(json.dumps({**base, "firmware": bad}).encode(), COMMIT)
    with pytest.raises(release.ReleaseError, match="not a file it names"):
        release.parse_manifest(json.dumps({**base, "firmware": "yes"}).encode(), COMMIT)


def test_the_core_is_installed_before_the_rig_that_requires_it():
    manifest = {"packages": [{"name": "alteriom_hil-1.0.7-py3-none-any.whl"},
                             {"name": "alteriom_hil_core-1.0.7-py3-none-any.whl"}]}
    assert release.wheels(manifest)[0].startswith("alteriom_hil_core")


# ---- the command ---------------------------------------------------------------

def test_a_release_from_a_directory_is_checked_and_installed(tmp_path, pip, capsys):
    out = _built(tmp_path)
    args = _args(tmp_path, source=out, web_root=tmp_path / "web")
    assert admin_cli.command_upgrade(args) == 0
    said = capsys.readouterr().out
    assert "release 1.0.7 (aaaaaaaaaaaa)" in said

    # pip was given both wheels, from a staging directory and not the source,
    # core first, and nothing else.
    assert len(pip) == 1, pip
    command = pip[0]
    assert command[1:5] == ["-m", "pip", "install", "--quiet"]
    wheels = [Path(part).name for part in command if part.endswith(".whl")]
    assert wheels == ["alteriom_hil_core-1.0.7-py3-none-any.whl", "alteriom_hil-1.0.7-py3-none-any.whl"]
    assert not any(str(out) in part for part in command), "installed from a staging copy, not the source"

    # The dashboard bundle is unpacked, `web/` stripped.
    assert (tmp_path / "web" / "app.js").read_bytes() == b"// the dashboard"
    assert (tmp_path / "web" / "index.html").is_file()
    assert "restart" in said, "and the operator is told the service still runs the old code"


def test_the_firmware_a_release_carries_is_checked_and_named_and_not_installed(
        tmp_path, pip, capsys):
    """The release carries the health check firmware, so `upgrade` checks it
    with everything else and says it is there. It does not flash it, and it
    does not quietly hand it to pip: installing and pinning it is 13d, and
    until then the command says so rather than leaving a rig owner to guess
    (docs/public-release-plan.md, steps 14b and 13d)."""
    source = _built(tmp_path, firmware=True)
    assert admin_cli.command_upgrade(_args(tmp_path, source=source)) == 0
    said = capsys.readouterr().out
    assert FIRMWARE in said, "a file of the release is listed with the rest"
    assert "health check firmware 1.0.12" in said and "esp32, esp32-c6" in said
    assert "not installed yet" in said
    installed = [word for command in pip for word in command if word.endswith(".whl")]
    assert len(installed) == 2 and not any(FIRMWARE in word for command in pip for word in command)

    # And a release that disagrees about the firmware is refused whole, like
    # any other file: nothing is installed.
    (source / FIRMWARE).write_bytes(b"a different bundle")
    pip.clear()
    with pytest.raises(SystemExit, match="refusing to install"):
        admin_cli.command_upgrade(_args(tmp_path, source=source))
    assert pip == []


def test_a_dry_run_says_what_it_would_do_and_does_none_of_it(tmp_path, pip, capsys):
    args = _args(tmp_path, source=_built(tmp_path), web_root=tmp_path / "web", dry_run=True)
    assert admin_cli.command_upgrade(args) == 0
    assert "nothing installed" in capsys.readouterr().out
    assert not pip and not (tmp_path / "web").exists()


@pytest.mark.parametrize("break_it, says", [
    ("tamper", "size or digest differ"),
    ("remove", "which is not in"),
    ("empty", "holds no release.json"),
])
def test_a_release_that_is_not_what_it_says_is_not_installed(tmp_path, pip, break_it, says):
    out = _built(tmp_path)
    if break_it == "tamper":
        name = "alteriom_hil-1.0.7-py3-none-any.whl"
        (out / name).write_bytes((out / name).read_bytes() + b" tampered")
    elif break_it == "remove":
        (out / "alteriom_hil_core-1.0.7-py3-none-any.whl").unlink()
    else:
        for path in out.iterdir():
            path.unlink()
    with pytest.raises(SystemExit, match=says):
        admin_cli.command_upgrade(_args(tmp_path, source=out))
    assert not pip, "nothing is installed until every file is what the release says it is"


def test_a_dashboard_bundle_cannot_name_its_way_out_of_the_web_root(tmp_path, pip):
    out = _built(tmp_path, dashboard={"../../etc/cron.d/evil": b"pwned"})
    with pytest.raises(SystemExit, match="not inside web/"):
        admin_cli.command_upgrade(_args(tmp_path, source=out, web_root=tmp_path / "web"))
    assert not (tmp_path / "etc").exists() and not (tmp_path.parent / "etc").exists()


def test_a_pip_that_refuses_the_wheels_is_said_and_nothing_is_claimed(tmp_path, monkeypatch, capsys):
    class Refused:
        returncode = 1
        stdout = "could not install"
        stderr = "no matching distribution"

    monkeypatch.setattr(admin_cli.subprocess, "run", lambda *a, **k: Refused())
    with pytest.raises(SystemExit, match="pip refused"):
        admin_cli.command_upgrade(_args(tmp_path, source=_built(tmp_path)))


# ---- from a portal ---------------------------------------------------------------

def _portal(monkeypatch, out: Path, commit: str = COMMIT, current: dict | None = None):
    """The portal's answers, from a built directory. Returns the paths asked for."""
    asked = []

    class Answer(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=None):
        path = request.full_url.split("/api/v1", 1)[1]
        asked.append((path, request.headers.get("Authorization")))
        if path == "/releases/current":
            return Answer(json.dumps(current if current is not None else {"commit": commit}).encode())
        name = path.rsplit("/", 1)[1]
        return Answer((out / name).read_bytes())

    monkeypatch.setattr(admin_cli, "urlopen", fake_urlopen)
    return asked


def test_a_rig_takes_the_release_its_portal_names(tmp_path, pip, monkeypatch, capsys):
    out = _built(tmp_path)
    asked = _portal(monkeypatch, out)
    key = tmp_path / "node.key"
    key.write_text("k" * 40, encoding="utf-8")
    args = _args(tmp_path, portal="https://portal.example.org", token_file=str(key))
    assert admin_cli.command_upgrade(args) == 0
    assert "from https://portal.example.org" in capsys.readouterr().out
    # Which release, then the manifest, then every file it names -- each with
    # the node's key.
    assert [path for path, _ in asked][:2] == ["/releases/current", f"/releases/{COMMIT}/files/release.json"]
    assert {name for name, _ in asked} >= {f"/releases/{COMMIT}/files/{n}" for n in FILES}
    assert {header for _, header in asked} == {"Bearer " + "k" * 40}
    assert pip, "and it installed"


def test_a_portal_naming_no_release_is_said_rather_than_guessed(tmp_path, pip, monkeypatch):
    _portal(monkeypatch, _built(tmp_path), current={})
    key = tmp_path / "node.key"
    key.write_text("k" * 40, encoding="utf-8")
    with pytest.raises(SystemExit, match="named no current release"):
        admin_cli.command_upgrade(_args(tmp_path, portal="https://portal.example.org", token_file=str(key)))
    assert not pip


def test_a_manifest_of_another_release_is_refused_over_http(tmp_path, pip, monkeypatch):
    """The portal serves what it was given; a rig checks it anyway. A manifest
    of another commit is the likeliest way for the wrong files to be
    installed, and it costs one comparison."""
    out = _built(tmp_path, commit="b" * 40)
    _portal(monkeypatch, out, commit=COMMIT)
    key = tmp_path / "node.key"
    key.write_text("k" * 40, encoding="utf-8")
    with pytest.raises(SystemExit, match="is of bbbbbbbbbbbb"):
        admin_cli.command_upgrade(_args(tmp_path, portal="https://portal.example.org", token_file=str(key)))
    assert not pip


def test_with_no_portal_and_no_directory_it_says_so(tmp_path, pip):
    with pytest.raises(SystemExit, match="no --from directory and no portal"):
        admin_cli.command_upgrade(_args(tmp_path))
    assert not pip
