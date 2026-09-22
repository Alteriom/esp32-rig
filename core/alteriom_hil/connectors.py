"""What a rig offers a suite, declared once.

A rig is not painlessMesh's. It flashes boards, holds them, and around them it
can offer things a firmware project may want to test against: a Wi-Fi network
of its own, an HTTP endpoint behind it, a broker beside it, a real messaging
service its owner pays for. painlessMesh uses some of them today; another
project will want a different set, and neither should have to read the farm's
source to find out what is there.

So each is a **connector**: a name, what it gives a suite, and the environment
it arrives in. The farm tells every run which connectors its rig has
(`ALTERIOM_HIL_CONNECTORS`), and a suite decides what to do with that -- use
it, or skip the rows that need it and say so. Nothing here runs anything or
knows what a consumer's test looks like: this is the contract, and a consumer
implements against it when it wants to.

    available(env) -> the connectors this rig offers, in the order above

Adding one is a row here, the settings that turn it on, and the environment it
puts them in. What a suite does with it is the suite's business.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class Connector:
    key: str
    title: str
    # What a suite can do with it, in a sentence a consumer's author reads.
    offers: str
    # The environment it arrives in. The first name is the one that says it is
    # there at all; the rest are what a suite needs to use it.
    env: tuple[str, ...]
    # Where it comes from on the rig, for the page that offers to set it up.
    setting: str

    def present(self, env: Mapping[str, str]) -> bool:
        return bool(env.get(self.env[0]))


CONNECTORS: tuple[Connector, ...] = (
    Connector(
        "wifi", "The rig's own Wi-Fi",
        offers="a network a board can associate with, whose name, password and channel are "
               "the rig's to know: radio scans, joins, and anything that needs a board online",
        env=("ALTERIOM_HIL_WIFI_SSID", "ALTERIOM_HIL_WIFI_PASSWORD_FILE",
             "ALTERIOM_HIL_GATEWAY_CHANNEL"),
        setting="gateway.enabled",
    ),
    Connector(
        "uplink", "An HTTP endpoint behind that Wi-Fi",
        offers="a deterministic http server on the rig's own address: a board proves it "
               "reached the Internet through the rig, and reads back exactly what was served",
        env=("ALTERIOM_HIL_GATEWAY_ENDPOINT",),
        setting="gateway.enabled",
    ),
    Connector(
        "mqtt", "A broker beside the rig",
        offers="an MQTT broker a board can publish to and a suite can read from, on the "
               "same address as the Wi-Fi",
        env=("ALTERIOM_HIL_MQTT_URL",),
        setting="mqtt.enabled",
    ),
    Connector(
        "callmebot", "CallMeBot, the owner's own",
        offers="a real WhatsApp message sent by a board, through the owner's account, "
               "within a daily budget -- the proof a mesh reached a service nobody emulated",
        env=("ALTERIOM_HIL_CALLMEBOT_URL_FILE", "ALTERIOM_HIL_CALLMEBOT_SEND",
             "ALTERIOM_HIL_CALLMEBOT_MAX_PER_DAY", "ALTERIOM_HIL_CALLMEBOT_BUDGET_FILE"),
        setting="providers.callmebot",
    ),
    Connector(
        "telegram", "Telegram, the owner's own",
        offers="the same, through a Telegram bot: an https GET with the text in the query, "
               "within a daily budget of its own",
        env=("ALTERIOM_HIL_TELEGRAM_URL_FILE", "ALTERIOM_HIL_TELEGRAM_SEND",
             "ALTERIOM_HIL_TELEGRAM_MAX_PER_DAY", "ALTERIOM_HIL_TELEGRAM_BUDGET_FILE"),
        setting="providers.telegram",
    ),
)

BY_KEY = {connector.key: connector for connector in CONNECTORS}


def available(env: Mapping[str, str]) -> list[Connector]:
    """The connectors this environment says the rig has."""
    return [connector for connector in CONNECTORS if connector.present(env)]


def names(env: Mapping[str, str]) -> str:
    """The value of ``ALTERIOM_HIL_CONNECTORS``: what a suite reads to know
    what it may use, without having to test for each name itself."""
    return ",".join(connector.key for connector in available(env))


def declared(env: Mapping[str, str]) -> list[str]:
    """What ``ALTERIOM_HIL_CONNECTORS`` says, as a suite should read it.

    A rig older than a connector says nothing about it rather than saying no,
    so a suite that finds the name absent falls back to the environment it
    knows -- which is why each connector's first variable is enough on its own.
    """
    said = [item.strip() for item in (env.get("ALTERIOM_HIL_CONNECTORS") or "").split(",")]
    return [item for item in said if item in BY_KEY]
