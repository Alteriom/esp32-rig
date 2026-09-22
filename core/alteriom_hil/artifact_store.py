"""The firmware bundles a farm keeps on disk: find them, hand them back, let them go.

A bundle is a directory the schema-2 contract describes (`alteriom_hil.artifacts`):
`manifest.json` beside one directory per MCU family holding the merged image and
the components it was merged from. The farm keeps one per job that built, named
by that job's id, and a job that reused an earlier build gets a symlink to that
directory instead of a copy.

Until this module the farm could create bundles and never account for them:
2.2 GB in eleven days on the Pi, no list, no sizes, and no safe way to delete
one -- removing a directory by hand leaves every run that reused it pointing at
nothing.

This module knows only the directory. Which job built a bundle, which profile it
was for and whether a job still needs it is the service's knowledge, handed in.
The split keeps the code that deletes files small enough to test on its own.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import tarfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# A bundle is named by the id of the job that made it: 32 hex digits. Anything
# else under the artifacts directory is not a bundle and is left alone.
BUNDLE_ID = re.compile(r"[0-9a-f]{32}\Z")
# What a bundle holds, and therefore what may be served, archived or counted,
# is two things. First, every file the manifest tells the flasher to read --
# each image, OTA image and component, wherever the manifest put it, since
# alteriom_hil.artifacts.load_artifacts accepts any relative path. Second,
# everything else a scan finds without following a symlink: regular files
# whose every path component is a plain name, down to BUNDLE_DEPTH
# directories. Either way, nothing that resolves outside the bundle.
FILE_NAME_MAX = 64
FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,%d}\Z" % (FILE_NAME_MAX - 1))
BUNDLE_DEPTH = 4
# An archive is built in memory. A six-family painlessMesh bundle is ~17 MB; a
# bundle a hundred times that is not one this farm made.
ARCHIVE_LIMIT_BYTES = 512 * 1024 * 1024


@dataclass
class Bundle:
    id: str
    path: Path
    # Relative POSIX path -> size in bytes, for every file the bundle holds.
    files: dict = field(default_factory=dict)
    # The parsed manifest, or None when it is missing or unreadable -- such a
    # directory is still listed, so it can still be deleted.
    manifest: dict | None = None
    # When the bundle was written (its manifest's mtime), ISO 8601 in UTC.
    modified: str = ""
    # Manifest path as written -> the key this bundle holds that file by.
    # The two differ when a manifest is spelled through a directory or a
    # link: `alias/../fw.bin` is held as `sub/fw.bin`.
    keys: dict = field(default_factory=dict)
    # Every byte under the directory -- what deleting it frees, including
    # whatever the enumeration above leaves out. Walking the whole tree is
    # not free, and most callers only want to know the bundle is there, so
    # it is 0 unless `load_bundle` was asked to measure. `bundle_usage`
    # measures every bundle in one pass, off the request thread.
    disk_bytes: int = 0

    @property
    def bytes(self) -> int:
        return sum(self.files.values())


@dataclass
class Scan:
    bundles: dict = field(default_factory=dict)  # id -> Bundle
    links: dict = field(default_factory=dict)  # link id -> id of the bundle it points to
    dangling: list = field(default_factory=list)  # link ids whose bundle is gone


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _collect(path: str, prefix: str, depth: int, files: dict) -> None:
    try:
        entries = sorted(os.scandir(path), key=lambda entry: entry.name)
    except OSError:
        return
    for entry in entries:
        if not FILE_NAME.fullmatch(entry.name):
            continue
        try:
            if entry.is_symlink():
                continue
            if entry.is_file():
                files[prefix + entry.name] = entry.stat().st_size
            elif entry.is_dir() and depth > 0:
                _collect(entry.path, f"{prefix}{entry.name}/", depth - 1, files)
        except OSError:
            # Gone between the listing and the look -- a delete or a prune got
            # there first. What remains is still an answer; an exception here
            # would fail the whole listing over one file.
            continue


def bundle_contents(path: Path, manifest: dict | None = None) -> tuple:
    """What a bundle holds and where each manifest path lands in it.

    Returns `(files, keys)`: relative POSIX path -> size for every file, and
    manifest path as written -> the key the file it resolves to is held by.
    The two differ whenever a manifest walks through a directory or a link:
    `alias/../fw.bin`, with `alias -> sub/inner`, is the file `sub/fw.bin`,
    which is what load_artifacts reads and so what the store must hold.
    """
    files: dict = {}
    _collect(str(path), "", BUNDLE_DEPTH, files)
    keys: dict = {}
    for relative in manifest_paths(manifest):
        found = _resolved(path, relative)
        if found is None:
            continue
        key, size = found
        files.setdefault(key, size)
        keys[relative] = key
    return files, keys


def bundle_files(path: Path, manifest: dict | None = None) -> dict:
    """Every file a bundle holds, by relative POSIX path, with its size."""
    return bundle_contents(path, manifest)[0]


def manifest_paths(manifest: dict | None) -> list:
    """Every relative path a schema-2 manifest has the flasher read: each
    target's image, its OTA image, and each component it verifies at an
    offset (`<family>/<file>`, as load_artifacts reads them)."""
    paths: list = []
    targets = (manifest or {}).get("targets")
    if not isinstance(targets, dict):
        return paths
    for family, entry in targets.items():
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("image"), str):
            paths.append(entry["image"])
        ota = entry.get("ota")
        if isinstance(ota, dict) and isinstance(ota.get("image"), str):
            paths.append(ota["image"])
        segments = entry.get("segments")
        if isinstance(segments, dict):
            paths.extend(f"{family}/{name}" for name in segments if isinstance(name, str))
    return paths


def safe_path(relative) -> str | None:
    """A manifest path that could name a file at all: a relative path with no
    NUL that the filesystem can encode. Whether it stays inside the bundle is
    decided by walking it, not by reading it -- a link changes where `..`
    goes, so no lexical rule can answer that."""
    if not isinstance(relative, str) or not relative or "\x00" in relative or relative.startswith("/"):
        return None
    try:
        os.fsencode(relative)
    except UnicodeError:
        # A lone surrogate, which JSON's \ud800 escape produces from a
        # corrupted or hand-edited manifest: no file on disk has this name,
        # and every os.path call on it would raise rather than say so.
        return None
    return relative


def _relative(target: str, root: str) -> str:
    return os.path.relpath(target, root).replace(os.sep, "/")


def _walk(root: str, relative: str) -> tuple | None:
    """Follow a manifest path component by component, as the kernel does:
    each link resolved where it is met, each `..` taken from where the walk
    has got to. Returns the file it reaches and the directories and links it
    walked along, or None if it reaches no file inside the bundle.

    Nothing that leaves the bundle is accepted, even if a later `..` would
    come back: the archive could not reproduce such a path, and the farm has
    no business reading through it.
    """
    parts = [part for part in relative.split("/") if part not in ("", ".")]
    if not parts:
        return None
    current = root
    walked: list = []
    for index, component in enumerate(parts):
        if component == "..":
            if current == root:
                return None
            current = os.path.dirname(current)
            continue
        candidate = os.path.join(current, component)
        if os.path.islink(candidate):
            resolved = os.path.realpath(candidate)
            if os.path.commonpath([root, resolved]) != root:
                return None
            # Kept as a path relative to the link, so it means the same thing
            # inside an extracted archive as it does here.
            target = _relative(resolved, os.path.dirname(candidate))
            walked.append(("link", _relative(candidate, root), target))
            if os.path.isdir(resolved):
                # Where the link points: an empty directory no archived file
                # would create, and without it the link dangles once extracted.
                walked.append(("dir", _relative(resolved, root), None))
            current = resolved
        elif os.path.isdir(candidate):
            walked.append(("dir", _relative(candidate, root), None))
            current = candidate
        elif index == len(parts) - 1 and os.path.isfile(candidate):
            current = candidate
        else:
            return None
    return (current, walked) if os.path.isfile(current) else None


def _resolved(path: Path, relative: str) -> tuple | None:
    """The key a manifest path's file is held by, and that file's size."""
    if safe_path(relative) is None:
        return None
    try:
        # Inside the guard: a manifest is whatever a build wrote, and one bad
        # path must cost that path, not the whole listing.
        root = os.path.realpath(path)
        walked = _walk(root, relative)
        if walked is None:
            return None
        target = walked[0]
        return _relative(target, root), os.stat(target).st_size
    except (OSError, ValueError):  # UnicodeError is a ValueError
        return None


def manifest_layout(path: Path, manifest: dict | None) -> list:
    """The directories and links a manifest's own spellings walk along, so an
    extracted archive resolves them exactly as the bundle does.

    A plain archive of the files it holds would lose an empty directory a
    `..` step passes through (`spare/` in `spare/../firmware.bin`) and the
    link a path is spelled through (`alias/` in `alias/../fw.bin`), and the
    manifest, which is archived unchanged, would then resolve to nothing.
    """
    try:
        root = os.path.realpath(path)
    except (OSError, ValueError):
        return []
    seen: dict = {}
    for relative in manifest_paths(manifest):
        if safe_path(relative) is None:
            continue
        try:
            walked = _walk(root, relative)
        except (OSError, ValueError):
            continue
        if walked is None:
            continue
        if _relative(walked[0], root) == relative:
            # The path names its file directly: archiving the file creates
            # every directory on the way, and there is nothing else to keep.
            continue
        for kind, name, target in walked[1]:
            seen.setdefault(name, (kind, target))
    return [{"kind": kind, "name": name, "target": target} for name, (kind, target) in sorted(seen.items())]


def link_target(root: Path, name: str) -> str | None:
    """The bundle the reuse link `name` points to, or None.

    A link is a reuse only when it resolves to a bundle directory directly
    under `root`. Anything else -- a link to nothing, to somewhere outside,
    to a directory that is not a bundle -- is not one.
    """
    path = Path(root) / name
    if not BUNDLE_ID.fullmatch(name or "") or not path.is_symlink():
        return None
    target = os.path.realpath(path)
    if os.path.dirname(target) != os.path.realpath(root):
        return None
    bundle = os.path.basename(target)
    return bundle if BUNDLE_ID.fullmatch(bundle) and os.path.isdir(target) else None


def load_bundle(root: Path, bundle_id: str, measure: bool = False) -> Bundle | None:
    """The bundle directory named `bundle_id`, or None if there is none.

    A reuse link is not a bundle: it holds nothing, and deleting through it
    would delete another run's images.

    `measure` walks the whole tree for `disk_bytes`. Off by default: a
    listing of every bundle would walk the store twice over on a request
    thread, and most callers -- a job asking whether its bundle is still
    there, a pin, a download -- never read the figure at all.
    """
    import json

    if not BUNDLE_ID.fullmatch(bundle_id or ""):
        return None
    path = Path(root) / bundle_id
    if path.is_symlink() or not path.is_dir():
        return None
    manifest_path = path / "manifest.json"
    manifest = None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            manifest = None
    except (OSError, ValueError):
        manifest = None
    try:
        modified = _iso((manifest_path if manifest_path.is_file() else path).stat().st_mtime)
    except OSError:
        modified = ""
    files, keys = bundle_contents(path, manifest)
    disk_bytes = tree_size(path)[0] if measure else 0
    return Bundle(bundle_id, path, files, manifest, modified, keys, disk_bytes)


def scan(root: Path, measure: bool = False) -> Scan:
    """Every bundle under `root`, every reuse link, and every link left dangling.

    A link that points outside `root` at something that exists is not the
    farm's to judge and is ignored: nothing here deletes what it did not make.
    One that points at nothing holds nothing, wherever it pointed.
    """
    root = Path(root)
    result = Scan()
    try:
        entries = sorted(os.scandir(root), key=lambda entry: entry.name)
    except OSError:
        return result
    for entry in entries:
        if not BUNDLE_ID.fullmatch(entry.name):
            continue
        if entry.is_symlink():
            target = link_target(root, entry.name)
            if target:
                result.links[entry.name] = target
            elif not os.path.exists(entry.path):  # follows the link: False when it points at nothing
                result.dangling.append(entry.name)
            continue
        if entry.is_dir():
            bundle = load_bundle(root, entry.name, measure)
            if bundle is not None:
                result.bundles[entry.name] = bundle
    return result


def bundle_usage(root: Path) -> tuple:
    """`(per_bundle, total, count)` for the artifacts directory in one pass.

    `per_bundle` is bundle id -> bytes under that directory; `total` is every
    byte under `root`, bundles and anything else alike; `count` is how many
    bundles there are. Not a manifest is read.

    One walk answers both what the storage panel shows and what each bundle
    costs, which is what the artifact listing reports -- measured here, on
    the measuring thread, instead of per bundle on whatever thread happens
    to be answering a request.
    """
    per_bundle: dict = {}
    total = count = 0
    try:
        entries = list(os.scandir(root))
    except OSError:
        return per_bundle, total, count
    for entry in entries:
        try:
            if entry.is_symlink():
                continue
            if entry.is_file():
                total += entry.stat().st_size
                continue
            if not entry.is_dir():
                continue
            size, _ = tree_size(entry.path)
            total += size
            if BUNDLE_ID.fullmatch(entry.name):
                per_bundle[entry.name] = size
                count += 1
        except OSError:
            # Gone or unreadable: a size a little low is still an answer.
            continue
    return per_bundle, total, count


def disk_usage(root: Path) -> tuple:
    """Bytes under the artifacts directory and how many bundles it holds.

    What the storage panel needs, without reading a single manifest: it is
    asked every few seconds while the disk is being measured.
    """
    _per_bundle, total, count = bundle_usage(root)
    return total, count


def file_path(bundle: Bundle, relative: str) -> Path:
    """The real file behind one relative path of a bundle, if the bundle
    holds it.

    Answered from the enumeration, never from the path the caller spelled,
    and resolved again here: a manifest may name a file through a link, so
    what is read is checked to be inside the bundle at the moment it is read,
    not only when the bundle was listed.
    """
    if relative not in bundle.files:
        raise LookupError(f"bundle {bundle.id[:8]} holds no file {relative!r}")
    found = _resolved(bundle.path, relative)
    if found is None or found[0] != relative:
        raise LookupError(f"bundle {bundle.id[:8]} no longer holds {relative!r} inside itself")
    return Path(os.path.join(os.path.realpath(bundle.path), relative))


def archive(bundle: Bundle, top: str) -> bytes:
    """The bundle as a .tar.gz under one directory named `top`.

    Laid out exactly as it was flashed -- manifest beside the images, at the
    paths the manifest names -- so the extracted directory is itself a valid
    artifact directory: `alteriom-hil-flash --artifacts <top>` flashes it as
    it comes. Every file is archived as a regular file holding the content
    it resolves to; the only links it carries are those a manifest path is
    spelled through, and those point inside the bundle.
    """
    if bundle.bytes > ARCHIVE_LIMIT_BYTES:
        raise ValueError(f"bundle {bundle.id[:8]} is {bundle.bytes} bytes, past the archive limit")
    if not FILE_NAME.fullmatch(top):
        raise ValueError(f"invalid archive directory name: {top!r}")

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        # The directories and links a manifest path is spelled through, so
        # the manifest, archived unchanged, still resolves once extracted.
        for member in manifest_layout(bundle.path, bundle.manifest):
            entry = tarfile.TarInfo(f"{top}/{member['name']}")
            if member["kind"] == "link":
                entry.type = tarfile.SYMTYPE
                entry.linkname = member["target"]
                entry.mode = 0o777
            else:
                entry.type = tarfile.DIRTYPE
                entry.mode = 0o755
            tar.addfile(entry)
        for relative in sorted(bundle.files):
            source = file_path(bundle, relative)
            info = tar.gettarinfo(str(source), arcname=f"{top}/{relative}")
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o644
            with open(source, "rb") as stream:
                tar.addfile(info, stream)
    return buffer.getvalue()


def remove(root: Path, bundle_id: str, links: dict) -> dict:
    """Delete one bundle, and every reuse link that points to it first.

    The links go before the directory so there is no moment in which a run's
    artifacts entry points at nothing. If the directory then cannot be
    deleted -- a read-only filesystem, permissions, an I/O error -- the
    links are put back: the bundle is still there and still reusable, and a
    run that reused it must not be left saying its images are gone. The
    failure is raised for the caller to report. Returns what was freed.
    """
    bundle = load_bundle(root, bundle_id)
    if bundle is None:
        raise KeyError(bundle_id)
    taken: list = []
    for link, target in sorted(links.items()):
        if target != bundle_id:
            continue
        path = Path(root) / link
        if not path.is_symlink():
            continue
        try:
            # Where it pointed, as spelled, so putting it back reproduces it.
            pointed_at = os.readlink(path)
            path.unlink()
        except OSError:
            # Gone, or not ours to take, between the look and the taking.
            # The bundle's delete goes ahead; a link left pointing at
            # nothing is listed as dangling and tidied by a prune.
            continue
        taken.append((link, pointed_at))
    # Measured, not summed from the listing: a hidden or very deep file is
    # not listed and is deleted all the same, and a prune that said it
    # freed nothing would be wrong by however much that file was.
    freed, _ = tree_size(bundle.path)
    try:
        shutil.rmtree(bundle.path)
    except Exception:
        for link, pointed_at in taken:
            path = Path(root) / link
            try:
                if not path.is_symlink() and not path.exists():
                    os.symlink(pointed_at, path, target_is_directory=True)
            except OSError:
                continue
        raise
    return {"id": bundle_id, "bytes": freed, "links_removed": [link for link, _ in taken]}


def remove_dangling(root: Path, dangling: list) -> list:
    """Unlink reuse links whose bundle no longer exists."""
    removed = []
    for link in dangling:
        path = Path(root) / link
        if BUNDLE_ID.fullmatch(link) and path.is_symlink() and not path.exists():
            path.unlink()
            removed.append(link)
    return removed


def when(value: str | None) -> datetime:
    """An ISO 8601 timestamp as an aware datetime; anything unparseable is
    the distant past, so it sorts as oldest rather than raising."""
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def latest(*stamps: str | None) -> str | None:
    """The latest of some ISO 8601 timestamps, compared as times, not text."""
    present = [stamp for stamp in stamps if stamp]
    return max(present, key=when) if present else None


def select_prunable(
    entries: list,
    older_than_days: int | None = None,
    keep_per_profile: int | None = None,
    now: datetime | None = None,
) -> list:
    """The ids a prune would delete, oldest first.

    `entries` are dicts with `id`, `profile`, `last_used_at`, `pinned` and
    `held`. A pinned bundle, and one a queued or running job holds, is never
    chosen. Of the rest:

    - `keep_per_profile` keeps the newest that many per profile, by last use;
    - `older_than_days` chooses only bundles last used longer ago than that;
    - both together choose what is past the kept window *and* that old.

    Neither is refused: "prune" with no rule would mean "delete everything",
    and that should be a sentence someone typed rather than a default.
    """
    if older_than_days is None and keep_per_profile is None:
        raise ValueError("say what to prune: older_than_days, keep_per_profile, or both")
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=older_than_days) if older_than_days is not None else None
    candidates = [entry for entry in entries if not entry.get("pinned") and not entry.get("held")]
    by_profile: dict = {}
    for entry in candidates:
        by_profile.setdefault(entry.get("profile") or "", []).append(entry)
    chosen = []
    for group in by_profile.values():
        group.sort(key=lambda entry: when(entry.get("last_used_at")), reverse=True)
        beyond = group[keep_per_profile:] if keep_per_profile is not None else group
        for entry in beyond:
            if cutoff is None or when(entry.get("last_used_at")) < cutoff:
                chosen.append(entry)
    chosen.sort(key=lambda entry: when(entry.get("last_used_at")))
    return [entry["id"] for entry in chosen]


def child_usage(root: Path) -> tuple:
    """`(children, total, files)` for a directory, in one pass.

    `children` is one entry per immediate child -- name, bytes, file count,
    last modified, whether it is a directory -- and `total` and `files` are
    everything under `root`, loose files included. What the storage panel's
    detail pages list, from the same walk that measures the totals, so
    looking at what fills a directory costs no second walk and never happens
    on a request. Symlinks are neither followed nor counted.
    """
    children: list = []
    total = files = 0
    try:
        entries = list(os.scandir(root))
    except OSError:
        return children, total, files
    for entry in entries:
        try:
            if entry.is_symlink():
                continue
            stat = entry.stat()
            if entry.is_dir():
                size, count = tree_size(entry.path)
                is_dir = True
            elif entry.is_file():
                size, count, is_dir = stat.st_size, 1, False
            else:
                continue
        except OSError:
            # Gone or unreadable: a list a little short is still an answer.
            continue
        children.append({
            "name": entry.name, "bytes": size, "files": count,
            "modified": _iso(stat.st_mtime), "dir": is_dir,
        })
        total += size
        files += count
    return children, total, files


def tree_size(path: Path) -> tuple:
    """Bytes and file count under `path`, never following a symlink.

    For the storage panel. Unreadable entries are skipped: a size that is a
    little low is still an answer, and an exception is not.
    """
    total = files = 0
    stack = [str(path)]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    stack.append(entry.path)
                elif entry.is_file():
                    total += entry.stat().st_size
                    files += 1
            except OSError:
                continue
    return total, files
