import pytest

from alteriom_hil.board import Board, BoardMap


def write_map(tmp_path, text):
    p = tmp_path / "board-map.yaml"
    p.write_text(text)
    return p


def test_load_valid_map(tmp_path):
    p = write_map(
        tmp_path,
        """
boards:
  - id: esp32-01
    port: /dev/esp32-farm-01
    power_hub: "1-1"
    power_port: 1
    tags: [mesh]
  - id: esp32-02
    port: /dev/esp32-farm-02
""",
    )
    bm = BoardMap.load(p)
    assert len(bm) == 2
    b1 = bm.get("esp32-01")
    assert b1.port == "/dev/esp32-farm-01"
    assert b1.chip == "esp32"
    assert b1.power_port == 1
    assert bm.with_tag("mesh") == [b1]


def test_duplicate_ids_rejected(tmp_path):
    p = write_map(
        tmp_path,
        """
boards:
  - id: esp32-01
    port: /dev/a
  - id: esp32-01
    port: /dev/b
""",
    )
    with pytest.raises(ValueError, match="duplicate board ids"):
        BoardMap.load(p)


def test_duplicate_ports_rejected(tmp_path):
    p = write_map(
        tmp_path,
        """
boards:
  - id: esp32-01
    port: /dev/a
  - id: esp32-02
    port: /dev/a
""",
    )
    with pytest.raises(ValueError, match="duplicate ports"):
        BoardMap.load(p)


def test_duplicate_power_coordinates_rejected(tmp_path):
    p = write_map(
        tmp_path,
        """boards:
  - {id: esp32-01, port: /dev/a, power_hub: '1-1', power_port: 1}
  - {id: esp32-02, port: /dev/b, power_hub: '1-1', power_port: 1}
""",
    )
    with pytest.raises(ValueError, match="duplicate power coordinates"):
        BoardMap.load(p)


def test_empty_map_rejected(tmp_path):
    p = write_map(tmp_path, "boards: []\n")
    with pytest.raises(ValueError, match="no boards"):
        BoardMap.load(p)


def test_missing_board_lookup():
    bm = BoardMap([Board(id="a", port="/dev/a")])
    with pytest.raises(KeyError):
        bm.get("nope")


def test_accepts_all_supported_artifact_targets(tmp_path):
    p = write_map(
        tmp_path,
        """boards:
  - {id: classic, port: /dev/a, chip: esp32, target: esp32}
  - {id: c3, port: /dev/b, chip: esp32c3, target: esp32-c3}
  - {id: s3, port: /dev/c, chip: esp32s3, target: esp32-s3}
""",
    )
    rig = BoardMap.load(p)
    assert [board.target for board in rig] == ["esp32", "esp32-c3", "esp32-s3"]


def test_rejects_unknown_artifact_target():
    with pytest.raises(ValueError, match="unsupported target"):
        Board(id="bad", port="/dev/bad", target="esp32-h2")


def test_rejects_target_chip_mismatch():
    with pytest.raises(ValueError, match="requires chip 'esp32c3'"):
        Board(id="bad-c3", port="/dev/bad", target="esp32-c3", chip="esp32")


def test_rejects_duplicate_stable_identity():
    with pytest.raises(ValueError, match="duplicate MAC"):
        BoardMap([
            Board(id="a", port="/dev/a", mac="aa:bb:cc:dd:ee:ff"),
            Board(id="b", port="/dev/b", mac="AA-BB-CC-DD-EE-FF"),
        ])
