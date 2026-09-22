"""The artifact store's filesystem half: find bundles, hand them back, let them go.

Everything here deletes or serves files on the farm host, so the tests are
about the edges: what counts as part of a bundle, what a reuse link is, and
that nothing outside a bundle can be reached, archived or removed through it.
"""

import io
import json
import os
import tarfile
from datetime import datetime, timedelta, timezone

import pytest

from alteriom_hil import artifact_store
from alteriom_hil.artifacts import load_artifacts, sha256

A = "a" * 32
B = "b" * 32
C = "c" * 32
D = "d" * 32


def make_bundle(root, bundle_id, sha="1" * 40, families=("esp32",)):
    """A schema-2 bundle a flasher would accept: a bootloader component
    placed at its offset inside the merged image, both checksummed."""
    path = root / bundle_id
    targets = {}
    for family in families:
        folder = path / family
        folder.mkdir(parents=True)
        boot = b"\xe9boot-" + family.encode()
        (folder / "bootloader.bin").write_bytes(boot)
        (folder / "flash-image.bin").write_bytes(b"\xff" * 0x1000 + boot)
        targets[family] = {
            "image": f"{family}/flash-image.bin",
            "sha256": sha256(folder / "flash-image.bin"),
            "flash_offset": "0x0",
            "segments": {"bootloader.bin": "0x1000"},
            "files": {"bootloader.bin": {"sha256": sha256(folder / "bootloader.bin")}},
        }
    (path / "manifest.json").write_text(json.dumps({"schema": 2, "painlessmesh_sha": sha, "targets": targets}))
    return path


def test_a_scan_finds_bundles_reuse_links_and_links_to_nothing(tmp_path):
    root = tmp_path / "artifacts"
    make_bundle(root, A)
    make_bundle(root, B, families=("esp32", "esp32-c3"))
    os.symlink(root / A, root / C, target_is_directory=True)  # a run that reused A
    os.symlink(root / ("e" * 32), root / D)  # its bundle is gone
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    os.symlink(outside, root / ("f" * 32), target_is_directory=True)
    (root / "notes.txt").write_text("not a bundle\n")
    (root / "tmp").mkdir()

    found = artifact_store.scan(root)
    assert sorted(found.bundles) == [A, B]
    assert found.links == {C: A}
    assert found.dangling == [D]
    assert sorted(found.bundles[B].manifest["targets"]) == ["esp32", "esp32-c3"]
    assert found.bundles[A].bytes == sum(found.bundles[A].files.values()) > 0


def test_only_what_a_bundle_holds_is_counted_or_served(tmp_path):
    root = tmp_path / "artifacts"
    bundle_dir = make_bundle(root, A)
    # Nested files are the bundle's too: the manifest loader accepts an image
    # at any relative path, so the store cannot assume <family>/<file>.
    (bundle_dir / "esp32" / "deeper").mkdir()
    (bundle_dir / "esp32" / "deeper" / "x.bin").write_bytes(b"x" * 99)
    too_deep = bundle_dir / "a" / "b" / "c" / "d" / "e"
    too_deep.mkdir(parents=True)
    (too_deep / "lost.bin").write_bytes(b"x")
    (bundle_dir / ".hidden").write_text("no")
    secret = tmp_path / "token"
    secret.write_text("s3cret")
    os.symlink(secret, bundle_dir / "esp32" / "linked.bin")
    os.symlink(tmp_path, bundle_dir / "escape", target_is_directory=True)

    bundle = artifact_store.load_bundle(root, A)
    assert sorted(bundle.files) == [
        "esp32/bootloader.bin", "esp32/deeper/x.bin", "esp32/flash-image.bin", "manifest.json",
    ]
    assert artifact_store.file_path(bundle, "esp32/flash-image.bin") == bundle_dir / "esp32" / "flash-image.bin"
    for spelled in ("../token", "esp32/../manifest.json", "esp32/linked.bin", "escape/token", ".hidden", "a/b/c/d/e/lost.bin"):
        with pytest.raises(LookupError):
            artifact_store.file_path(bundle, spelled)


def test_an_image_the_loader_accepts_anywhere_is_in_the_archive(tmp_path):
    """load_artifacts takes an image at any relative path; a downloaded
    bundle must carry it wherever it is, or it is not flashable."""
    root = tmp_path / "artifacts"
    path = root / A
    (path / "images" / "esp32").mkdir(parents=True)
    (path / "images" / "esp32" / "flash.bin").write_bytes(b"\xe9image")
    target = {"image": "images/esp32/flash.bin", "sha256": sha256(path / "images" / "esp32" / "flash.bin")}
    (path / "manifest.json").write_text(json.dumps({"schema": 2, "git_sha": "1" * 40, "targets": {"esp32": target}}))
    data = artifact_store.archive(artifact_store.load_bundle(root, A), "bundle")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        safe = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
        tar.extractall(tmp_path / "out", **safe)
    assert sorted(load_artifacts(tmp_path / "out" / "bundle")["targets"]) == ["esp32"]


def test_every_file_the_manifest_has_the_flasher_read_is_held_wherever_it_is(tmp_path):
    """The loader accepts an image under a hidden directory, deeper than a
    scan goes, or with a component longer than a scan's plain names. Each is
    the bundle's all the same: listed, counted, and in the archive, which is
    then still a directory the loader accepts."""
    root = tmp_path / "artifacts"
    path = root / A
    long_name = "esp32-" + "x" * 80 + ".bin"
    image = f".build/a/b/c/d/e/{long_name}"
    (path / ".build" / "a" / "b" / "c" / "d" / "e").mkdir(parents=True)
    (path / image).write_bytes(b"\xff" * 0x1000 + b"\xe9boot")
    (path / "esp32").mkdir()
    (path / "esp32" / "bootloader.bin").write_bytes(b"\xe9boot")
    (path / ".ota").mkdir()
    (path / ".ota" / "next.bin").write_bytes(b"\xe9next")
    target = {
        "image": image, "sha256": sha256(path / image),
        "segments": {"bootloader.bin": "0x1000"},
        "files": {"bootloader.bin": {"sha256": sha256(path / "esp32" / "bootloader.bin")}},
        "ota": {"image": ".ota/next.bin", "sha256": sha256(path / ".ota" / "next.bin")},
    }
    (path / "manifest.json").write_text(json.dumps({"schema": 2, "git_sha": "1" * 40, "targets": {"esp32": target}}))
    bundle = artifact_store.load_bundle(root, A)
    assert {image, "esp32/bootloader.bin", ".ota/next.bin", "manifest.json"} <= set(bundle.files)
    assert artifact_store.file_path(bundle, image).read_bytes().endswith(b"\xe9boot")
    data = artifact_store.archive(bundle, "bundle")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        safe = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
        tar.extractall(tmp_path / "out", **safe)
    loaded = load_artifacts(tmp_path / "out" / "bundle")
    assert loaded["targets"]["esp32"]["ota"]["path"].is_file()


def test_a_manifest_path_through_a_directory_still_resolves_once_extracted(tmp_path):
    """`spare/../firmware.bin` is a path load_artifacts accepts when `spare`
    exists. The file is held as `firmware.bin`; the archive brings `spare/`
    along, so the manifest -- unchanged -- validates after extraction."""
    root = tmp_path / "artifacts"
    path = root / A
    (path / "spare").mkdir(parents=True)
    (path / "firmware.bin").write_bytes(b"\xe9image")
    target = {"image": "spare/../firmware.bin", "sha256": sha256(path / "firmware.bin")}
    (path / "manifest.json").write_text(json.dumps({"schema": 2, "git_sha": "1" * 40, "targets": {"esp32": target}}))
    bundle = artifact_store.load_bundle(root, A)
    assert "firmware.bin" in bundle.files
    assert [member["name"] for member in artifact_store.manifest_layout(path, bundle.manifest)] == ["spare"]
    data = artifact_store.archive(bundle, "bundle")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        assert tar.getmember("bundle/spare").isdir()
        safe = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
        tar.extractall(tmp_path / "out", **safe)
    assert sorted(load_artifacts(tmp_path / "out" / "bundle")["targets"]) == ["esp32"]


def test_a_path_no_file_can_have_costs_only_that_path(tmp_path):
    root = tmp_path / "artifacts"
    path = make_bundle(root, A)
    manifest = json.loads((path / "manifest.json").read_text())
    manifest["targets"]["esp32"]["ota"] = {"image": "\ud800.bin", "sha256": "0" * 64}
    manifest["targets"]["x1"] = {"image": "a/\udfff/b.bin", "segments": {"\ud800": "0x0"}}
    (path / "manifest.json").write_text(json.dumps(manifest))  # json escapes the surrogates
    bundle = artifact_store.load_bundle(root, A)
    assert "esp32/flash-image.bin" in bundle.files, "the bundle's real files are still listed"
    assert not any("\ud800" in name or "\udfff" in name for name in bundle.files)
    if os.name == "posix":
        # Where the farm runs, such a name cannot even be encoded for the
        # filesystem (Windows passes surrogates through, and simply finds no
        # file by that name).
        assert artifact_store.safe_path("\ud800.bin") is None
    with pytest.raises(LookupError):
        artifact_store.file_path(bundle, "\ud800.bin")
    artifact_store.archive(bundle, "bundle")


def test_a_manifest_cannot_name_its_way_out_of_the_bundle(tmp_path):
    root = tmp_path / "artifacts"
    path = make_bundle(root, A)
    secret = tmp_path / "secret.bin"
    secret.write_bytes(b"s3cret")
    os.symlink(secret, path / "esp32" / "outside.bin")
    os.symlink(path / "esp32" / "bootloader.bin", path / "esp32" / "alias.bin")
    manifest = json.loads((path / "manifest.json").read_text())
    manifest["targets"]["esp32"]["ota"] = {"image": "esp32/outside.bin", "sha256": "0" * 64}
    manifest["targets"]["x1"] = {"image": "../secret.bin", "sha256": "0" * 64}
    manifest["targets"]["x2"] = {"image": str(secret), "sha256": "0" * 64}
    manifest["targets"]["x3"] = {"image": "esp32/alias.bin", "sha256": "0" * 64}
    (path / "manifest.json").write_text(json.dumps(manifest))

    bundle = artifact_store.load_bundle(root, A)
    assert "esp32/outside.bin" not in bundle.files, "a link out of the bundle"
    assert not any("secret" in relative for relative in bundle.files)
    assert bundle.keys["esp32/alias.bin"] == "esp32/bootloader.bin", "held as the file it resolves to"
    for spelled in ("esp32/outside.bin", "../secret.bin", str(secret)):
        with pytest.raises(LookupError):
            artifact_store.file_path(bundle, spelled)
    with tarfile.open(fileobj=io.BytesIO(artifact_store.archive(bundle, "bundle")), mode="r:gz") as tar:
        members = {member.name: member for member in tar.getmembers()}
    assert members["bundle/esp32/bootloader.bin"].isfile(), "content, as a regular file"
    assert members["bundle/esp32/alias.bin"].issym(), "and the link the manifest is spelled through"
    assert not any("secret" in name or "outside" in name for name in members)



def test_a_manifest_spelled_through_a_link_holds_the_file_the_flasher_reads(tmp_path):
    """`alias/../fw.bin`, with `alias -> sub/inner`, is `sub/fw.bin` to the
    kernel and to load_artifacts -- not `fw.bin`, which is what normalising
    the path lexically would have said. The store holds what is flashed, and
    the archive carries the link so the manifest still resolves once
    extracted."""
    root = tmp_path / "artifacts"
    path = root / A
    (path / "sub" / "inner").mkdir(parents=True)
    (path / "sub" / "fw.bin").write_bytes(b"\xe9the one that is flashed")
    (path / "fw.bin").write_bytes(b"\xe9the one a lexical guess would take")
    os.symlink(path / "sub" / "inner", path / "alias", target_is_directory=True)
    target = {"image": "alias/../fw.bin", "sha256": sha256(path / "sub" / "fw.bin")}
    (path / "manifest.json").write_text(json.dumps({"schema": 2, "git_sha": "1" * 40, "targets": {"esp32": target}}))

    bundle = artifact_store.load_bundle(root, A)
    assert bundle.keys["alias/../fw.bin"] == "sub/fw.bin"
    assert artifact_store.file_path(bundle, "sub/fw.bin").read_bytes().endswith(b"flashed")
    data = artifact_store.archive(bundle, "bundle")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        assert tar.getmember("bundle/alias").issym()
        safe = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
        tar.extractall(tmp_path / "out", **safe)
    # The manifest is archived unchanged, so this is the path it names.
    loaded = load_artifacts(tmp_path / "out" / "bundle")
    assert loaded["targets"]["esp32"]["path"].read_bytes().endswith(b"flashed")


def test_a_manifest_path_that_walks_out_of_the_bundle_is_not_held(tmp_path):
    """Even when a later `..` would come back: the archive could not
    reproduce such a path, and the farm has no business reading through it."""
    root = tmp_path / "artifacts"
    path = make_bundle(root, A)
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "x.bin").write_bytes(b"x")
    os.symlink(tmp_path / "outside", path / "away", target_is_directory=True)
    manifest = json.loads((path / "manifest.json").read_text())
    manifest["targets"]["x1"] = {"image": "away/../artifacts/" + A + "/esp32/flash-image.bin"}
    manifest["targets"]["x2"] = {"image": "away/x.bin"}
    (path / "manifest.json").write_text(json.dumps(manifest))
    bundle = artifact_store.load_bundle(root, A)
    assert "away/x.bin" not in bundle.keys and "away/../artifacts/" + A + "/esp32/flash-image.bin" not in bundle.keys
    assert not any("away" in member["name"] for member in artifact_store.manifest_layout(path, bundle.manifest))

def test_a_link_is_a_reuse_only_when_it_resolves_to_a_bundle_beside_it(tmp_path):
    root = tmp_path / "artifacts"
    make_bundle(root, A)
    os.symlink(root / A, root / C, target_is_directory=True)
    os.symlink(tmp_path, root / D, target_is_directory=True)
    assert artifact_store.link_target(root, C) == A
    assert artifact_store.link_target(root, D) is None, "outside the artifacts directory"
    assert artifact_store.link_target(root, A) is None, "a bundle is not a link"
    assert artifact_store.link_target(root, "../" + C) is None


def test_disk_usage_counts_bundles_and_bytes_without_reading_a_manifest(tmp_path):
    root = tmp_path / "artifacts"
    make_bundle(root, A)
    (root / B).mkdir()
    (root / B / "manifest.json").write_text("not json")
    os.symlink(root / A, root / C, target_is_directory=True)
    size, count = artifact_store.disk_usage(root)
    assert count == 2, "two bundle directories; the link is not one"
    assert size == artifact_store.tree_size(root)[0] > 0
    assert artifact_store.disk_usage(tmp_path / "missing") == (0, 0)


def test_bundle_usage_measures_every_bundle_in_one_pass(tmp_path):
    """What the storage panel shows and what each bundle costs come from the
    same walk. Measuring a bundle at a time, on whatever thread is answering
    a request, is the thing this exists to avoid."""
    root = tmp_path / "artifacts"
    first = make_bundle(root, A)
    (first / ".hidden.bin").write_bytes(b"h" * 700)
    make_bundle(root, B)
    os.symlink(root / A, root / C, target_is_directory=True)
    (root / "notes.txt").write_text("not a bundle")

    per_bundle, total, count = artifact_store.bundle_usage(root)
    assert count == 2 and sorted(per_bundle) == sorted([A, B]), "the link is not a bundle"
    assert per_bundle[A] == artifact_store.tree_size(first)[0] > 0
    assert per_bundle[A] >= 700, "every byte under the directory, hidden files included"
    assert total == artifact_store.tree_size(root)[0] > sum(per_bundle.values())
    assert artifact_store.bundle_usage(tmp_path / "missing") == ({}, 0, 0)
    # Which is what the panel's own figures are, unchanged.
    assert artifact_store.disk_usage(root) == (total, count)


def test_a_reuse_link_is_not_a_bundle(tmp_path):
    root = tmp_path / "artifacts"
    make_bundle(root, A)
    os.symlink(root / A, root / C, target_is_directory=True)
    assert artifact_store.load_bundle(root, C) is None
    assert artifact_store.load_bundle(root, "../" + A) is None
    with pytest.raises(KeyError):
        artifact_store.remove(root, C, {C: A})
    assert (root / A / "manifest.json").is_file(), "deleting through a link would delete another run's images"


def test_a_bundle_archive_extracts_to_a_directory_the_flasher_accepts(tmp_path):
    root = tmp_path / "artifacts"
    make_bundle(root, A, families=("esp32", "esp32-s3"))
    bundle = artifact_store.load_bundle(root, A)
    data = artifact_store.archive(bundle, "painlessmesh-1111111111-aaaaaaaa")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = tar.getmembers()
        assert {member.name for member in members} == {
            f"painlessmesh-1111111111-aaaaaaaa/{name}" for name in bundle.files
        }
        assert all(member.uid == 0 and member.uname == "" and member.mode == 0o644 for member in members)
        safe = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
        tar.extractall(tmp_path / "out", **safe)
    manifest = load_artifacts(tmp_path / "out" / "painlessmesh-1111111111-aaaaaaaa")
    assert sorted(manifest["targets"]) == ["esp32", "esp32-s3"]
    with pytest.raises(ValueError):
        artifact_store.archive(bundle, "../escape")


def test_removing_a_bundle_takes_its_reuse_links_first(tmp_path):
    root = tmp_path / "artifacts"
    make_bundle(root, A)
    make_bundle(root, B)
    os.symlink(root / A, root / C, target_is_directory=True)
    os.symlink(root / B, root / D, target_is_directory=True)
    size = artifact_store.load_bundle(root, A).bytes

    removed = artifact_store.remove(root, A, artifact_store.scan(root).links)
    assert removed == {"id": A, "bytes": size, "links_removed": [C]}
    assert not (root / A).exists() and not os.path.lexists(root / C)
    assert (root / B).is_dir() and (root / D).is_symlink(), "another bundle and its link are untouched"
    with pytest.raises(KeyError):
        artifact_store.remove(root, A, {})


def test_what_a_removal_frees_is_the_whole_directory(tmp_path):
    """A hidden file, or one deeper than a scan goes, is not listed -- and is
    deleted with the bundle all the same. A prune that said it freed nothing
    would be wrong by however much that file was."""
    root = tmp_path / "artifacts"
    path = make_bundle(root, A)
    (path / ".big.bin").write_bytes(b"x" * 5000)
    deep = path / "a" / "b" / "c" / "d" / "e"
    deep.mkdir(parents=True)
    (deep / "deeper.bin").write_bytes(b"y" * 3000)

    bundle = artifact_store.load_bundle(root, A, measure=True)
    assert ".big.bin" not in bundle.files and bundle.disk_bytes >= bundle.bytes + 8000
    assert artifact_store.load_bundle(root, A).disk_bytes == 0, "measured only when asked"
    removed = artifact_store.remove(root, A, {})
    assert removed["bytes"] == bundle.disk_bytes
    assert not (root / A).exists()


def test_a_bundle_that_could_not_be_deleted_keeps_its_reuse_links(tmp_path):
    """The links go first so no run ever points at nothing. If the directory
    then will not delete -- read-only, permissions, an I/O error -- the
    bundle is still there and still reusable, so the links go back: a run
    must not be left saying its images are gone while they are on disk."""
    root = tmp_path / "artifacts"
    path = make_bundle(root, A)
    os.symlink(path, root / B, target_is_directory=True)
    os.symlink(path, root / C, target_is_directory=True)
    pointed_at = os.readlink(root / B)
    links = {B: A, C: A}

    def a_read_only_filesystem(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    real_rmtree = artifact_store.shutil.rmtree
    artifact_store.shutil.rmtree = a_read_only_filesystem
    try:
        with pytest.raises(PermissionError):
            artifact_store.remove(root, A, links)
    finally:
        artifact_store.shutil.rmtree = real_rmtree

    assert (root / A).is_dir(), "the bundle is still there"
    assert (root / B).is_symlink() and (root / C).is_symlink(), "and so are its links"
    assert os.readlink(root / B) == pointed_at, "pointing where they did"
    found = artifact_store.scan(root)
    assert found.links == links and found.dangling == []
    # And the delete that works takes them, as it always did.
    removed = artifact_store.remove(root, A, links)
    assert sorted(removed["links_removed"]) == sorted([B, C])
    assert not (root / A).exists() and not (root / B).is_symlink()


def test_only_links_to_nothing_are_tidied(tmp_path):
    root = tmp_path / "artifacts"
    make_bundle(root, A)
    os.symlink(root / A, root / C, target_is_directory=True)
    os.symlink(root / ("e" * 32), root / D)
    assert artifact_store.remove_dangling(root, [C, D, "../x"]) == [D]
    assert (root / C).is_symlink()


def _entry(bundle_id, profile, days_ago, **extra):
    moment = datetime(2026, 9, 11, tzinfo=timezone.utc) - timedelta(days=days_ago)
    return {"id": bundle_id, "profile": profile, "last_used_at": moment.isoformat(), "pinned": False, "held": False, **extra}


def test_a_prune_rule_is_required_and_pinned_or_held_bundles_are_never_chosen():
    now = datetime(2026, 9, 11, tzinfo=timezone.utc)
    entries = [
        _entry("p1", "painlessmesh", 1),
        _entry("p2", "painlessmesh", 10),
        _entry("p3", "painlessmesh", 40),
        _entry("p4", "painlessmesh", 50, pinned=True),
        _entry("f1", "alteriom-firmware", 45),
        _entry("f2", "alteriom-firmware", 60, held=True),
    ]
    with pytest.raises(ValueError, match="say what to prune"):
        artifact_store.select_prunable(entries, now=now)
    # Age alone: everything unused for more than 30 days, oldest first.
    assert artifact_store.select_prunable(entries, older_than_days=30, now=now) == ["f1", "p3"]
    # A count alone: beyond the newest one per project.
    assert artifact_store.select_prunable(entries, keep_per_profile=1, now=now) == ["p3", "p2"]
    # Both: beyond the kept window *and* that old.
    assert artifact_store.select_prunable(entries, older_than_days=5, keep_per_profile=2, now=now) == ["p3"]
    assert artifact_store.select_prunable(entries, keep_per_profile=0, now=now) == ["f1", "p3", "p2", "p1"]


def test_tree_size_never_follows_a_link(tmp_path):
    (tmp_path / "tree" / "sub").mkdir(parents=True)
    (tmp_path / "tree" / "a.bin").write_bytes(b"a" * 10)
    (tmp_path / "tree" / "sub" / "b.bin").write_bytes(b"b" * 5)
    (tmp_path / "big").write_bytes(b"x" * 1000)
    os.symlink(tmp_path / "big", tmp_path / "tree" / "link.bin")
    assert artifact_store.tree_size(tmp_path / "tree") == (15, 2)
    assert artifact_store.tree_size(tmp_path / "missing") == (0, 0)


def test_child_usage_measures_each_child_once_and_counts_loose_files(tmp_path):
    """One walk gives both the storage panel's total and the detail page's
    list, so the two can never disagree about what is in a directory."""
    import os

    from alteriom_hil import artifact_store

    (tmp_path / "a" / "deep").mkdir(parents=True)
    (tmp_path / "a" / "deep" / "one.bin").write_bytes(b"1" * 100)
    (tmp_path / "a" / "two.bin").write_bytes(b"2" * 20)
    (tmp_path / "loose.log").write_bytes(b"3" * 7)
    children, total, files = artifact_store.child_usage(tmp_path)
    by_name = {item["name"]: item for item in children}
    assert by_name["a"] == {**by_name["a"], "bytes": 120, "files": 2, "dir": True}
    assert by_name["loose.log"]["bytes"] == 7 and by_name["loose.log"]["dir"] is False
    assert (total, files) == (127, 3)
    assert artifact_store.child_usage(tmp_path / "missing") == ([], 0, 0)
    try:
        os.symlink(tmp_path / "a", tmp_path / "link", target_is_directory=True)
    except OSError:
        return  # no symlinks on this host; the rest has been shown
    assert "link" not in {item["name"] for item in artifact_store.child_usage(tmp_path)[0]}
