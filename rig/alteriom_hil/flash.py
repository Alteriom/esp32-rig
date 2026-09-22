"""Flash a firmware image onto a board.

Two strategies:

- ``flash_pio``: `pio run -t upload` in a PlatformIO project dir with
  ``--upload-port`` — used by the painlessMesh suite, whose firmware is a
  PlatformIO project. PlatformIO handles bootloader/partition images.
- ``flash_esptool``: raw `esptool.py write_flash` of a prebuilt .bin — for
  suites that ship binaries.

Both return the completed process and raise on failure.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .board import Board


def flash_pio(
    project_dir: str | Path,
    board: Board,
    env: str = "esp32dev",
    extra_env: dict | None = None,
    timeout: int = 600,
) -> subprocess.CompletedProcess:
    import os

    run_env = dict(os.environ)
    if extra_env:
        run_env.update(extra_env)
    return subprocess.run(
        [
            "pio",
            "run",
            "-d",
            str(project_dir),
            "-e",
            env,
            "-t",
            "upload",
            "--upload-port",
            board.port,
        ],
        check=True,
        env=run_env,
        timeout=timeout,
        capture_output=True,
        text=True,
    )


def flash_esptool(
    image: str | Path,
    board: Board,
    offset: str = "0x10000",
    timeout: int = 300,
) -> subprocess.CompletedProcess:
    bauds = [board.flash_baud]
    if board.flash_baud != 115200:
        bauds.append(115200)
    last_error = None
    for baud in bauds:
        try:
            return subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "esptool",
                    "--chip",
                    board.chip,
                    "--port",
                    board.port,
                    "--baud",
                    str(baud),
                    "write-flash",
                    offset,
                    str(image),
                ],
                check=True,
                timeout=timeout,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            last_error = exc
    assert last_error is not None
    raise last_error
