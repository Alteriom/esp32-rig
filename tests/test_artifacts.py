import json
from pathlib import Path

import pytest

from suites.painlessmesh.build_artifacts import (
    TARGETS,
    agent_source_sha,
    load_artifacts,
    normalize_targets,
    sha256,
)


def test_hil_agent_source_identity_is_stable_and_complete():
    digest = agent_source_sha()
    assert len(digest) == 64
    assert digest == agent_source_sha()


def test_target_matrix_matches_the_hal_family_table():
    from alteriom_hil.board import TARGET_CHIPS

    assert {name: target.chip for name, target in TARGETS.items()} == TARGET_CHIPS
    assert all(target.env == name for name, target in TARGETS.items())
    # Silicon facts: bootloader offsets and the one family without a merged image.
    assert TARGETS["esp32"].bootloader_offset == "0x1000"
    assert TARGETS["esp32-c5"].bootloader_offset == "0x2000"
    for name in ("esp32-c3", "esp32-c6", "esp32-s3"):
        assert TARGETS[name].bootloader_offset == "0x0"
    assert TARGETS["esp8266"].layout == "esp8266"
    assert all(t.layout == "esp32" for n, t in TARGETS.items() if n != "esp8266")


def test_firmware_project_defines_one_environment_per_family():
    import configparser
    from pathlib import Path

    ini = configparser.ConfigParser(interpolation=None)
    ini.read(Path(__file__).resolve().parents[1] / "suites/painlessmesh/firmware/platformio.ini")
    envs = {name[len("env:"):] for name in ini.sections() if name.startswith("env:")}
    assert envs == set(TARGETS)
    for name, target in TARGETS.items():
        section = ini[f"env:{name}"]
        assert section["board"] == target.board
        assert f'HIL_ARTIFACT_TARGET=\\"{name}\\"' in section["build_flags"]
    assert ini["env:esp8266"]["platform"] == "espressif8266"
    assert "USE_FS_LITTLEFS" in ini["env:esp8266"]["build_flags"]
    for name in ("esp32-c3", "esp32-c5", "esp32-c6", "esp32-s3"):
        assert "${native_usb.build_flags}" in ini[f"env:{name}"]["build_flags"], name
    # The pinned platform is Arduino core 2.x: C5 and C6 need core 3.x (pioarduino).
    assert ini["esp32_common"]["platform"] == "espressif32@7.0.1"
    assert "pioarduino/platform-espressif32" in ini["esp32_arduino3"]["platform"]
    for name in ("esp32-c5", "esp32-c6"):
        assert ini[f"env:{name}"]["extends"] == "esp32_arduino3", name
    assert ini["esp32_arduino3"]["lib_ldf_mode"] == "deep", "deep+ drops core 3.x's guarded Network.h include"
    assert ini["esp32_arduino3"]["board_build.partitions"] == "min_spiffs.csv", "core 3.x images overflow default.csv"
    for name in ("esp32", "esp32-c3", "esp32-s3"):
        assert ini[f"env:{name}"]["extends"] == "esp32_common", name


def test_target_selection_is_unique_and_deterministic():
    assert normalize_targets(["esp32-s3", "esp32", "esp32-s3"]) == [
        "esp32",
        "esp32-s3",
    ]
    with pytest.raises(ValueError, match="unsupported artifact"):
        normalize_targets(["esp32-h2"])


def test_manifest_loader_verifies_immutable_image(tmp_path):
    image_dir = tmp_path / "esp32-c3"
    image_dir.mkdir()
    image = image_dir / "flash-image.bin"
    image.write_bytes(b"one compiled image")
    manifest = {
        "schema": 2,
        "painlessmesh_ref": "abc123",
        "targets": {
            "esp32-c3": {
                "image": "esp32-c3/flash-image.bin",
                "sha256": sha256(image),
                "files": {},
                "segments": {},
            }
        },
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    loaded = load_artifacts(tmp_path)
    assert loaded["targets"]["esp32-c3"]["path"] == image

    image.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum mismatch"):
        load_artifacts(tmp_path)


def test_manifest_loader_verifies_ota_candidate(tmp_path):
    image_dir = tmp_path / "esp32"
    image_dir.mkdir()
    image = image_dir / "flash-image.bin"
    ota = image_dir / "ota-firmware.bin"
    image.write_bytes(b"normal")
    ota.write_bytes(b"generation two")
    manifest = {
        "schema": 2,
        "targets": {
            "esp32": {
                "image": "esp32/flash-image.bin",
                "sha256": sha256(image),
                "files": {},
                "segments": {},
                "ota": {
                    "image": "esp32/ota-firmware.bin",
                    "sha256": sha256(ota),
                    "size": ota.stat().st_size,
                    "generation": 2,
                },
            }
        },
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    loaded = load_artifacts(tmp_path)
    assert loaded["targets"]["esp32"]["ota"]["path"] == ota

    ota.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="OTA artifact checksum mismatch"):
        load_artifacts(tmp_path)


def test_flash_all_flashes_each_family_image_onto_every_matching_board(tmp_path, monkeypatch):
    from suites.painlessmesh import flash_all

    board_map = tmp_path / "boards.yaml"
    board_map.write_text(
        """boards:
  - {id: e1, port: /dev/e1, chip: esp32, target: esp32}
  - {id: e2, port: /dev/e2, chip: esp32, target: esp32}
  - {id: c1, port: /dev/c1, chip: esp32c3, target: esp32-c3}
"""
    )
    images = {name: tmp_path / f"{name}.bin" for name in ("esp32", "esp32-c3")}
    for image in images.values():
        image.write_bytes(b"image")
    flash_calls = []
    monkeypatch.setenv("ALTERIOM_HIL_BOARD_MAP", str(board_map))
    monkeypatch.setattr(
        flash_all,
        "load_artifacts",
        lambda directory: {
            "painlessmesh_sha": "deadbeef",
            "targets": {
                name: {
                    "path": path,
                    "flash_offset": "0x0",
                    "sha256": name * 8,
                }
                for name, path in images.items()
            }
        },
    )
    monkeypatch.setattr(
        flash_all,
        "flash_esptool",
        lambda image, board, offset: flash_calls.append((image, board.id, offset)),
    )

    assert flash_all.main(["--artifacts", str(tmp_path)]) == 0
    assert flash_calls == [
        (images["esp32"], "e1", "0x0"),
        (images["esp32"], "e2", "0x0"),
        (images["esp32-c3"], "c1", "0x0"),
    ]


def test_flash_all_has_no_build_mode(tmp_path):
    """The farm does not build firmware. The bundle is painlessMesh CI's, and
    a flag that once compiled on the rig is refused rather than ignored."""
    from suites.painlessmesh import flash_all

    assert not hasattr(flash_all, "build_artifacts")
    for retired in (["--skip-build"], ["--ref", "main"]):
        with pytest.raises(SystemExit):
            flash_all.main(["--artifacts", str(tmp_path), *retired])


def _flash_all_with_one_board(tmp_path, monkeypatch, flash):
    from suites.painlessmesh import flash_all

    board_map = tmp_path / "boards.yaml"
    board_map.write_text(
        "boards:\n  - {id: e02, port: /dev/e02, chip: esp32, target: esp32}\n"
    )
    image = tmp_path / "esp32.bin"
    image.write_bytes(b"image")
    monkeypatch.setenv("ALTERIOM_HIL_BOARD_MAP", str(board_map))
    monkeypatch.setattr(
        flash_all,
        "load_artifacts",
        lambda directory: {
            "painlessmesh_sha": "d9ad664",
            "targets": {
                "esp32": {"path": image, "flash_offset": "0x0", "sha256": "b" * 64}
            },
        },
    )
    monkeypatch.setattr(flash_all, "flash_esptool", flash)
    monkeypatch.setattr(flash_all.time, "sleep", lambda seconds: None)
    return flash_all.main(["--artifacts", str(tmp_path)])


def test_flash_all_retries_a_board_once_after_a_transient_failure(tmp_path, monkeypatch):
    # esptool lost the serial stream once mid-write on a UART-bridge board —
    # the only such failure in dozens of suites — and the run lost its
    # twenty-five minutes before a test had started.
    import subprocess

    attempts = []

    def flaky(artifact, board, offset):
        attempts.append(board.id)
        if len(attempts) == 1:
            raise subprocess.CalledProcessError(
                2, ["esptool"], "", "Serial data stream stopped"
            )

    assert _flash_all_with_one_board(tmp_path, monkeypatch, flaky) == 0
    assert attempts == ["e02", "e02"]


def test_flash_all_still_fails_a_board_that_fails_twice(tmp_path, monkeypatch):
    import subprocess

    attempts = []

    def broken(artifact, board, offset):
        attempts.append(board.id)
        raise subprocess.CalledProcessError(2, ["esptool"], "", "no response")

    assert _flash_all_with_one_board(tmp_path, monkeypatch, broken) == 1
    assert attempts == ["e02", "e02"]


def test_every_pio_run_builds_in_the_family_core_directory(tmp_path, monkeypatch):
    # The OTA-generation build of esp32 ran without PLATFORMIO_CORE_DIR and
    # fell back to ~/.platformio: empty on the farm host, so every run died
    # in its build stage, and stale-but-present on the sim host, so CI passed.
    from suites.painlessmesh import build_artifacts

    firmware = tmp_path / "firmware"
    (firmware / ".pio" / "build" / "esp32").mkdir(parents=True)
    core = tmp_path / "cores" / "esp32"
    boot_app0 = core / "packages" / "framework-arduinoespressif32" / "tools" / "partitions"
    boot_app0.mkdir(parents=True)
    (boot_app0 / "boot_app0.bin").write_bytes(b"boot")
    monkeypatch.setenv("ALTERIOM_PIO_CORES", str(tmp_path / "cores"))
    monkeypatch.setattr(build_artifacts, "FIRMWARE_DIR", firmware)
    monkeypatch.setattr(build_artifacts, "checkout_painlessmesh", lambda ref: (tmp_path / "src", "a" * 40))
    monkeypatch.setattr(build_artifacts, "agent_source_sha", lambda: "b" * 64)
    monkeypatch.setattr(build_artifacts.shutil, "which", lambda name: "/fake/pio")
    runs = []

    def fake_run(args, check=True, env=None, **kwargs):
        if args[0] == "/fake/pio":
            runs.append(env)
            build = firmware / ".pio" / "build" / "esp32"
            for name in ("bootloader.bin", "partitions.bin", "firmware.bin"):
                (build / name).write_bytes(f"{name}:{env['HIL_OTA_GENERATION']}".encode())
        else:  # esptool merge-bin
            Path(args[args.index("-o") + 1]).write_bytes(b"merged")

    monkeypatch.setattr(build_artifacts.subprocess, "run", fake_run)
    manifest = build_artifacts.build_artifacts("main", tmp_path / "out", ["esp32"])
    assert len(runs) == 2, "image build and OTA-generation build"
    assert [env["HIL_OTA_GENERATION"] for env in runs] == ["1", "2"]
    assert {env["PLATFORMIO_CORE_DIR"] for env in runs} == {str(core)}
    assert (tmp_path / "out" / "esp32" / "ota-firmware.bin").read_bytes() == b"firmware.bin:2"
    assert manifest.exists()
