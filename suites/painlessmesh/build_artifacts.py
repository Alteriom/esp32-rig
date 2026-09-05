#!/usr/bin/env python3
"""Build one immutable, merged flash image per ESP32 MCU family."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

FIRMWARE_DIR = Path(__file__).resolve().parent / "firmware"
DEFAULT_OUT = Path(os.environ.get("ALTERIOM_HIL_ARTIFACT_DIR", "hil-firmware"))


@dataclass(frozen=True)
class Target:
    env: str
    chip: str
    board: str
    bootloader_offset: str
    # "esp32": bootloader + partition table + OTA boot selector + app, merged
    # into one image at offset 0. "esp8266": PlatformIO already emits one
    # self-contained image that flashes at 0x0; nothing to merge.
    layout: str = "esp32"


# Bootloader offsets are silicon facts: 0x1000 on the original ESP32, 0x0 on
# C3/C6/S3, 0x2000 on C5. Keep them in sync with the esptool defaults.
TARGETS = {
    "esp32": Target("esp32", "esp32", "esp32dev", "0x1000"),
    "esp32-c3": Target("esp32-c3", "esp32c3", "esp32-c3-devkitm-1", "0x0"),
    "esp32-c5": Target("esp32-c5", "esp32c5", "esp32-c5-devkitc-1", "0x2000"),
    "esp32-c6": Target("esp32-c6", "esp32c6", "esp32-c6-devkitc-1", "0x0"),
    "esp32-s3": Target("esp32-s3", "esp32s3", "esp32-s3-devkitc-1", "0x0"),
    "esp8266": Target("esp8266", "esp8266", "nodemcuv2", "0x0", layout="esp8266"),
}


# One PlatformIO core directory per MCU family, all under one parent so a
# single path can be granted to the farm service, cached by CI, and cleaned.
# Override the parent with ALTERIOM_PIO_CORES.
PIO_CORES_DIRNAME = ".platformio-cores"


def core_dir_for(name: str) -> Path:
    """The PlatformIO core directory a family builds in. Nothing is shared.

    One directory per family under ~/.platformio-cores, so esp32-c5 builds
    in ~/.platformio-cores/esp32-c5. Two reasons, one forced and one chosen.

    Forced: the pinned `espressif32` platform and pioarduino's both ship a
    package *named* `framework-arduinoespressif32` — Arduino core 2.x and
    3.x. PlatformIO treats that requirement as satisfied by name and then
    resolves it by spec, so in one directory whichever installs first keeps
    it, the other platform's core is never reinstalled, and its builder gets
    None for the framework and dies with `TypeError: argument should be a
    str ... not NoneType`, naming nothing that leads back here. The state
    cannot be repaired from outside: asking PlatformIO to reinstall the
    missing core does nothing, because the name is already satisfied.

    Chosen: a per-family directory is what makes a build reproducible from
    what this repository declares. A directory shared between families
    carries whatever an earlier family installed into it, so the same commit
    can build differently depending on what ran before it on that host —
    which is exactly how this bug stayed invisible for so long.

    The cost is disk and a slow first build per family; both are cheap next
    to a build whose result depends on its host's history.

    They live under one parent so there is a single path to grant, cache, and
    clean: the farm service runs with ProtectHome=read-only and an explicit
    ReadWritePaths, and sibling directories would each need granting.
    """
    root = Path(os.environ.get("ALTERIOM_PIO_CORES") or Path.home() / PIO_CORES_DIRNAME)
    return root / name


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def agent_source_sha() -> str:
    """Identify the HIL agent source independently of the library under test."""
    digest = hashlib.sha256()
    for path in sorted(FIRMWARE_DIR.rglob("*")):
        if not path.is_file() or ".pio" in path.parts:
            continue
        relative = path.relative_to(FIRMWARE_DIR).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def normalize_targets(names: list[str] | None) -> list[str]:
    selected = sorted(set(names or TARGETS))
    unknown = set(selected) - set(TARGETS)
    if unknown:
        raise ValueError(
            f"unsupported artifact target(s): {sorted(unknown)}; "
            f"expected {sorted(TARGETS)}"
        )
    return selected


def _boot_app0(core_dir: Path) -> Path:
    """The fixed OTA-data image, from the core directory this family built in.

    It must come from the family's own core directory: with one directory per
    family there is no shared `~/.platformio` left to fall back on, and a
    stray copy from another family's cache would defeat the isolation even
    though the file itself is identical everywhere.
    """
    candidates = sorted(
        core_dir.glob("packages/framework-arduinoespressif32*/tools/partitions/boot_app0.bin")
    )
    if not candidates:
        raise FileNotFoundError(f"PlatformIO boot_app0.bin was not installed under {core_dir}")
    return candidates[0]


def checkout_painlessmesh(ref: str) -> tuple[Path, str]:
    """Fetch exactly ``ref`` once and return its detached source + SHA."""
    source = FIRMWARE_DIR / ".pio" / "painlessmesh-source"
    source.mkdir(parents=True, exist_ok=True)
    git_dir = source / ".git"
    if not git_dir.is_dir():
        subprocess.run(["git", "init", str(source)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "remote",
                "add",
                "origin",
                "https://github.com/Alteriom/painlessMesh.git",
            ],
            check=True,
        )
    fetch = subprocess.run(
        ["git", "-C", str(source), "fetch", "--depth", "1", "origin", ref],
        capture_output=True,
        text=True,
    )
    if fetch.returncode:
        # The common cause is a ref that no longer exists — a branch deleted
        # when its pull request merged, most often. Say that, rather than
        # raising a CalledProcessError whose traceback buries the ref among
        # subprocess internals and reads like a farm fault.
        raise RuntimeError(
            f"cannot fetch painlessMesh ref {ref!r}: it does not exist on the "
            f"remote, or the host cannot reach GitHub. git said: "
            f"{fetch.stderr.strip().splitlines()[-1] if fetch.stderr.strip() else 'nothing'}"
        )
    subprocess.run(
        ["git", "-C", str(source), "checkout", "--detach", "--force", "FETCH_HEAD"],
        check=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return source.resolve(), sha


def build_artifacts(ref: str, out_dir: Path, names: list[str] | None = None) -> Path:
    selected = normalize_targets(names)
    pio = shutil.which("pio")
    if not pio:
        raise FileNotFoundError("pio is not on PATH")
    out_dir.mkdir(parents=True, exist_ok=True)
    entries = {}
    source_dir, source_sha = checkout_painlessmesh(ref)
    hil_agent_sha = agent_source_sha()
    build_env = dict(
        os.environ,
        PAINLESSMESH_REF=source_sha,
        PAINLESSMESH_DIR=str(source_dir),
        HIL_AGENT_SHA=hil_agent_sha,
        HIL_OTA_GENERATION="1",
    )

    for name in selected:
        target = TARGETS[name]
        core_dir = core_dir_for(name)
        print(f"==> build {name} ({target.board}) with painlessMesh@{ref} in {core_dir}")
        subprocess.run(
            [pio, "run", "-d", str(FIRMWARE_DIR), "-e", target.env],
            check=True,
            env={**build_env, "PLATFORMIO_CORE_DIR": str(core_dir)},
        )
        pio_build = FIRMWARE_DIR / ".pio" / "build" / target.env
        target_dir = out_dir / name
        target_dir.mkdir(parents=True, exist_ok=True)
        if target.layout == "esp8266":
            components = {"firmware.bin": pio_build / "firmware.bin"}
            segment_offsets = {"firmware.bin": "0x0"}
        else:
            components = {
                "bootloader.bin": pio_build / "bootloader.bin",
                "partitions.bin": pio_build / "partitions.bin",
                "boot_app0.bin": _boot_app0(core_dir),
                "firmware.bin": pio_build / "firmware.bin",
            }
            segment_offsets = {
                "bootloader.bin": target.bootloader_offset,
                "partitions.bin": "0x8000",
                "boot_app0.bin": "0xe000",
                "firmware.bin": "0x10000",
            }
        for filename, source in components.items():
            if not source.is_file():
                raise FileNotFoundError(f"missing build component: {source}")
            shutil.copy2(source, target_dir / filename)

        ota = None
        if name == "esp32":
            ota_env = dict(build_env, HIL_OTA_GENERATION="2")
            subprocess.run(
                [pio, "run", "-d", str(FIRMWARE_DIR), "-e", target.env],
                check=True,
                env=ota_env,
            )
            ota_image = target_dir / "ota-firmware.bin"
            shutil.copy2(pio_build / "firmware.bin", ota_image)
            ota = {
                "generation": 2,
                "hardware": "ESP32",
                "image": f"{name}/ota-firmware.bin",
                "sha256": sha256(ota_image),
                "size": ota_image.stat().st_size,
            }

        merged = target_dir / "flash-image.bin"
        if target.layout == "esp8266":
            shutil.copy2(target_dir / "firmware.bin", merged)
        else:
            merge = [sys.executable, "-m", "esptool", "--chip", target.chip, "merge-bin", "-o", str(merged)]
            for filename, offset in segment_offsets.items():
                merge.extend((offset, str(target_dir / filename)))
            subprocess.run(merge, check=True)
        files = {
            path.name: {"sha256": sha256(path), "size": path.stat().st_size}
            for path in sorted(target_dir.glob("*.bin"))
        }
        entries[name] = {
            "platformio_env": target.env,
            "board": target.board,
            "chip": target.chip,
            "flash_offset": "0x0",
            "image": f"{name}/flash-image.bin",
            "sha256": files["flash-image.bin"]["sha256"],
            "files": files,
            "segments": segment_offsets,
        }
        if ota is not None:
            entries[name]["ota"] = ota

    manifest = out_dir / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": 2,
                "hil_agent_sha": hil_agent_sha,
                "painlessmesh_ref": ref,
                "painlessmesh_sha": source_sha,
                "targets": entries,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"artifact manifest: {manifest}")
    return manifest


def load_artifacts(directory: Path) -> dict[str, dict]:
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != 2:
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default=os.environ.get("PAINLESSMESH_REF") or "main")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--target", action="append", choices=sorted(TARGETS))
    args = parser.parse_args(argv)
    build_artifacts(args.ref, args.out, args.target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
