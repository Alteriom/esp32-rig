#!/usr/bin/env python3
"""Build the Rig Health Check firmware: one merged flash image per ESP32 MCU family.

The Rig Health Check (the `canary` profile) is the farm's own firmware -- ESP
and rig health, nothing from painlessMesh and nothing from a consumer -- so
this build has no library to fetch. Three identities travel in the manifest
and they answer different questions:

  * `farm_sha`, the farm commit this was built from, is the bundle's
    revision of record (the profile's `revision_key`). It is what the rest
    of the farm means by a revision: a bundle belongs to a release, is
    reused by a run that asks for that commit, and links to something a
    person can read.
  * `canary_sha`, the digest of `canary/firmware/`, is the firmware's own
    identity. Two farm commits that did not touch the canary produce the
    same `canary_sha`, which is what lets a deploy skip the build -- and
    what a board reports back over serial, so a report can say which canary
    answered rather than which release installed it.
  * `version`, MAJOR.MINOR.PATCH, is the one a person reads. MAJOR.MINOR is
    `canary/firmware/VERSION`, moved by hand; PATCH is the number of commits
    that changed `canary/firmware/`, so it rises by itself with the firmware
    and stands still across farm releases that did not touch it -- the same
    scheme as the farm's own version, counted over the firmware alone. It is
    compiled in, so a board reports it too.

The output is the schema-2 manifest contract in `alteriom_hil.artifacts`,
the same one every other producer emits, so the canary is flashed, verified,
listed, pinned and pruned by exactly the machinery that already exists.

    python canary/build_artifacts.py --out hil-canary [--target esp32-c6]
    python canary/build_artifacts.py --version
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rig"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from alteriom_hil.artifacts import sha256  # noqa: E402

FIRMWARE_DIR = Path(__file__).resolve().parent / "firmware"
DEFAULT_OUT = Path(os.environ.get("ALTERIOM_CANARY_ARTIFACT_DIR", "hil-canary"))

# The same families, boards and bootloader offsets as every other producer:
# silicon facts, kept in sync with the esptool defaults and with
# suites/painlessmesh/build_artifacts.TARGETS.
TARGETS = {
    "esp32": {"chip": "esp32", "board": "esp32dev", "bootloader": "0x1000", "layout": "esp32"},
    "esp32-c3": {"chip": "esp32c3", "board": "esp32-c3-devkitm-1", "bootloader": "0x0", "layout": "esp32"},
    "esp32-c5": {"chip": "esp32c5", "board": "esp32-c5-devkitc-1", "bootloader": "0x2000", "layout": "esp32"},
    "esp32-c6": {"chip": "esp32c6", "board": "esp32-c6-devkitc-1", "bootloader": "0x0", "layout": "esp32"},
    "esp32-s3": {"chip": "esp32s3", "board": "esp32-s3-devkitc-1", "bootloader": "0x0", "layout": "esp32"},
    "esp8266": {"chip": "esp8266", "board": "nodemcuv2", "bootloader": "0x0", "layout": "esp8266"},
}

# One PlatformIO core directory per family, all under one parent, exactly as
# the painlessMesh build does and for the same reason: the two Arduino cores
# ship a package of the same name, and in one directory whichever installs
# first keeps it. Override the parent with ALTERIOM_PIO_CORES.
PIO_CORES_DIRNAME = ".platformio-cores"


def core_dir_for(name: str) -> Path:
    root = Path(os.environ.get("ALTERIOM_PIO_CORES") or Path.home() / PIO_CORES_DIRNAME)
    return root / name


def canary_sha() -> str:
    """The digest of the canary's own source: this bundle's revision.

    Every file under `canary/firmware/`, path and bytes, so a change to the
    platformio.ini that picks a platform counts as much as a change to the
    firmware. A build directory is not source and is skipped.
    """
    digest = hashlib.sha256()
    for path in sorted(FIRMWARE_DIR.rglob("*")):
        if not path.is_file() or ".pio" in path.parts:
            continue
        digest.update(path.relative_to(FIRMWARE_DIR).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def farm_sha() -> str:
    """The farm commit this build ran from: the bundle's revision of record.

    From the checkout, because that is the source: the canary lives in this
    repository and a `farm` profile builds in the deployed tree. GITHUB_SHA
    is the fallback for a CI build on the simulation host, where the checkout
    may be a detached one git will still answer for -- and if neither can
    say, the build fails rather than recording a revision it invented, since
    a bundle whose revision is a guess is one nothing can reuse safely.
    """
    try:
        found = subprocess.run(
            ["git", "-C", str(FIRMWARE_DIR), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=20,
        )
        if found.returncode == 0 and found.stdout.strip():
            return found.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    fallback = (os.environ.get("GITHUB_SHA") or "").strip()
    if fallback:
        return fallback
    raise RuntimeError(
        "cannot determine the farm commit this canary is built from: git "
        "could not answer and GITHUB_SHA is unset"
    )


def firmware_version() -> str:
    """MAJOR.MINOR from VERSION, PATCH from the commits that changed the firmware.

    Counted over `canary/firmware/` only, so the number moves when the
    firmware does and not on every farm release. A modified or untracked file
    under the firmware directory is marked `+modified`: that build is not the
    version it would otherwise claim. A shallow checkout counts only the
    commits it fetched, and git that cannot answer counts nothing, so both
    fail the build rather than stamp a number that looks right and is not --
    the same rule `farm_sha` keeps for the commit.
    """
    try:
        base = "".join((FIRMWARE_DIR / "VERSION").read_text(encoding="utf-8").split())
    except OSError as exc:
        raise RuntimeError(f"canary/firmware/VERSION is missing: {exc}") from exc
    if not re.fullmatch(r"\d+\.\d+", base):
        raise RuntimeError(f"canary/firmware/VERSION must be MAJOR.MINOR, not {base!r}")

    def git(*args: str) -> str:
        found = subprocess.run(
            ["git", "-C", str(FIRMWARE_DIR), *args],
            capture_output=True, text=True, timeout=20,
        )
        if found.returncode != 0:
            raise RuntimeError(
                f"cannot number the Rig Health Check firmware: git {' '.join(args)} "
                f"failed: {found.stderr.strip() or found.returncode}"
            )
        return found.stdout.strip()

    try:
        if git("rev-parse", "--is-shallow-repository") == "true":
            raise RuntimeError(
                "cannot number the Rig Health Check firmware from a shallow "
                "checkout: fetch the full history (actions/checkout fetch-depth: 0)"
            )
        count = int(git("rev-list", "--count", "HEAD", "--", "."))
        modified = bool(git("status", "--porcelain", "--", "."))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise RuntimeError(f"cannot number the Rig Health Check firmware: {exc}") from exc
    return f"{base}.{count}" + ("+modified" if modified else "")


def normalize_targets(names: list[str] | None) -> list[str]:
    selected = sorted(set(names or TARGETS))
    unknown = set(selected) - set(TARGETS)
    if unknown:
        raise ValueError(
            f"unsupported canary target(s): {sorted(unknown)}; expected {sorted(TARGETS)}"
        )
    return selected


def _boot_app0(core_dir: Path) -> Path:
    """The fixed OTA-data image, from the core directory this family built in.

    From the family's own directory: with one per family there is no shared
    `~/.platformio` to fall back on, and a stray copy from another family's
    cache would defeat the isolation even though the bytes are identical
    everywhere.
    """
    candidates = sorted(
        core_dir.glob("packages/framework-arduinoespressif32*/tools/partitions/boot_app0.bin")
    )
    if not candidates:
        raise FileNotFoundError(f"PlatformIO boot_app0.bin was not installed under {core_dir}")
    return candidates[0]


def build_artifacts(out_dir: Path, names: list[str] | None = None) -> Path:
    selected = normalize_targets(names)
    pio = shutil.which("pio")
    if not pio:
        raise FileNotFoundError("pio is not on PATH")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    revision = canary_sha()
    commit = farm_sha()
    version = firmware_version()
    build_env = dict(os.environ, CANARY_SHA=revision, CANARY_VERSION=version)
    entries: dict = {}

    for name in selected:
        target = TARGETS[name]
        core_dir = core_dir_for(name)
        print(f"==> Rig Health Check {version} {name} ({target['board']}) in {core_dir}")
        subprocess.run(
            [pio, "run", "-d", str(FIRMWARE_DIR), "-e", name],
            check=True,
            env={**build_env, "PLATFORMIO_CORE_DIR": str(core_dir)},
        )
        pio_build = FIRMWARE_DIR / ".pio" / "build" / name
        target_dir = out_dir / name
        target_dir.mkdir(parents=True, exist_ok=True)
        if target["layout"] == "esp8266":
            # PlatformIO already emits one self-contained image at 0x0.
            components = {"firmware.bin": pio_build / "firmware.bin"}
            segments = {"firmware.bin": "0x0"}
        else:
            components = {
                "bootloader.bin": pio_build / "bootloader.bin",
                "partitions.bin": pio_build / "partitions.bin",
                "boot_app0.bin": _boot_app0(core_dir),
                "firmware.bin": pio_build / "firmware.bin",
            }
            segments = {
                "bootloader.bin": target["bootloader"],
                "partitions.bin": "0x8000",
                "boot_app0.bin": "0xe000",
                "firmware.bin": "0x10000",
            }
        for filename, source in components.items():
            if not source.is_file():
                raise FileNotFoundError(f"missing build component: {source}")
            shutil.copy2(source, target_dir / filename)

        merged = target_dir / "flash-image.bin"
        if target["layout"] == "esp8266":
            shutil.copy2(target_dir / "firmware.bin", merged)
        else:
            merge = [
                sys.executable, "-m", "esptool", "--chip", target["chip"],
                "merge-bin", "-o", str(merged),
            ]
            for filename, offset in segments.items():
                merge.extend((offset, str(target_dir / filename)))
            subprocess.run(merge, check=True)
        files = {
            path.name: {"sha256": sha256(path), "size": path.stat().st_size}
            for path in sorted(target_dir.glob("*.bin"))
        }
        entries[name] = {
            "platformio_env": name,
            "board": target["board"],
            "chip": target["chip"],
            "flash_offset": "0x0",
            "image": f"{name}/flash-image.bin",
            "sha256": files["flash-image.bin"]["sha256"],
            "files": files,
            "segments": segments,
        }

    manifest = out_dir / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": 2,
                # What this bundle is, so the store and the dashboard can say
                # so without consulting the job that built it.
                "producer": "canary",
                # The revision of record, and the firmware's own identity.
                "farm_sha": commit,
                "canary_sha": revision,
                # The number a person reads, and the one a board reports.
                "version": version,
                "targets": entries,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Rig Health Check {version} manifest: {manifest}")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--target", action="append", choices=sorted(TARGETS))
    # Accepted and ignored: the farm renders one build command for every
    # profile, and the canary's revision is its own source rather than a ref.
    parser.add_argument("--ref", default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--revision", action="store_true",
        help="print the canary source digest and build nothing (a deploy's cache key)",
    )
    parser.add_argument(
        "--version", action="store_true", dest="print_version",
        help="print the firmware version this checkout builds, and build nothing",
    )
    args = parser.parse_args(argv)
    if args.print_version:
        print(firmware_version())
        return 0
    if args.revision:
        print(canary_sha())
        return 0
    build_artifacts(args.out, args.target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
