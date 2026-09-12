"""Is this ESP healthy: it boots and says what it is, its serial path is
clean, its flash keeps a value, and it restarts when the rig says so."""

from __future__ import annotations

import pytest

from conftest import RUN_TAG

pytestmark = [
    # A canary failure is never a library's bug: there is no library in it.
    # It is this board, or the rig around it.
    pytest.mark.failure_class("flaky_hardware"),
]


@pytest.mark.hil_only(reason="peripheral")
@pytest.mark.capability("esp.boot")
def test_the_board_boots_and_says_what_it_is(board, canary):
    """The first question about any board, and the one every later check
    depends on: it started, it is talking, and the part answering is the
    part the registry says is there."""
    info = board.ensure_responsive()
    assert info.get("bootId"), f"{canary} answered info with no boot id"
    assert info.get("family"), f"{canary} did not say which family it was built for"
    assert info.get("canarySha"), f"{canary} did not say which canary it is running"
    silicon = info.get("silicon") or {}
    assert silicon.get("chip"), f"{canary} did not name its chip"
    # A part that answers but reports no heap and no flash is one whose
    # ROM calls are failing -- worth catching here rather than as a strange
    # failure in somebody's run three hours later.
    assert int(info.get("freeHeap") or 0) > 0, f"{canary} reports no free heap"
    assert int(silicon.get("flashBytes") or 0) > 0, f"{canary} reports no flash"
    assert info.get("resetReason"), f"{canary} did not say why it last started"


@pytest.mark.hil_only(reason="peripheral")
@pytest.mark.capability("esp.serial")
def test_the_serial_path_returns_a_long_line_byte_for_byte(board, canary):
    """The cable, the hub port and the console driver, checked together.

    A long line is the case that fails: a marginal cable or an overrun
    receive buffer drops or mangles bytes somewhere past the first few
    hundred, and every symptom of that in a consumer's run looks like a
    firmware fault instead.
    """
    board.ensure_responsive()
    # Every printable byte the framing allows, repeated to a length that
    # does not fit one UART FIFO, and ending in a marker so a truncation
    # shows as a difference rather than a timeout.
    alphabet = "".join(chr(code) for code in range(0x21, 0x7F) if chr(code) not in '"\\')
    text = (alphabet * 12)[:1024] + f"-{RUN_TAG}"
    reply = board.echo(text)
    assert reply.get("ok", True), reply
    returned = reply.get("text", "")
    assert len(returned) == len(text), (
        f"{canary} returned {len(returned)} of {len(text)} bytes: the serial "
        f"path is dropping data"
    )
    assert returned == text, f"{canary} returned a different line than it was sent"
    assert int(reply.get("len") or 0) == len(text)


@pytest.mark.hil_only(reason="peripheral")
@pytest.mark.capability("esp.flash")
def test_the_flash_keeps_a_value_and_gives_it_back(board, canary):
    """Write a key, read it back, erase it.

    NVS on the ESP32 families and a LittleFS file on the ESP8266, which has
    no NVS. A part whose flash has worn out or whose partition table is
    wrong passes every other check here and then fails whatever a consumer
    stores -- credentials, a role, an OTA marker.
    """
    board.ensure_responsive()
    key = "canary-probe"
    value = f"{canary}-{RUN_TAG}"
    written = board.store_write(key, value)
    assert written.get("ok") is True, f"{canary} could not write to its {written.get('backing')}: {written}"
    read = board.store_read(key)
    assert read.get("found") is True, f"{canary} lost the key it had just written: {read}"
    assert read.get("value") == value, (
        f"{canary} returned {read.get('value')!r} for a key written as {value!r}"
    )
    erased = board.store_erase(key)
    assert erased.get("ok") is True, f"{canary} could not erase the key: {erased}"
    gone = board.store_read(key)
    assert gone.get("found") is False, f"{canary} still has a key it was told to erase"


@pytest.mark.hil_only(reason="power")
@pytest.mark.capability("esp.reset")
def test_the_rig_can_restart_the_board(board, canary):
    """A board the rig cannot restart is a board nothing can recover.

    The reset lines are how every run gets a wedged board back, so this
    checks the lines and the board together: a new boot id proves it really
    restarted rather than answering from the session that was already
    running.
    """
    before = board.ensure_responsive()
    booted = board.restart()
    assert booted.get("bootId"), f"{canary} booted without a boot id"
    assert booted["bootId"] != before.get("bootId"), (
        f"{canary} reports the same boot id after a reset: it did not restart"
    )
    after = board.info(timeout=60)
    assert after["bootId"] == booted["bootId"], (
        f"{canary} answered info from a different session than the one that booted"
    )
    assert after.get("resetReason"), f"{canary} did not say why it restarted"
