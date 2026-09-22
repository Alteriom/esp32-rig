"""Fetching the bundle a consumer's CI built.

This is the farm reaching into another repository and writing what it finds
onto the host that drives the boards, so the checks here are the point of the
module rather than decoration: where a bundle may come from is the profile's
to say and never the dispatch's, the run must be the one for the commit under
validation, and nothing in the archive may land outside the bundle.

The farm service checks the bundle again when it is handed over -- origin,
every checksum, every component at its offset, the commit, the agent. These
are the earlier refusals, the ones that happen before a run reaches the rig
and whose messages name what is wrong.
"""

from __future__ import annotations

import importlib.util
import io
import json
import shutil
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "rig"))
sys.path.insert(0, str(REPO / "core"))

_SPEC = importlib.util.spec_from_file_location(
    "fetch_supplied_bundle", REPO / "runner" / "fetch_supplied_bundle.py"
)
fetch_supplied_bundle = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fetch_supplied_bundle)

CONSUMER = "Alteriom/alteriom-firmware"
COMMIT = "a" * 40
RUN = "34670859442"


@pytest.fixture()
def farm(tmp_path) -> Path:
    """A farm checkout with the real profiles, so the contract under test is
    the one that ships rather than a fixture's idea of it."""
    repo = tmp_path / "farm"
    (repo / "profiles").mkdir(parents=True)
    for document in (REPO / "profiles").glob("*.yaml"):
        shutil.copy2(document, repo / "profiles" / document.name)
    return repo


def bundle_zip(entries: dict[str, bytes] | None = None, manifest: dict | None = None) -> bytes:
    """A zip shaped like actions/upload-artifact's: the bundle's own contents
    at the top level, manifest.json beside the family directories."""
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        if manifest is not False:
            archive.writestr(
                "manifest.json",
                json.dumps(manifest if manifest is not None else {
                    "schema": 2, "git_sha": COMMIT,
                    "targets": {"esp32": {"image": "esp32/flash-image.bin"}},
                }),
            )
        archive.writestr("esp32/flash-image.bin", b"\xff" * 32)
        for name, body in (entries or {}).items():
            archive.writestr(name, body)
    return payload.getvalue()


def responses(run: dict | None = None, artifacts: list | None = None) -> dict:
    """What GitHub says, keyed by the API path each call uses."""
    return {
        f"/repos/{CONSUMER}/actions/runs/{RUN}": run if run is not None else {
            "repository": {"full_name": CONSUMER},
            "path": ".github/workflows/hil.yml",
            "head_sha": COMMIT,
            "html_url": f"https://github.com/{CONSUMER}/actions/runs/{RUN}",
        },
        f"/repos/{CONSUMER}/actions/runs/{RUN}/artifacts?per_page=100": {
            "artifacts": artifacts if artifacts is not None else [{
                "name": "hil-artifacts",
                "expired": False,
                "size_in_bytes": 4096,
                "archive_download_url": "https://api.github.com/zip",
            }],
        },
    }


def wire(monkeypatch, answers: dict, body: bytes | None = None):
    monkeypatch.setattr(
        fetch_supplied_bundle, "api_json",
        lambda path, token: answers[path],
    )
    monkeypatch.setattr(
        fetch_supplied_bundle, "download",
        lambda url, token: body if body is not None else bundle_zip(),
    )


def test_a_bundle_from_the_profiles_producer_at_the_commit_under_test_is_taken(farm, tmp_path, monkeypatch):
    wire(monkeypatch, responses())
    out = tmp_path / "supplied"
    found = fetch_supplied_bundle.fetch("alteriom-firmware", RUN, COMMIT, out, "t", farm)
    assert found["repo"] == f"https://github.com/{CONSUMER}"
    assert found["workflow"] == ".github/workflows/hil.yml"
    assert found["run_id"] == RUN and found["commit"] == COMMIT
    assert found["families"] == ["esp32"], "what the farm will be asked to flash"
    # The bundle's own contents at the top level, which is what load_artifacts
    # and the flasher read.
    assert (out / "manifest.json").is_file()
    assert (out / "esp32" / "flash-image.bin").read_bytes() == b"\xff" * 32


def test_a_run_of_another_workflow_or_repository_is_refused(farm, tmp_path, monkeypatch):
    """Where a bundle may come from is the profile's to say. A dispatch names
    only the run -- otherwise a run could point the farm at any repository's
    artifact and have it flashed onto the boards."""
    wire(monkeypatch, responses(run={
        "repository": {"full_name": CONSUMER},
        "path": ".github/workflows/release.yml",
        "head_sha": COMMIT,
    }))
    with pytest.raises(fetch_supplied_bundle.Refused, match="takes bundles from .github/workflows/hil.yml"):
        fetch_supplied_bundle.fetch("alteriom-firmware", RUN, COMMIT, tmp_path / "a", "t", farm)

    wire(monkeypatch, responses(run={
        "repository": {"full_name": "somebody/else"},
        "path": ".github/workflows/hil.yml",
        "head_sha": COMMIT,
    }))
    with pytest.raises(fetch_supplied_bundle.Refused, match="belongs to somebody/else"):
        fetch_supplied_bundle.fetch("alteriom-firmware", RUN, COMMIT, tmp_path / "b", "t", farm)


def test_a_bundle_built_from_another_commit_is_refused_twice_over(farm, tmp_path, monkeypatch):
    """A run reports the commit it built, and the manifest says it again.
    Both must be the commit under validation: flashing one commit's firmware
    under another's name is a passing report for code that never ran."""
    wire(monkeypatch, responses(run={
        "repository": {"full_name": CONSUMER},
        "path": ".github/workflows/hil.yml",
        "head_sha": "b" * 40,
    }))
    with pytest.raises(fetch_supplied_bundle.Refused, match="must not be flashed under this one's name"):
        fetch_supplied_bundle.fetch("alteriom-firmware", RUN, COMMIT, tmp_path / "a", "t", farm)

    # The run is right and the manifest disagrees: a build that resolved a
    # different revision than the run it ran in.
    wire(monkeypatch, responses(), body=bundle_zip(manifest={
        "schema": 2, "git_sha": "c" * 40, "targets": {"esp32": {}},
    }))
    with pytest.raises(fetch_supplied_bundle.Refused, match="manifest says it was built from"):
        fetch_supplied_bundle.fetch("alteriom-firmware", RUN, COMMIT, tmp_path / "b", "t", farm)


def test_a_missing_or_expired_artifact_says_which(farm, tmp_path, monkeypatch):
    wire(monkeypatch, responses(artifacts=[{"name": "hil-evidence-1"}, {"name": "firmware-unified-oled"}]))
    with pytest.raises(fetch_supplied_bundle.Refused, match="no artifact called 'hil-artifacts'"):
        fetch_supplied_bundle.fetch("alteriom-firmware", RUN, COMMIT, tmp_path / "a", "t", farm)

    wire(monkeypatch, responses(artifacts=[]))
    with pytest.raises(fetch_supplied_bundle.Refused, match="no artifacts at all"):
        fetch_supplied_bundle.fetch("alteriom-firmware", RUN, COMMIT, tmp_path / "b", "t", farm)

    wire(monkeypatch, responses(artifacts=[{"name": "hil-artifacts", "expired": True}]))
    with pytest.raises(fetch_supplied_bundle.Refused, match="has expired"):
        fetch_supplied_bundle.fetch("alteriom-firmware", RUN, COMMIT, tmp_path / "c", "t", farm)


def test_nothing_in_the_archive_lands_outside_the_bundle(farm, tmp_path, monkeypatch):
    """A consumer's CI is trusted to build firmware, not to write anywhere on
    the host that drives the boards. zipfile will happily write an absolute
    path or one full of `..` if asked."""
    for member in ("../escaped.bin", "/etc/cron.d/escaped", "esp32/../../escaped.bin"):
        wire(monkeypatch, responses(), body=bundle_zip({member: b"no"}))
        with pytest.raises(fetch_supplied_bundle.Refused, match="leaves the bundle"):
            fetch_supplied_bundle.fetch("alteriom-firmware", RUN, COMMIT, tmp_path / "a", "t", farm)
        assert not (tmp_path / "escaped.bin").exists()

    # A link is not a firmware image, and a link is how an archive reaches a
    # file it does not contain.
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"schema": 2, "git_sha": COMMIT, "targets": {}}))
        link = zipfile.ZipInfo("esp32/flash-image.bin")
        link.external_attr = (0xA1FF << 16)  # symlink, 0777
        archive.writestr(link, "/etc/passwd")
    wire(monkeypatch, responses(), body=payload.getvalue())
    with pytest.raises(fetch_supplied_bundle.Refused, match="holds a link"):
        fetch_supplied_bundle.fetch("alteriom-firmware", RUN, COMMIT, tmp_path / "b", "t", farm)


def test_an_upload_of_the_wrong_shape_says_what_to_change(farm, tmp_path, monkeypatch):
    """The commonest mistake, and one whose default message would be a
    checksum error deep in the loader: uploading the directory *containing*
    the bundle rather than the bundle."""
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("hil/manifest.json", json.dumps({"schema": 2}))
        archive.writestr("hil/esp32/flash-image.bin", b"\xff")
    wire(monkeypatch, responses(), body=payload.getvalue())
    with pytest.raises(fetch_supplied_bundle.Refused, match="no manifest.json at its top level"):
        fetch_supplied_bundle.fetch("alteriom-firmware", RUN, COMMIT, tmp_path / "a", "t", farm)


def test_a_profile_that_declares_no_producer_supplies_nothing(farm, tmp_path, monkeypatch):
    wire(monkeypatch, responses())
    # A profile without a producer cannot even be loaded -- the farm builds
    # nothing, so nothing could ever give it firmware -- so the producer is
    # taken off a loaded one. The fetch's own check must hold on its own.
    import dataclasses

    real = fetch_supplied_bundle.load_profiles

    def without_producer(repo):
        found = dict(real(repo))
        found["painlessmesh"] = dataclasses.replace(
            found["painlessmesh"], supply_repo=None, supply_workflow=None
        )
        return found

    monkeypatch.setattr(fetch_supplied_bundle, "load_profiles", without_producer)
    with pytest.raises(fetch_supplied_bundle.Refused, match="declares no producer"):
        fetch_supplied_bundle.fetch("painlessmesh", RUN, COMMIT, tmp_path / "a", "t", farm)
    with pytest.raises(fetch_supplied_bundle.Refused, match="has no 'nonesuch' profile"):
        fetch_supplied_bundle.fetch("nonesuch", RUN, COMMIT, tmp_path / "b", "t", farm)


def test_a_run_id_or_commit_that_is_not_one_is_refused_before_any_call(farm, tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("nothing should be asked of GitHub for a malformed request")

    monkeypatch.setattr(fetch_supplied_bundle, "api_json", refuse)
    with pytest.raises(fetch_supplied_bundle.Refused, match="must be numeric"):
        fetch_supplied_bundle.fetch("alteriom-firmware", "not-a-run", COMMIT, tmp_path / "a", "t", farm)
    with pytest.raises(fetch_supplied_bundle.Refused, match="40-character revision"):
        fetch_supplied_bundle.fetch("alteriom-firmware", RUN, "main", tmp_path / "b", "t", farm)


def test_the_client_hands_over_whose_run_built_the_bundle():
    """The producer is the consumer's run, not the farm job that handed the
    bundle over. The farm checks that claim against the profile, so a job
    describing itself would have every cross-repository supply refused."""
    import argparse

    _CLIENT = importlib.util.spec_from_file_location(
        "ci_farm_client", REPO / "runner" / "ci_farm_client.py"
    )
    client = importlib.util.module_from_spec(_CLIENT)
    _CLIENT.loader.exec_module(client)

    args = argparse.Namespace(
        profile="alteriom-firmware", ref=COMMIT,
        producer_repo=f"https://github.com/{CONSUMER}",
        producer_workflow=".github/workflows/hil.yml",
        producer_run=RUN,
        producer_run_url=f"https://github.com/{CONSUMER}/actions/runs/{RUN}",
    )
    fields = client.producer_fields(args)
    assert fields["repo"] == f"https://github.com/{CONSUMER}"
    assert fields["workflow"] == ".github/workflows/hil.yml"
    assert fields["run_id"] == RUN and fields["commit"] == COMMIT
    assert fields["run_url"].endswith(f"/runs/{RUN}")

    # Unstated, it is the running job: painlessMesh's pipeline builds and
    # hands over inside one workflow, and that is still the common case.
    import os

    os.environ.update({
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REPOSITORY": "Alteriom/alteriom-esp32-farm",
        "GITHUB_RUN_ID": "99",
        "GITHUB_WORKFLOW_REF": "Alteriom/alteriom-esp32-farm/.github/workflows/hil-painlessmesh.yml@refs/heads/main",
    })
    own = client.producer_fields(argparse.Namespace(
        profile="painlessmesh", ref=COMMIT, producer_repo=None,
        producer_workflow=None, producer_run=None, producer_run_url=None,
    ))
    assert own["repo"] == "https://github.com/Alteriom/alteriom-esp32-farm"
    assert own["workflow"] == ".github/workflows/hil-painlessmesh.yml"
    assert own["run_id"] == "99" and own["run_url"].endswith("/runs/99")


def test_the_token_is_not_sent_to_the_signed_storage_url(monkeypatch):
    """GitHub answers an artifact download with a 302 to a signed blob store.
    There the signature is the authorisation, and the store answers 401 for
    an Authorization header it did not expect -- urllib follows redirects on
    its own and re-sends every header, which is how the farm's first
    cross-repository supply failed with a 401 that read like a token problem.
    """
    from urllib.error import HTTPError

    seen = []

    def once(request):
        seen.append((request.full_url, dict(request.headers)))
        raise HTTPError(
            request.full_url, 302, "Found",
            {"Location": "https://blob.example/signed?sig=abc"}, None,
        )

    class Body:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, size=None): return b"PK the bundle"

    def follow(request):
        seen.append((request.full_url, dict(request.headers)))
        return Body()

    monkeypatch.setattr(fetch_supplied_bundle, "_open_once", once)
    monkeypatch.setattr(fetch_supplied_bundle, "_open", follow)

    body = fetch_supplied_bundle.download("https://api.github.com/zip", "s3cret")
    assert body == b"PK the bundle"
    assert [url for url, _ in seen] == [
        "https://api.github.com/zip", "https://blob.example/signed?sig=abc",
    ]
    api_headers, storage_headers = (headers for _, headers in seen)
    assert api_headers["Authorization"] == "Bearer s3cret", "the token opens the door"
    assert not any(
        name.lower() == "authorization" for name in storage_headers
    ), "and does not go through it: the signed URL refuses a token"

    # A 401 that is not a redirect still says what it was, and says the thing
    # worth checking first.
    def refuse(request):
        raise HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(fetch_supplied_bundle, "_open_once", refuse)
    with pytest.raises(fetch_supplied_bundle.Refused, match="signed storage URL"):
        fetch_supplied_bundle.download("https://api.github.com/zip", "s3cret")

    # And a redirect with nowhere to go is not silently a success.
    def nowhere(request):
        raise HTTPError(request.full_url, 302, "Found", {}, None)

    monkeypatch.setattr(fetch_supplied_bundle, "_open_once", nowhere)
    with pytest.raises(fetch_supplied_bundle.Refused, match="HTTP 302"):
        fetch_supplied_bundle.download("https://api.github.com/zip", "s3cret")


def test_a_bundle_and_a_run_say_whose_they_are_and_which_branch(monkeypatch):
    monkeypatch.delenv("GITHUB_TRIGGERING_ACTOR", raising=False)
    """A supplied bundle's page used to know its commit and nothing a person
    would recognise. The client hands over the branch and who started the
    run; for a consumer's bundle, whose run it was -- not this job's."""
    import argparse

    _CLIENT = importlib.util.spec_from_file_location(
        "ci_farm_client_whose", REPO / "runner" / "ci_farm_client.py"
    )
    client = importlib.util.module_from_spec(_CLIENT)
    _CLIENT.loader.exec_module(client)

    monkeypatch.setenv("GITHUB_ACTOR", "first-starter")
    monkeypatch.setenv("GITHUB_TRIGGERING_ACTOR", "re-runner")
    assert client.running_actor() == "re-runner", "a re-run is the re-runner's"
    monkeypatch.delenv("GITHUB_TRIGGERING_ACTOR")
    assert client.running_actor() == "first-starter"

    own = client.producer_fields(argparse.Namespace(
        profile="painlessmesh", ref=COMMIT, branch="fix/rejoin-459",
        producer_repo=None, producer_workflow=None, producer_run=None, producer_run_url=None,
    ))
    assert own["branch"] == "fix/rejoin-459" and own["actor"] == "first-starter"

    consumer = client.producer_fields(argparse.Namespace(
        profile="alteriom-firmware", ref=COMMIT, branch=None,
        producer_repo=f"https://github.com/{CONSUMER}", producer_workflow=".github/workflows/hil.yml",
        producer_run=RUN, producer_run_url=None,
        producer_branch="feature/x", producer_actor="consumer-dev",
    ))
    assert consumer["branch"] == "feature/x" and consumer["actor"] == "consumer-dev"

    body = client.build_request(argparse.Namespace(
        profile="painlessmesh", target=["esp32"], ref=COMMIT, branch="main",
        suite_env=None, simulation_evidence=None, actor=None,
    ))
    assert body["actor"] == "first-starter" and body["branch"] == "main"
