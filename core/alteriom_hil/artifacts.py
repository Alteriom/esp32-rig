"""The immutable artifact manifest: the contract between a build and a flash.

A consuming project's build step emits a directory containing `manifest.json`
plus one merged flash image per MCU family. The farm verifies and flashes it
without knowing anything else about the project -- which is the point, and the
reason this lives in the HAL rather than beside any one suite.

The manifest is schema 2:

    {
      "schema": 2,
      "<revision key>": "<40-hex commit the build resolved to>",
      "targets": {
        "esp32": {
          "board": "esp32dev",
          "chip": "esp32",
          "image": "esp32/flash-image.bin",   # merged, flashed whole
          "sha256": "...",
          "flash_offset": "0x0",
          "segments": {"bootloader.bin": "0x1000", ...},  # optional
          "files": {"bootloader.bin": {"sha256": "..."}, ...},
          "ota": {"image": "...", "sha256": "..."}        # optional
        }
      }
    }

The *revision key* is named by the profile, not fixed here: painlessMesh calls
it `painlessmesh_sha`, another project will call it something else, and the
loader has no reason to care which.

Verification is not a formality. `load_artifacts` re-hashes every image and
every component, and checks that each component actually appears at its stated
offset inside the merged image. A flash is the one step that cannot be undone
by retrying, and an artifact that was corrupted in transit would otherwise be
diagnosed as a firmware bug on hardware.
"""

from __future__ import annotations

import hashlib
import io
import shutil
import tarfile
from pathlib import Path

MANIFEST_SCHEMA = 2

# A bundle is firmware images; this is the room they get. It bounds what an
# archive may expand to, so a small file cannot fill a rig's disk.
MAX_BUNDLE_BYTES = 256 * 1024 * 1024


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_artifacts(directory: Path) -> dict[str, dict]:
    """Read and verify the manifest in `directory`.

    Returns the manifest with a resolved `path` added to each target (and to
    each target's `ota`, when present), so callers flash a checked file rather
    than re-deriving the location.
    """
    import json  # local: keeps module import cost off the HAL's hot path

    directory = Path(directory)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ValueError(f"unsupported artifact manifest: {manifest_path}")
    for name, entry in manifest.get("targets", {}).items():
        image = directory / entry["image"]
        if not image.is_file():
            raise FileNotFoundError(f"artifact image missing for {name}: {image}")
        actual = sha256(image)
        if actual != entry["sha256"]:
            raise ValueError(f"artifact checksum mismatch for {name}: {image}")
        ota = entry.get("ota")
        if ota:
            ota_image = directory / ota["image"]
            if not ota_image.is_file() or sha256(ota_image) != ota["sha256"]:
                raise ValueError(f"OTA artifact checksum mismatch for {name}: {ota_image}")
            ota["path"] = ota_image
        merged = image.read_bytes()
        for filename, offset_text in entry.get("segments", {}).items():
            component = directory / name / filename
            metadata = entry["files"][filename]
            if not component.is_file() or sha256(component) != metadata["sha256"]:
                raise ValueError(f"component checksum mismatch for {name}: {filename}")
            content = component.read_bytes()
            offset = int(offset_text, 0)
            if merged[offset : offset + len(content)] != content:
                raise ValueError(
                    f"merged image segment mismatch for {name}: {filename}"
                )
        entry["path"] = image
    return manifest


def manifest_revision(manifest: dict, revision_key: str) -> str:
    """The commit a manifest was built from, under the profile's key."""
    revision = manifest.get(revision_key)
    if not revision:
        raise ValueError(
            f"manifest has no {revision_key!r}; the profile's build.revision_key "
            f"must name a key the build script writes (found: "
            f"{sorted(k for k in manifest if k != 'targets')})"
        )
    return str(revision)


def extract_bundle(body: bytes, destination: Path, limit: int | None = None) -> None:
    """Unpack one directory of regular files, and nothing else.

    No absolute path, no `..`, no link, no device: the rules `tarfile`'s data
    filter applies, checked here so they hold on every Python the Pi has run
    rather than only the ones that have the filter. The expanded size is
    bounded too -- a small archive must not be able to fill the disk -- and the
    archive's single top directory is dropped, so the bundle lands laid out
    exactly as a farm build writes it.

    It lives beside the manifest it produces, because both ends of a bundle
    now read it: a portal taking one from a producer, and a rig unpacking the
    health check firmware out of a release (docs/public-release-plan.md, 13d).
    """
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
        members = archive.getmembers()
        if not members:
            raise ValueError("the bundle archive is empty")
        tops: set = set()
        total = 0
        for member in members:
            name = member.name.replace("\\", "/")
            if not (member.isfile() or member.isdir()):
                raise ValueError(
                    f"a bundle holds regular files only; {name} is not one"
                )
            parts = [part for part in name.split("/") if part not in ("", ".")]
            if name.startswith("/") or ".." in parts or not parts:
                raise ValueError(f"the bundle archive names a path outside itself: {name}")
            tops.add(parts[0])
            total += max(member.size, 0)
            if total > (MAX_BUNDLE_BYTES if limit is None else limit):
                raise ValueError("the bundle expands past the size a bundle may be")
        if len(tops) != 1:
            raise ValueError(
                f"a bundle archive holds exactly one directory; this holds {len(tops)}"
            )
        destination.mkdir(parents=True)
        for member in members:
            parts = [
                part
                for part in member.name.replace("\\", "/").split("/")
                if part not in ("", ".")
            ]
            if len(parts) == 1:
                if member.isfile():
                    raise ValueError(
                        f"a bundle archive holds one directory; {member.name} is beside it"
                    )
                continue
            target = destination.joinpath(*parts[1:])
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"the bundle archive cannot read {member.name}")
            with target.open("wb") as handle:
                shutil.copyfileobj(source, handle)
