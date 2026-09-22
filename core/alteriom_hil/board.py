"""Board inventory: parse and validate the rig's board map YAML."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import re

import yaml

from .devices import load_families

# target = artifact family / PlatformIO environment; chip = esptool chip name.
# Read from the family descriptors (alteriom_hil/devices/families). Adding a
# family: its descriptor, one Target in suites/painlessmesh/build_artifacts.py,
# one [env:<target>] in the firmware platformio.ini, and a row in
# docs/firmware-artifacts.md. Discovery, the farm service, the admin CLI, and
# the board map all read this table.
TARGET_CHIPS = {name: family.chip for name, family in sorted(load_families().items())}
SUPPORTED_TARGETS = frozenset(TARGET_CHIPS)


@dataclass
class Board:
    """One physical board attached to the rig."""

    id: str  # rig-local name, e.g. "esp32-01"
    port: str  # serial device, e.g. /dev/esp32-farm-01 (udev symlink)
    chip: str = "esp32"  # esptool chip type
    baud: int = 115200  # monitor baud rate
    flash_baud: int = 460800
    target: str = "esp32"  # artifact family / PlatformIO environment
    mac: Optional[str] = None  # stable silicon identity, populated by discovery
    usb_path: Optional[str] = None  # current physical hub path (diagnostic only)
    # Optional power-control coordinates (uhubctl): which hub + port
    power_hub: Optional[str] = None
    power_port: Optional[int] = None
    tags: list = field(default_factory=list)

    def __post_init__(self):
        if not self.id:
            raise ValueError("board id must be non-empty")
        if not self.port:
            raise ValueError(f"board {self.id}: port must be non-empty")
        if self.target not in SUPPORTED_TARGETS:
            raise ValueError(
                f"board {self.id}: unsupported target {self.target!r}; "
                f"expected one of {sorted(SUPPORTED_TARGETS)}"
            )
        expected_chip = TARGET_CHIPS[self.target]
        if self.chip != expected_chip:
            raise ValueError(
                f"board {self.id}: target {self.target!r} requires chip "
                f"{expected_chip!r}, got {self.chip!r}"
            )
        if self.mac is not None:
            normalized = self.mac.strip().lower().replace("-", ":")
            if not re.fullmatch(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}", normalized):
                raise ValueError(f"board {self.id}: invalid MAC address {self.mac!r}")
            self.mac = normalized


class BoardMap:
    """The rig's board inventory, loaded from YAML.

    Format:

        boards:
          - id: esp32-01
            port: /dev/esp32-farm-01
            chip: esp32
            target: esp32
            power_hub: "1-1"
            power_port: 1
            tags: [mesh]
    """

    def __init__(self, boards: list[Board]):
        ids = [b.id for b in boards]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate board ids in board map: {ids}")
        ports = [b.port for b in boards]
        if len(ports) != len(set(ports)):
            raise ValueError(f"duplicate ports in board map: {ports}")
        macs = [b.mac for b in boards if b.mac]
        if len(macs) != len(set(macs)):
            raise ValueError(f"duplicate MAC addresses in board map: {macs}")
        power_ports = [
            (b.power_hub, b.power_port)
            for b in boards
            if b.power_hub is not None and b.power_port is not None
        ]
        if len(power_ports) != len(set(power_ports)):
            raise ValueError(f"duplicate power coordinates in board map: {power_ports}")
        self.boards = boards

    @classmethod
    def load(cls, path: str | Path) -> "BoardMap":
        raw = yaml.safe_load(Path(path).read_text())
        if not raw or "boards" not in raw or not raw["boards"]:
            raise ValueError(f"board map {path} has no boards")
        boards = [Board(**entry) for entry in raw["boards"]]
        return cls(boards)

    def __len__(self) -> int:
        return len(self.boards)

    def __iter__(self):
        return iter(self.boards)

    def get(self, board_id: str) -> Board:
        for b in self.boards:
            if b.id == board_id:
                return b
        raise KeyError(f"no board {board_id!r} in board map")

    def with_tag(self, tag: str) -> list[Board]:
        return [b for b in self.boards if tag in b.tags]
