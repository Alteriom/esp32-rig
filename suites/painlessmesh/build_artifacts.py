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


TARGETS = {
    "esp32": Target("esp32", "esp32", "esp32dev", "0x1000"),
    "esp32-c3": Target(
        "esp32-c3", "esp32c3", "esp32-c3-devkitm-1", "0x0"
    ),
    "esp32-s3": Target(
        "esp32-s3", "esp32s3", "esp32-s3-devkitc-1", "0x0"
    ),
}


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


def _boot_app0() -> Path:
    core_dir = Path(os.environ.get("PLATFORMIO_CORE_DIR", Path.home() / ".platformio"))
    candidates = list(
        core_dir.glob("packages/framework-arduinoespressif32/tools/partitions/boot_app0.bin")
    )
    if not candidates:
        raise FileNotFoundError("PlatformIO boot_app0.bin was not installed")
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
    subprocess.run(
        ["git", "-C", str(source), "fetch", "--depth", "1", "origin", ref],
        check=True,
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
    )

    for name in selected:
        target = TARGETS[name]
        print(f"==> build {name} ({target.board}) with painlessMesh@{ref}")
        subprocess.run(
            [pio, "run", "-d", str(FIRMWARE_DIR), "-e", target.env],
            check=True,
            env=build_env,
        )
        pio_build = FIRMWARE_DIR / ".pio" / "build" / target.env
        target_dir = out_dir / name
        target_dir.mkdir(parents=True, exist_ok=True)
        components = {
            "bootloader.bin": pio_build / "bootloader.bin",
            "partitions.bin": pio_build / "partitions.bin",
            "boot_app0.bin": _boot_app0(),
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

        merged = target_dir / "flash-image.bin"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "esptool",
                "--chip",
                target.chip,
                "merge-bin",
                "-o",
                str(merged),
                target.bootloader_offset,
                str(target_dir / "bootloader.bin"),
                "0x8000",
                str(target_dir / "partitions.bin"),
                "0xe000",
                str(target_dir / "boot_app0.bin"),
                "0x10000",
                str(target_dir / "firmware.bin"),
            ],
            check=True,
        )
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
