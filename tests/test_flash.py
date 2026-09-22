import subprocess

import pytest

from alteriom_hil.board import Board
from alteriom_hil.flash import flash_esptool


def test_esptool_retries_at_safe_baud(monkeypatch, tmp_path):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[args.index("--baud") + 1] == "460800":
            raise subprocess.CalledProcessError(2, args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = flash_esptool(tmp_path / "image.bin", Board("board", "/dev/test"))

    assert result.returncode == 0
    assert [args[args.index("--baud") + 1] for args in calls] == ["460800", "115200"]
    assert all("write-flash" in args for args in calls)


def test_esptool_raises_after_both_bauds_fail(monkeypatch, tmp_path):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        raise subprocess.CalledProcessError(2, args)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        flash_esptool(tmp_path / "image.bin", Board("board", "/dev/test"))

    assert len(calls) == 2
