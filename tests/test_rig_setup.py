"""What a rig is set up to do, as the dashboard says it.

Most of a rig is decided on the rig -- an access point on its own radio, a
broker beside it, a provider's link in a root-owned file -- and until this
existed the dashboard showed none of it. What is asserted here is the part
that matters when someone is looking for why a row skipped: that a capability
is only `on` when something answered, that `off` and `broken` are different
answers, and that a rig which has said nothing is not described as switched
off.
"""

from alteriom_hil.rig_setup import capabilities, summarise


def rows(**kwargs):
    return {row["key"]: row for row in capabilities(**kwargs)}


def health(*checks):
    return {"checks": [{"name": name, "status": status, "message": message}
                       for name, status, message in checks]}


def test_a_rig_that_has_said_nothing_is_not_described_as_switched_off():
    """"Off" is a statement about a rig; with no configuration reported this
    would be a statement about the portal."""
    described = capabilities(None)
    assert {row["state"] for row in described} == {"unknown"}
    assert all("has not reported" in row["summary"] for row in described)
    assert summarise(described)["unknown"] == len(described)


def test_the_access_point_is_on_only_when_it_answered():
    """Configured and not working is the state an operator most needs to see:
    the uplink rows skip either way, and only one of the two is a fault."""
    configured = {"gateway": {"enabled": True, "ssid": "Alteriom-HIL", "channel": 1,
                              "endpoint": "http://10.42.0.1:8088"}}
    up = rows(config=configured, health=health(
        ("gateway_service", "ok", "active"), ("gateway_endpoint", "ok", "HTTP 200"),
        ("gateway_channel", "ok", "the gateway AP is on channel 1"),
    ))["network.ap"]
    assert up["state"] == "on"
    assert "SSID Alteriom-HIL" in up["details"] and "channel 1 (the mesh's channel)" in up["details"]

    split = rows(config=configured, health=health(
        ("gateway_service", "ok", "active"),
        ("gateway_channel", "unhealthy", "the gateway AP is on channel 6, not the mesh channel 1"),
    ))["network.ap"]
    assert split["state"] == "broken" and "channel 6" in split["summary"]

    off = rows(config={"gateway": {"enabled": False}})["network.ap"]
    assert off["state"] == "off" and "skip" in off["summary"]
    # And it says where it is turned on, with the command ready to paste.
    assert off["where"] == "rig" and "setup-gateway-network.sh" in off["command"]


def test_a_provider_says_whether_it_can_spend_and_what_it_has_spent():
    """The question asked of CallMeBot is never "is there a link" alone: a rig
    that will not send during this run answers the same as one with no link
    at all, in the run's rows."""
    never = rows(config={"callmebot": {"url_file": "/etc/alteriom-hil/providers/callmebot-url",
                                       "send": "never", "max_per_day": 25, "used_today": 0}})
    assert never["provider.callmebot"]["state"] == "on"
    assert "no real message is ever sent" in never["provider.callmebot"]["summary"]
    assert "never sends a real message" in never["provider.callmebot"]["details"]

    spending = rows(config={"callmebot": {"url_file": "/etc/x", "send": "release",
                                          "max_per_day": 25, "used_today": 3}})["provider.callmebot"]
    assert spending["state"] == "on"
    assert "sends only during a release run" in spending["details"]
    assert "3 of 25 messages used today" in spending["details"]

    absent = rows(config={"callmebot": {}})["provider.callmebot"]
    assert absent["state"] == "off" and "skip" in absent["summary"]

    refused = rows(config={"callmebot": {"url_file": "/etc/x", "send": "release"}},
                   health=health(("provider_callmebot", "unhealthy", "the stored link is not a CallMeBot URL")))
    assert refused["provider.callmebot"]["state"] == "broken"
    assert "not a CallMeBot URL" in refused["provider.callmebot"]["summary"]


def test_boards_and_power_are_read_from_what_is_registered():
    connected = {"boards": [{"id": "esp32-01", "target": "esp32", "power_hub": "1-1", "power_port": 1},
                            {"id": "esp8266-01", "target": "esp8266"}],
                 "unregistered": [], "missing": []}
    described = rows(config={"gateway": {}}, inventory=connected,
                     health=health(("usb_power", "ok", "uhubctl access available")))
    assert described["boards"]["state"] == "on"
    assert "2 board(s)" in described["boards"]["summary"]
    assert "families: esp32, esp8266" in described["boards"]["details"]
    assert described["power.switching"]["state"] == "on"
    assert "1 board(s) can be power-cut" in described["power.switching"]["summary"]

    # A rig where nothing is registered says so, and counts what is on the
    # ports: that is the difference between "no boards" and "not set up yet".
    new = rows(config={"gateway": {}},
               inventory={"boards": [], "unregistered": [{"mac": "6c:c8:40:34:1e:cc"}], "missing": []})
    assert new["boards"]["state"] == "off"
    assert "1 device(s) on the ports, none registered" in new["boards"]["summary"]

    # uhubctl works, but no board has coordinates: the capability is off, not
    # broken -- a ganged hub is a fact about the hardware, not a fault.
    ganged = rows(config={"gateway": {}}, inventory={"boards": [{"id": "esp32-01", "target": "esp32"}]},
                  health=health(("usb_power", "ok", "uhubctl access available")))
    assert ganged["power.switching"]["state"] == "off"


def test_a_node_that_could_not_install_its_release_says_so():
    node = rows(config={"farm": {"mode": "node"}},
                worker={"version": "1.0.277", "update": {"state": "failed", "detail": "rig NOT ready"}})
    assert node["updates"]["state"] == "broken"
    assert node["updates"]["summary"] == "rig NOT ready"
    assert "running 1.0.277" in node["updates"]["details"]

    standalone = rows(config={"farm": {"mode": "standalone"}})["updates"]
    assert standalone["state"] == "off" and "deployed with its host" in standalone["summary"]


def test_what_a_reader_should_look_at_first_is_what_is_set_up_and_not_working():
    described = capabilities(
        {"gateway": {"enabled": True}, "mqtt": {"enabled": True, "url": "mqtt://10.42.0.1:1883"},
         "callmebot": {"url_file": "/etc/x", "send": "release"}},
        health=health(("gateway_endpoint", "unhealthy", "gateway probe unavailable")),
    )
    counts = summarise(described)
    assert counts["attention"] == ["network.ap"]
    assert counts["broken"] == 1 and counts["on"] >= 2
    # Every row says who can change it, so a page never asks for something
    # the reader cannot do from where they are.
    assert {row["where"] for row in described} <= {"portal", "rig", "deploy"}


def test_a_chip_says_the_one_thing_a_reader_wants_at_a_glance():
    """The rig's heading has room for three words per capability, and "Rig
    access point: on" spends them on "yes". The label carries the detail
    instead: how many boards, which channel, which channels are told."""
    described = rows(
        config={"gateway": {"enabled": True, "channel": 6}, "mqtt": {"enabled": True},
                "notify_channels": [{"id": "a", "channel": "telegram", "enabled": True, "chat_id": "1"},
                                    {"id": "b", "channel": "webhook", "enabled": True, "format": "slack"},
                                    {"id": "c", "channel": "webhook", "enabled": True}]},
        inventory={"boards": [{"id": "esp32-01", "target": "esp32"}, {"id": "esp32-02", "target": "esp32"}],
                   "instruments": [{"id": "io-01", "wiring": [1, 2]}]},
    )
    assert described["boards"]["label"] == "2 boards"
    assert described["network.ap"]["label"] == "Wi-Fi AP · ch 6"
    assert described["network.broker"]["label"] == "MQTT broker"
    assert described["notifications"]["label"] == "Telegram + 2 webhooks"
    assert described["instruments"]["label"] == "1 instrument"
    # A rig that has said nothing keeps its names, because a label is what a
    # capability is called, and a count of nothing is not a fact about it.
    silent = rows(config=None)
    assert silent["boards"]["label"] == "no boards" and silent["network.ap"]["label"] == "Wi-Fi AP"
    assert all(row["label"] for row in silent.values())


def test_notifications_describe_every_channel_a_rig_is_told_through():
    """A rig with Telegram and a webhook was described by its first channel
    alone (the `notify` an older rig reports). The webhook whose last message
    failed is the one that matters, and it was the second."""
    one = rows(config={"notify": {"channel": "telegram", "enabled": True, "chat_id": "42",
                                  "events": "board_red, queue_paused"}})["notifications"]
    assert one["state"] == "on" and one["label"] == "Telegram"
    assert "Telegram chat 42" in one["details"]
    assert "sends: board_red, queue_paused" in one["details"], "a reported list is a string, not letters"

    two = rows(config={
        "notify": {"channel": "telegram", "enabled": True, "chat_id": "42"},
        "notify_channels": [
            {"id": "tg", "channel": "telegram", "enabled": True, "chat_id": "42",
             "last_delivery": {"at": "2026-09-17T10:00:00Z", "ok": True}},
            {"id": "hook", "channel": "webhook", "enabled": True, "format": "slack",
             "last_delivery": {"at": "2026-09-17T10:01:00Z", "ok": False, "error": "HTTP 500"}},
        ]})["notifications"]
    assert two["state"] == "broken" and two["summary"] == "HTTP 500"
    assert two["label"] == "Telegram + webhook"

    off = rows(config={"notify_channels": [{"id": "tg", "channel": "telegram", "enabled": False}]})["notifications"]
    assert off["state"] == "off" and off["label"] == "Notifications"
    assert rows(config={"notify_channels": []})["notifications"]["state"] == "unknown"


def test_the_last_delivery_is_read_in_the_shape_the_rig_reports_it():
    """The portal's `configuration()` says the last delivery as one line --
    `board_red at <when>, delivered`, or `..., failed: <why>` -- and the node
    forwards that unchanged. Read as a mapping, it raised on every rig that
    had ever sent a message; in the rigs list, that was every rig."""
    fine = rows(config={"notify_channels": [
        {"id": "tg", "channel": "telegram", "enabled": True, "chat_id": "42",
         "last_delivery": "board_red at 2026-09-17T10:00:00+00:00, delivered"}]})["notifications"]
    assert fine["state"] == "on"
    assert "last message board_red at 2026-09-17T10:00:00+00:00, delivered" in fine["details"]

    lost = rows(config={"notify_channels": [
        {"id": "hook", "channel": "webhook", "enabled": True, "format": "slack",
         "last_delivery": "queue_paused at 2026-09-17T10:01:00+00:00, failed: HTTP 500"}]})["notifications"]
    assert lost["state"] == "broken" and lost["summary"] == "HTTP 500"

    # A failure the rig could not name still says the message did not arrive,
    # and a channel that has sent nothing yet is not broken.
    unnamed = rows(config={"notify_channels": [
        {"id": "hook", "channel": "webhook", "enabled": True, "last_delivery": "board_red at t, failed: None"}]})
    assert unnamed["notifications"]["summary"] == "the last message did not arrive"
    quiet = rows(config={"notify_channels": [{"id": "hook", "channel": "webhook", "enabled": True, "last_delivery": None}]})
    assert quiet["notifications"]["state"] == "on"


def test_the_headline_is_what_is_on_and_what_is_broken_with_only_what_a_chip_needs():
    """A list of rigs polled every few seconds carries the chips and not the
    table: no command, no `enables`, and nothing that is off -- a heading says
    what a rig can do, not what it cannot."""
    from alteriom_hil.rig_setup import headline

    described = capabilities(
        {"gateway": {"enabled": True, "channel": 1}, "mqtt": {"enabled": False},
         "callmebot": {"url_file": "/etc/x", "send": "release"}},
        health=health(("gateway_endpoint", "unhealthy", "gateway probe unavailable")),
    )
    chips = headline(described)
    assert [chip["key"] for chip in chips] == ["network.ap", "provider.callmebot"]
    assert {chip["state"] for chip in chips} == {"broken", "on"}
    assert set(chips[0]) == {"key", "title", "label", "state", "summary"}
    assert headline(capabilities(None)) == []


def test_the_world_page_gets_what_a_rig_can_do_and_nothing_about_how_it_is_kept():
    """A stranger may see that a rig has a broker, not where it is; that it
    has boards, not their MACs; and nothing about its backups, its
    quarantine, who is told or how it is deployed -- those are its owner's."""
    from alteriom_hil.rig_setup import public_headline

    described = capabilities(
        {"gateway": {"enabled": True, "ssid": "Alteriom-HIL", "channel": 1},
         "mqtt": {"enabled": True, "url": "mqtt://10.42.0.1:1883"},
         "backup": {"enabled": True, "directory": "/var/backups/x", "keep": 7},
         "notify_channels": [{"id": "tg", "channel": "telegram", "enabled": True, "chat_id": "42"}]},
        inventory={"boards": [{"id": "esp32-01", "target": "esp32", "mac": "6c:c8:40:34:1e:cc"}]},
        health=health(("backup", "ok", "made")),
    )
    chips = public_headline(described)
    assert [chip["key"] for chip in chips] == ["boards", "network.ap", "network.broker"]
    assert all(set(chip) == {"key", "label", "state"} for chip in chips), "no summary: a summary can carry an address"
    said = repr(chips)
    assert "10.42" not in said and "Alteriom-HIL" not in said and "6c:c8" not in said and "42" not in said
