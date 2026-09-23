"""What a release is, as a document.

`runner/ci/build-release.sh` writes it: a `release.json` naming the two
wheels, the dashboard bundle, the health check firmware when the build made
one, and for each one its digest and its size. The
portal checks that document against the files published to it; a rig checks
it against the files it downloaded before installing them. One description,
because a release that means two things to two readers is not a release
(docs/public-release-plan.md, step 13).

Nothing here reads a network or a disk. `check` is given a reader, so the
same code checks a directory, a portal's store, and a test's dictionary.
"""

from __future__ import annotations

import hashlib
import json
import re

# What a release is made of besides its bundle, by plain file name: the two
# wheels, the dashboard bundle, the checksums, the manifest. No path in it --
# a release names files, and a name with a directory in it is not a name.
RELEASE_FILE_PATTERN = re.compile(
    r"(?:[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(?:whl|tar\.gz)|SHA256SUMS|release\.json)"
)

SCHEMA = 1


class ReleaseError(ValueError):
    """A release document that cannot be believed."""


def parse_manifest(body: bytes, commit: str | None = None) -> dict:
    """The manifest as a document: its shape, not its files.

    `commit`, when given, is the release this manifest must be of -- a
    manifest of another release is the likeliest way for the wrong files to
    end up installed, and it costs one comparison to refuse.
    """
    try:
        manifest = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"release.json is not JSON: {exc}") from None
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise ReleaseError(f"release.json must be a schema-{SCHEMA} manifest (runner/ci/build-release.sh)")
    if commit is not None and manifest.get("commit") != commit:
        raise ReleaseError(f"release.json is of {str(manifest.get('commit'))[:12]}, not {commit[:12]}")
    packages = manifest.get("packages")
    dashboard = manifest.get("dashboard")
    if not isinstance(packages, list) or not packages or not isinstance(dashboard, dict):
        raise ReleaseError("release.json names no packages, or no dashboard")
    firmware = manifest.get("firmware")
    if firmware is not None and not isinstance(firmware, dict):
        raise ReleaseError("release.json's firmware is not a file it names")
    for entry in entries(manifest):
        name = entry.get("name") if isinstance(entry, dict) else None
        if not (isinstance(name, str) and RELEASE_FILE_PATTERN.fullmatch(name)):
            raise ReleaseError(f"release.json names a file it may not: {name!r}")
        if not isinstance(entry.get("sha256"), str) or not isinstance(entry.get("bytes"), int):
            raise ReleaseError(f"release.json says nothing checkable about {name}")
    # The firmware is a bundle, not a wheel: what a rig does with it depends
    # on which boards it is for and which build it is, so a release that names
    # one says both. Nothing else may claim to be firmware by leaving them out.
    if firmware is not None:
        families = firmware.get("families")
        if not isinstance(firmware.get("version"), str) or not isinstance(firmware.get("revision"), str):
            raise ReleaseError("release.json's firmware says no version or no revision")
        if not isinstance(families, list) or not families or not all(isinstance(f, str) for f in families):
            raise ReleaseError("release.json's firmware is for no board family")
    return manifest


def entries(manifest: dict) -> list[dict]:
    """Every file the manifest names: packages, dashboard, and the health
    check firmware when the release carries one. `check` walks this, so a
    file that is named here is a file a rig refuses the release over."""
    named = [*manifest["packages"], manifest["dashboard"]]
    firmware = manifest.get("firmware")
    if firmware:
        named.append(firmware)
    return named


def firmware(manifest: dict) -> dict | None:
    """The health check firmware this release carries, if it carries one.

    A release built without it is a release: `alteriom_hil.release` describes
    what a build made, and a build on a host with no toolchain makes wheels.
    A rig asked to install the firmware of a release that has none is told so
    rather than handed an empty bundle."""
    return manifest.get("firmware") or None


def check(manifest: dict, read) -> None:
    """Every file the manifest names, as `read(name)` gives it back.

    Raises on the first one that is missing or is not what the manifest
    describes. A rig installs nothing until this has returned.
    """
    for entry in entries(manifest):
        name = entry["name"]
        try:
            data = read(name)
        except (OSError, LookupError) as exc:
            raise ReleaseError(f"release.json names {name}, which is not there: {exc}") from None
        if data is None:
            raise ReleaseError(f"release.json names {name}, which is not there")
        if len(data) != entry["bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ReleaseError(f"{name} is not the file release.json describes (size or digest differ)")


def summary(manifest: dict) -> dict:
    """What a record or a person needs of a checked manifest, and no more."""
    keep = ("name", "sha256", "bytes")
    kept = {
        "version": manifest.get("version"),
        "commit": manifest.get("commit"),
        "packages": [{k: entry[k] for k in keep} for entry in manifest["packages"]],
        "dashboard": {k: manifest["dashboard"][k] for k in (*keep, "contract") if k in manifest["dashboard"]},
    }
    carried = firmware(manifest)
    if carried:
        kept["firmware"] = {k: carried[k] for k in (*keep, "version", "revision", "families") if k in carried}
    return kept


def wheels(manifest: dict) -> list[str]:
    """The packages to install, core before rig: the rig requires the core,
    and a pip that is given both in one call does not care, but a pip given
    them one at a time does."""
    names = [entry["name"] for entry in manifest["packages"]]
    return sorted(names, key=lambda name: (not name.startswith("alteriom_hil_core"), name))
