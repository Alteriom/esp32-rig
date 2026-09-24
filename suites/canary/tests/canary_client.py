"""Typed helpers for the canary firmware's commands.

The canary speaks the same newline-JSON framing every board here does, so
this wraps `alteriom_hil.protocol.BoardClient` rather than replacing it: the
waiting, the retry on a reported parse failure, and the reset-on-wedged
recovery are the HAL's and are worth having here too.

It lives beside the suite rather than in the HAL because these commands are
the canary's, not the farm's contract with every project -- the same reason
painlessMesh's mesh helpers live in `BoardClient`'s painlessMesh half. Keep
it in lockstep with canary/firmware/src/main.cpp and alteriom_hil/sim.py.
"""

from __future__ import annotations

from alteriom_hil.protocol import BoardClient


class CanaryClient:
    def __init__(self, board: BoardClient):
        self.board = board
        self.board_id = board.board_id

    # ---- what it is ----

    def info(self, timeout: float = 20.0) -> dict:
        return self.board.send_cmd_awaiting(
            "info", lambda e: e["evt"] == "info", "canary info", timeout
        )

    def ensure_responsive(self, timeout: float = 20.0) -> dict:
        """The HAL's own recovery: one reset for a board that has wedged."""
        return self.board.ensure_responsive(timeout=timeout)

    # ---- the serial path ----

    def echo(self, text: str, timeout: float = 20.0) -> dict:
        return self.board.send_cmd_awaiting(
            "echo", lambda e: e["evt"] == "echo", "echo reply", timeout, text=text
        )

    # ---- the key/value store (NVS, or LittleFS on the ESP8266) ----

    def store_write(self, key: str, value: str, timeout: float = 20.0) -> dict:
        return self._store("store_write", "write", timeout, key=key, value=value)

    def store_read(self, key: str, timeout: float = 20.0) -> dict:
        return self._store("store_read", "read", timeout, key=key)

    def store_erase(self, key: str, timeout: float = 20.0) -> dict:
        return self._store("store_erase", "erase", timeout, key=key)

    def _store(self, cmd: str, op: str, timeout: float, **kwargs) -> dict:
        return self.board.send_cmd_awaiting(
            cmd,
            lambda e: e["evt"] == "store" and e.get("op") == op,
            f"store {op} reply",
            timeout,
            **kwargs,
        )

    # ---- the radio ----

    def wifi_scan(self, ssid: str | None = None, timeout: float = 45.0) -> dict:
        # A blocking all-channel scan is seconds on every family and thirteen
        # on the C5, so the budget is generous by default: a scan the host
        # gave up on looks like a radio that cannot see, which is the one
        # answer this check must not get wrong. Measured on a seven-board rig
        # (farm runs 51fc3bed and be46491e): 2.2 s on the ESP8266, 2.8 s on the C3
        # and C6, 6.3 s on the ESP32, 12.3 s on the C5 -- the budget is not
        # what a scan runs out of.
        # A scan is safe to repeat, so a reply that reached the rig unreadable
        # is asked for again rather than waited out: esp32-fde4's did, once,
        # its first 32 characters arriving as 64 undecodable bytes with the
        # tail intact (be46491e), and the board was blamed for 45 s of silence
        # it never kept.
        kwargs = {"ssid": ssid} if ssid else {}
        return self.board.send_cmd_awaiting(
            "wifi_scan",
            lambda e: e["evt"] == "wifi_scan",
            "scan reply",
            timeout,
            idempotent=True,
            **kwargs,
        )

    def wifi_join(self, ssid: str, password: str, budget_ms: int = 20000,
                  timeout: float | None = None) -> dict:
        return self.board.send_cmd_awaiting(
            "wifi_join",
            lambda e: e["evt"] == "wifi_join",
            "join reply",
            timeout if timeout is not None else budget_ms / 1000.0 + 15,
            ssid=ssid,
            password=password,
            timeoutMs=budget_ms,
        )

    def wifi_leave(self, timeout: float = 20.0) -> dict:
        return self.board.send_cmd_awaiting(
            "wifi_leave", lambda e: e["evt"] == "wifi_leave", "leave reply", timeout
        )

    # ---- the rig around it ----

    def http_get(self, url: str, budget_ms: int = 8000, timeout: float | None = None) -> dict:
        return self.board.send_cmd_awaiting(
            "http_get",
            lambda e: e["evt"] == "http_get",
            "http_get reply",
            timeout if timeout is not None else budget_ms / 1000.0 + 15,
            url=url,
            timeoutMs=budget_ms,
        )

    def mqtt_publish(self, host: str, port: int, topic: str, payload: str,
                     budget_ms: int = 8000, timeout: float | None = None) -> dict:
        return self.board.send_cmd_awaiting(
            "mqtt_publish",
            lambda e: e["evt"] == "mqtt_publish",
            "mqtt_publish reply",
            timeout if timeout is not None else budget_ms / 1000.0 + 15,
            host=host,
            port=port,
            topic=topic,
            payload=payload,
        )

    # ---- the board's end of a wire (the wiring check) ----
    # A refused pin raises rather than returning a level nobody read: the
    # canary refuses every pin that is not wireable on its family.

    def _gpio(self, cmd: str, op: str, timeout: float, **kwargs) -> dict:
        from alteriom_hil.protocol import ProtocolError

        reply = self.board.send_cmd_awaiting(
            cmd,
            lambda e: e["evt"] == "gpio" and e.get("op") == op and e.get("pin") == kwargs.get("pin"),
            f"gpio {op} reply",
            timeout,
            **kwargs,
        )
        if not reply.get("ok"):
            raise ProtocolError(f"{self.board_id}: gpio {op} on GPIO{kwargs.get('pin')} refused: {reply.get('error')}")
        return reply

    def gpio_mode(self, pin: int, mode: str, timeout: float = 10.0) -> dict:
        return self._gpio("gpio_mode", "mode", timeout, pin=pin, mode=mode)

    def gpio_write(self, pin: int, level: int, timeout: float = 10.0) -> dict:
        return self._gpio("gpio_write", "write", timeout, pin=pin, level=1 if level else 0)

    def gpio_read(self, pin: int, timeout: float = 10.0) -> int:
        return int(self._gpio("gpio_read", "read", timeout, pin=pin)["level"])

    def gpio_release(self, pin: int, timeout: float = 10.0) -> dict:
        return self._gpio("gpio_release", "release", timeout, pin=pin)

    # ---- it restarts ----

    def restart(self, timeout: float = 45.0) -> dict:
        """Reset the board and return its `boot`.

        Over RTS/DTR where the rig has those lines, because that is the rig
        capability worth checking -- a board that only restarts when its own
        firmware agrees to is a board nothing can recover. Where there are
        no lines to pulse (the simulator), the command path stands in, and
        the invariant asserted is the same: a new boot id.
        """
        from alteriom_hil.protocol import ProtocolError

        try:
            return self.board.hard_reset(timeout=timeout)
        except ProtocolError:
            self.board.clear_pending()
            self.board.send_cmd("reset")
            return self.board.wait_for(
                lambda e: e["evt"] == "boot", "boot after reset command", timeout
            )
