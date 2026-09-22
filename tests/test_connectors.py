"""What a rig offers a suite, and how a suite is told.

A rig is not painlessMesh's. It flashes boards and holds them, and around them
it can offer a Wi-Fi network, an endpoint behind it, a broker, a real messaging
service its owner pays for. Which of those a project uses is the project's
business; what is asserted here is that the farm says what it has, in one
place, in a form a consumer can read without reading the farm's source.
"""

from alteriom_hil import connectors


def test_a_rig_says_what_it_offers_rather_than_a_suite_guessing():
    env = {
        "ALTERIOM_HIL_WIFI_SSID": "Alteriom-HIL",
        "ALTERIOM_HIL_WIFI_PASSWORD_FILE": "/etc/alteriom-hil/gateway-wifi-password",
        "ALTERIOM_HIL_GATEWAY_CHANNEL": "1",
        "ALTERIOM_HIL_GATEWAY_ENDPOINT": "http://10.42.0.1:8088",
        "ALTERIOM_HIL_MQTT_URL": "mqtt://10.42.0.1:1883",
    }
    assert connectors.names(env) == "wifi,uplink,mqtt"
    assert [item.key for item in connectors.available(env)] == ["wifi", "uplink", "mqtt"]

    # A rig with no network of its own offers neither, and says so by saying
    # nothing: a suite skips those rows rather than failing them.
    assert connectors.names({}) == ""
    assert connectors.available({}) == []


def test_a_provider_is_a_connector_like_the_rest():
    """The owner's own CallMeBot account and the owner's own Telegram bot are
    the same kind of offer: a real service a board can reach, within a budget.
    A suite reads the same list for both."""
    env = {
        "ALTERIOM_HIL_CALLMEBOT_URL_FILE": "/etc/alteriom-hil/providers/callmebot-url",
        "ALTERIOM_HIL_TELEGRAM_URL_FILE": "/etc/alteriom-hil/providers/telegram-link",
    }
    assert connectors.names(env) == "callmebot,telegram"
    telegram = connectors.BY_KEY["telegram"]
    assert telegram.setting == "providers.telegram"
    assert "budget" in telegram.offers
    # Each names its whole environment, so a consumer's author has one place
    # to look for what to read.
    assert "ALTERIOM_HIL_TELEGRAM_SEND" in telegram.env
    assert "ALTERIOM_HIL_TELEGRAM_BUDGET_FILE" in telegram.env


def test_a_rig_older_than_a_connector_says_nothing_rather_than_no():
    """`ALTERIOM_HIL_CONNECTORS` is a convenience, not the contract: a suite
    that finds it absent falls back to the variables it knows, which is why
    each connector's first variable is enough on its own."""
    assert connectors.declared({}) == []
    assert connectors.declared({"ALTERIOM_HIL_CONNECTORS": "wifi,uplink"}) == ["wifi", "uplink"]
    # A name from a newer farm is ignored rather than tripping an older suite.
    assert connectors.declared({"ALTERIOM_HIL_CONNECTORS": "wifi,teleportation"}) == ["wifi"]


def test_every_connector_is_described_for_the_person_implementing_against_it():
    for connector in connectors.CONNECTORS:
        assert connector.title and connector.offers and connector.env and connector.setting
        assert connector.offers[0].islower(), f"{connector.key}: a phrase, not a heading"
        assert all(name.startswith("ALTERIOM_HIL_") for name in connector.env)
