import importlib.util
import json
from pathlib import Path

import yaml


RUNNER = Path(__file__).resolve().parents[1] / "runner"
# The rig's own scripts, examples and schemas, which are beside its package
# now (docs/public-release-plan.md, step 12f).
RIG = Path(__file__).resolve().parents[1] / "rig"
SPEC = importlib.util.spec_from_file_location(
    "hil_config", RUNNER.parent / "core" / "alteriom_hil" / "hil_config.py")
hil_config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hil_config)


def valid_config(tmp_path):
    return {
        "schema": 2,
        "mode": "hardware",
        "runner": {"unit": "actions.runner.Alteriom.farm.service"},
        "paths": {
            "venv": str(tmp_path / "venv"),
            "board_map": str(tmp_path / "board-map.yaml"),
        },
        "health": {
            "interval_minutes": 5,
            "minimum_boards": 2,
            "disk_warn_percent": 85,
            "disk_critical_percent": 95,
        },
        "gateway": {
            "enabled": True,
            "ssid": "Alteriom-HIL",
            "password_file": str(tmp_path / "gateway-password"),
            "endpoint": "http://10.42.0.1:8088",
            "channel": 1,
        },
        "mqtt": {"enabled": True, "url": "mqtt://10.42.0.1:1883"},
        "service": {
            "enabled": True,
            "bind": "127.0.0.1",
            "port": 8090,
            "token_file": str(tmp_path / "api-token"),
            "public_host": "hil.example.com",
        },
    }


def test_example_matches_schema_and_internal_validation():
    example = yaml.safe_load((RIG / "hil-config.example.yaml").read_text())
    schema = json.loads((RIG / "hil-config.schema.json").read_text())
    assert example["schema"] == schema["properties"]["schema"]["const"]
    assert hil_config.validate_config(example) == []


def test_unknown_and_invalid_values_are_rejected(tmp_path):
    payload = valid_config(tmp_path)
    payload["surprise"] = True
    payload["health"]["disk_warn_percent"] = 99
    payload["health"]["disk_critical_percent"] = 95
    errors = hil_config.validate_config(payload)
    assert "unknown setting: surprise" in errors
    assert "health.disk_warn_percent must be below disk_critical_percent" in errors


def test_the_queue_says_how_many_runs_may_be_in_progress(tmp_path):
    payload = valid_config(tmp_path)
    assert "ALTERIOM_HIL_MAX_RUNS" not in hil_config.runtime_env(payload), "absent: the service's default of one"
    payload["queue"] = {"concurrency": 3}
    assert "ALTERIOM_HIL_MAX_RUNS=3\n" in hil_config.runtime_env(payload)
    for bad in (0, 17, "2", True):
        payload["queue"] = {"concurrency": bad}
        assert "queue.concurrency must be an integer from 1 to 16" in hil_config.validate_config(payload)
    payload["queue"] = {"concurrency": 2, "fairness": "strict"}
    assert "unknown setting: queue.fairness" in hil_config.validate_config(payload)


def test_a_node_names_its_portal_its_worker_and_its_key_file(tmp_path):
    payload = valid_config(tmp_path)
    payload["farm"] = {"mode": "standalone"}
    text = hil_config.runtime_env(payload)
    assert "ALTERIOM_HIL_FARM_MODE" not in text, "standalone is the default and exports nothing"
    payload["farm"] = {"mode": "node", "portal_url": "https://espfarm.alteriom.net",
                       "worker_name": "esp32-hil", "node_key_file": "/etc/alteriom-hil/node-key"}
    text = hil_config.runtime_env(payload)
    for line in ("ALTERIOM_HIL_FARM_MODE=node", "ALTERIOM_HIL_PORTAL_URL=https://espfarm.alteriom.net",
                 "ALTERIOM_HIL_WORKER_NAME=esp32-hil", "ALTERIOM_HIL_NODE_KEY_FILE=/etc/alteriom-hil/node-key"):
        assert line + "\n" in text
    payload["farm"] = {"mode": "node", "portal_url": "http://espfarm.alteriom.net", "worker_name": "Pi 1",
                       "node_key_file": "node-key"}
    errors = hil_config.validate_config(payload)
    assert "farm.portal_url must be the portal's https:// URL" in errors
    assert "farm.worker_name must be 1-32 lowercase letters, digits, dots, underscores or hyphens" in errors
    assert "farm.node_key_file must be an absolute path" in errors
    payload["farm"] = {"mode": "cluster"}
    assert "farm.mode must be one of standalone, attached, portal, node" in hil_config.validate_config(payload)
    # Attached: the main service stays standalone -- no FARM_MODE -- and the
    # installer starts the node agent beside it.
    payload["farm"] = {"mode": "attached", "portal_url": "https://espfarm.alteriom.net",
                       "worker_name": "esp32-hil", "node_key_file": "/etc/alteriom-hil/node-key"}
    text = hil_config.runtime_env(payload)
    assert "ALTERIOM_HIL_FARM_MODE" not in text
    assert "ALTERIOM_HIL_FARM_ATTACHED=1\n" in text and "ALTERIOM_HIL_WORKER_NAME=esp32-hil\n" in text
    payload["farm"] = {"mode": "attached"}
    assert "farm.portal_url must be the portal's https:// URL" in hil_config.validate_config(payload)


def test_a_portal_changes_only_the_settings_a_node_allows_remotely(tmp_path):
    payload = valid_config(tmp_path)
    payload.pop("queue", None)
    updated = hil_config.set_values(payload, {"queue.concurrency": 2, "retention.enabled": True,
                                              "retention.run_evidence_days": 30})
    assert updated["queue"] == {"concurrency": 2} and updated["retention"]["run_evidence_days"] == 30
    for refused, says in (({"farm.portal_url": "https://x"}, "cannot be changed remotely"),
                          ({"queue.concurrency": "2"}, "must be an integer"),
                          ({"retention.enabled": 1}, "must be true or false"),
                          ({}, "non-empty")):
        try:
            hil_config.set_values(valid_config(tmp_path), refused)
        except hil_config.ConfigError as exc:
            assert says in str(exc)
        else:
            raise AssertionError(refused)
    try:
        hil_config.set_values(valid_config(tmp_path), {"queue.concurrency": 99})
    except hil_config.ConfigError as exc:
        assert "queue.concurrency" in str(exc), "the whole file is validated before it is kept"
    else:
        raise AssertionError("out of range")


def test_a_portal_sets_a_rigs_callmebot_policy_but_never_its_link(tmp_path):
    """The send policy and the daily cap are remote settings; the section is
    made with its defaults on a host that had none, and the link's file is
    never something a portal names."""
    payload = valid_config(tmp_path)
    payload.pop("providers", None)
    updated = hil_config.set_values(payload, {"providers.callmebot.send": "never"})
    assert updated["providers"]["callmebot"] == {**hil_config.DEFAULT_CALLMEBOT, "send": "never"}
    updated = hil_config.set_values(updated, {"providers.callmebot.max_per_day": 12, "providers.callmebot.send": "always"})
    assert updated["providers"]["callmebot"]["max_per_day"] == 12 and updated["providers"]["callmebot"]["send"] == "always"
    assert updated["providers"]["callmebot"]["url_file"] == hil_config.DEFAULT_CALLMEBOT["url_file"]
    for refused, says in (({"providers.callmebot.send": "sometimes"}, "must be one of never, release, always"),
                          ({"providers.callmebot.send": True}, "must be one of"),
                          ({"providers.callmebot.max_per_day": "5"}, "must be an integer"),
                          ({"providers.callmebot.max_per_day": 51}, "from 1 to 50"),
                          ({"providers.callmebot.max_per_day": 0}, "from 1 to 50"),
                          ({"providers.callmebot.url_file": "/tmp/mine"}, "cannot be changed remotely")):
        try:
            hil_config.checked_remote_settings(refused)
        except hil_config.ConfigError as exc:
            assert says in str(exc), (refused, str(exc))
        else:
            raise AssertionError(refused)
    # Every remote setting is one the dashboard can change (rig/web/app.js):
    # most as a row in the settings table, the notification ones from the
    # channel card that shows what they mean -- but each of them from the page,
    # never only from an ssh session.
    script = (RUNNER.parent / "rig" / "web" / "app.js").read_text(encoding="utf-8")
    for key in hil_config.REMOTE_SETTINGS:
        assert f'"{key}"' in script, key


def test_a_portal_reads_the_configuration_its_deployment_names(tmp_path, monkeypatch):
    """A portal's container mounts its file where the deployment says."""
    import importlib.util

    named = tmp_path / "portal.yaml"
    named.write_text("schema: 2\nquarantine: {enabled: true, after_failures: 2}\n", encoding="utf-8")

    def fresh():
        spec = importlib.util.spec_from_file_location("hil_config_fresh", Path(hil_config.__file__))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    monkeypatch.setenv("ALTERIOM_HIL_CONFIG", str(named))
    portal = fresh()
    assert portal.CONFIG_PATH == named
    assert portal.load_config()["quarantine"] == {"enabled": True, "after_failures": 2}
    monkeypatch.delenv("ALTERIOM_HIL_CONFIG")
    assert fresh().CONFIG_PATH == Path("/etc/alteriom-hil/config.yaml")


def test_a_node_needs_no_actions_runner_and_every_other_host_does(tmp_path):
    """A node takes its work and its releases from its portal; its runner is
    removed. Anything else is still deployed through one."""
    payload = valid_config(tmp_path)
    payload["runner"] = {"unit": None}
    assert "runner.unit must be an actions.runner.*.service unit (or null on a node)" in hil_config.validate_config(payload)
    payload["farm"] = {"mode": "node", "portal_url": "https://espfarm.alteriom.net",
                       "worker_name": "esp32-hil", "node_key_file": "/etc/alteriom-hil/node-key"}
    assert hil_config.validate_config(payload) == []
    assert "HIL_RUNNER_UNIT" not in hil_config.runtime_env(payload)


def test_quarantine_is_off_until_a_host_turns_it_on(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(hil_config.dump_config(valid_config(tmp_path)))
    assert hil_config.load_config(path)["quarantine"] == {"enabled": False, "after_failures": 2}
    payload = valid_config(tmp_path)
    payload["quarantine"] = {"enabled": "yes", "after_failures": 0}
    errors = hil_config.validate_config(payload)
    assert "quarantine.enabled must be a boolean" in errors
    assert "quarantine.after_failures must be an integer from 1 to 10" in errors


def test_runtime_environment_contains_only_derived_service_values(tmp_path):
    text = hil_config.runtime_env(valid_config(tmp_path))
    assert "ALTERIOM_HIL_MODE=hardware\n" in text
    assert f"ALTERIOM_HIL_VENV={tmp_path / 'venv'}\n" in text
    assert "HIL_DISK_WARN_PERCENT=85\n" in text
    assert "ALTERIOM_HIL_WIFI_SSID=Alteriom-HIL\n" in text
    assert f"ALTERIOM_HIL_WIFI_PASSWORD_FILE={tmp_path / 'gateway-password'}\n" in text
    assert "ALTERIOM_HIL_SERVICE_ENABLED=1\n" in text
    assert "ALTERIOM_HIL_API_BIND=127.0.0.1\n" in text
    assert "ALTERIOM_HIL_PUBLIC_HOST=hil.example.com\n" in text
    assert "ALTERIOM_HIL_MQTT_URL=mqtt://10.42.0.1:1883\n" in text
    assert "schema" not in text


def test_a_host_without_a_broker_exports_no_mqtt_url(tmp_path):
    """A suite skips its queue scenario when the variable is absent; an
    empty or default URL would make it try to connect to nothing."""
    payload = valid_config(tmp_path)
    payload["mqtt"]["enabled"] = False
    assert "ALTERIOM_HIL_MQTT_URL" not in hil_config.runtime_env(payload)

    del payload["mqtt"]
    assert "ALTERIOM_HIL_MQTT_URL" not in hil_config.runtime_env(payload)


def test_a_broker_url_must_be_mqtt_host_port(tmp_path):
    payload = valid_config(tmp_path)
    payload["mqtt"]["url"] = "http://10.42.0.1:1883"
    assert "mqtt.url must be mqtt://<host>:<port>" in hil_config.validate_config(payload)
    payload["mqtt"]["url"] = "mqtt://10.42.0.1:1883"
    assert not hil_config.validate_config(payload)


def test_invalid_public_hostname_is_rejected(tmp_path):
    payload = valid_config(tmp_path)
    payload["service"]["public_host"] = "https://hil.example.com/path"
    assert "service.public_host must be a DNS hostname when set" in hil_config.validate_config(
        payload
    )


def test_set_value_preserves_type_and_validates(tmp_path):
    payload = valid_config(tmp_path)
    updated = hil_config.set_value(payload, "health.interval_minutes", "15")
    assert updated["health"]["interval_minutes"] == 15
    updated = hil_config.set_value(payload, "gateway.enabled", "false")
    assert updated["gateway"]["enabled"] is False


def test_atomic_config_write(tmp_path):
    path = tmp_path / "config.yaml"
    hil_config.write_atomic(path, hil_config.dump_config(valid_config(tmp_path)))
    assert path.stat().st_mode & 0o777 == 0o640
    assert hil_config.load_config(path)["schema"] == 2


def test_joining_a_portal_makes_the_host_its_node_with_no_runner(tmp_path):
    payload = hil_config.join_portal(valid_config(tmp_path), "https://espfarm.alteriom.net/", "rig-2")
    assert payload["farm"] == {"mode": "node", "portal_url": "https://espfarm.alteriom.net",
                               "worker_name": "rig-2", "node_key_file": "/etc/alteriom-hil/node-key"}
    assert payload["runner"]["unit"] is None
    assert "ALTERIOM_HIL_WORKER_NAME=rig-2\n" in hil_config.runtime_env(payload)
    import pytest

    with pytest.raises(hil_config.ConfigError):
        hil_config.join_portal(valid_config(tmp_path), "http://espfarm.alteriom.net", "rig-2")
    with pytest.raises(hil_config.ConfigError):
        hil_config.join_portal(valid_config(tmp_path), "https://espfarm.alteriom.net", "Rig 2")


def test_a_callmebot_provider_exports_only_its_paths_and_policy(tmp_path):
    payload = valid_config(tmp_path)
    assert "CALLMEBOT" not in hil_config.runtime_env(payload), "no section, no provider"
    payload["providers"] = {"callmebot": {}}
    text = hil_config.runtime_env(payload)
    for line in ("ALTERIOM_HIL_CALLMEBOT_URL_FILE=/etc/alteriom-hil/providers/callmebot-url",
                 "ALTERIOM_HIL_CALLMEBOT_SEND=release",
                 "ALTERIOM_HIL_CALLMEBOT_MAX_PER_DAY=5",
                 "ALTERIOM_HIL_CALLMEBOT_BUDGET_FILE=/var/lib/alteriom-hil/callmebot-budget.json"):
        assert line + "\n" in text
    payload["paths"]["state"] = str(tmp_path / "state")
    payload["providers"] = {"callmebot": {"url_file": "/etc/x/link", "send": "always", "max_per_day": 50}}
    text = hil_config.runtime_env(payload)
    assert "ALTERIOM_HIL_CALLMEBOT_URL_FILE=/etc/x/link\n" in text
    assert "ALTERIOM_HIL_CALLMEBOT_SEND=always\n" in text
    assert "ALTERIOM_HIL_CALLMEBOT_MAX_PER_DAY=50\n" in text
    assert f"ALTERIOM_HIL_CALLMEBOT_BUDGET_FILE={tmp_path / 'state'}/callmebot-budget.json\n" in text


def test_a_callmebot_provider_is_validated(tmp_path):
    payload = valid_config(tmp_path)
    payload["providers"] = {"callmebot": {"url_file": "relative", "send": "sometimes", "max_per_day": 0}}
    errors = hil_config.validate_config(payload)
    assert "providers.callmebot.url_file must be an absolute path" in errors
    assert "providers.callmebot.send must be one of never, release, always" in errors
    assert "providers.callmebot.max_per_day must be an integer from 1 to 50" in errors
    for bad in (51, "5", True):
        payload["providers"] = {"callmebot": {"max_per_day": bad}}
        assert "providers.callmebot.max_per_day must be an integer from 1 to 50" in hil_config.validate_config(payload)
    payload["providers"] = {"callmebot": {"send": "never", "url": "https://api.callmebot.com/"}}
    assert "unknown setting: providers.callmebot.url" in hil_config.validate_config(payload)
    payload["providers"] = {"telegram": {}}
    assert "unknown setting: providers.telegram" in hil_config.validate_config(payload)
    payload["providers"] = ["callmebot"]
    assert "providers must be a mapping" in hil_config.validate_config(payload)
    payload["providers"] = {"callmebot": "on"}
    assert "providers.callmebot must be a mapping" in hil_config.validate_config(payload)
    payload["providers"] = {"callmebot": {"send": "never"}}
    assert hil_config.validate_config(payload) == []
    updated = hil_config.set_value(payload, "providers.callmebot.send", "always")
    assert updated["providers"]["callmebot"]["send"] == "always"


def test_the_schema_and_the_example_describe_the_providers_section():
    schema = json.loads((RIG / "hil-config.schema.json").read_text())
    callmebot = schema["properties"]["providers"]["properties"]["callmebot"]["properties"]
    assert callmebot["send"]["enum"] == list(hil_config.PROVIDER_SEND_POLICIES)
    assert callmebot["url_file"]["default"] == hil_config.DEFAULT_CALLMEBOT["url_file"]
    assert callmebot["max_per_day"]["maximum"] == 50
    assert "# providers:" in (RIG / "hil-config.example.yaml").read_text()


def test_every_name_the_runtime_environment_can_hold_is_known(tmp_path):
    """What the farm refuses from a dispatch is derived from here, so a name
    runtime_env() writes must be in the set, whatever the host enables."""
    keys = hil_config.runtime_env_keys()
    for name in ("ALTERIOM_HIL_WIFI_SSID", "ALTERIOM_HIL_WIFI_PASSWORD_FILE", "ALTERIOM_HIL_GATEWAY_ENDPOINT",
                 "ALTERIOM_HIL_MQTT_URL", "ALTERIOM_HIL_MODE", "ALTERIOM_HIL_BOARD_MAP", "ALTERIOM_HIL_NODE_KEY_FILE",
                 "ALTERIOM_HIL_FARM_ATTACHED", "ALTERIOM_HIL_FARM_MODE", "ALTERIOM_HIL_CALLMEBOT_URL_FILE",
                 "ALTERIOM_HIL_CALLMEBOT_SEND", "ALTERIOM_HIL_NOTIFY_WEBHOOK_FILE", "ALTERIOM_HIL_BACKUP_DIR",
                 "HIL_RUNNER_UNIT", "PATH"):
        assert name in keys, name
    payload = valid_config(tmp_path)
    payload["providers"] = {"callmebot": {}}
    payload["notify"] = {"enabled": True, "webhook_url_file": "/n"}
    written = {line.split("=", 1)[0] for line in hil_config.runtime_env(payload).splitlines()}
    assert written <= keys


def test_the_gateway_channel_reaches_the_suites_and_is_a_real_channel(tmp_path):
    """The AP's channel is the mesh's channel: a bridge takes the mesh to
    whatever this AP is on, so a suite and the health check both need to know
    which it should be. A rig configured before the setting existed has none,
    and is read as painlessMesh's own default rather than refused."""
    payload = valid_config(tmp_path)
    assert "ALTERIOM_HIL_GATEWAY_CHANNEL=1\n" in hil_config.runtime_env(payload)

    payload["gateway"]["channel"] = 11
    assert hil_config.validate_config(payload) == []
    assert "ALTERIOM_HIL_GATEWAY_CHANNEL=11\n" in hil_config.runtime_env(payload)

    del payload["gateway"]["channel"]
    assert hil_config.validate_config(payload) == []
    assert "ALTERIOM_HIL_GATEWAY_CHANNEL=1\n" in hil_config.runtime_env(payload)

    # The published schema is what other tooling validates a rig's file with,
    # so a setting the CLI writes must be in it.
    schema = json.loads((RIG / "hil-config.schema.json").read_text())
    channel_schema = schema["properties"]["gateway"]["properties"]["channel"]
    assert channel_schema["minimum"] == 1 and channel_schema["maximum"] == 13
    assert channel_schema["default"] == hil_config.GATEWAY_DEFAULT_CHANNEL

    # A gateway section that is not a mapping is left as written, so the
    # validator can say so rather than load_config raising on it.
    broken = tmp_path / "broken.yaml"
    aged_broken = valid_config(tmp_path)
    aged_broken["gateway"] = "enabled"
    broken.write_text(yaml.safe_dump(aged_broken), encoding="utf-8")
    assert "gateway must be a mapping" in hil_config.validate_config(
        hil_config.load_config(broken)
    )

    # A rig whose config.yaml predates the setting gains the default on load,
    # so `alteriom-hil-admin config set gateway.channel 6` has a type to follow.
    written = tmp_path / "config.yaml"
    aged = valid_config(tmp_path)
    del aged["gateway"]["channel"]
    written.write_text(yaml.safe_dump(aged), encoding="utf-8")
    loaded = hil_config.load_config(written)
    assert loaded["gateway"]["channel"] == 1
    assert loaded["gateway"]["ssid"] == "Alteriom-HIL"
    assert hil_config.set_value(loaded, "gateway.channel", "6")["gateway"]["channel"] == 6

    for refused in (0, 14, "6", True):
        payload["gateway"]["channel"] = refused
        assert "gateway.channel must be a whole number from 1 to 13" in hil_config.validate_config(payload)


def test_boards_register_themselves_unless_the_rig_says_not_to(tmp_path):
    """The setting that makes a rig something you wire up rather than
    something you enrol, and the suites are told either way."""
    payload = valid_config(tmp_path)
    payload["inventory"] = {"auto_register": True}
    assert hil_config.validate_config(payload) == []
    assert "ALTERIOM_HIL_AUTO_REGISTER=1\n" in hil_config.runtime_env(payload)

    payload["inventory"]["auto_register"] = False
    assert "ALTERIOM_HIL_AUTO_REGISTER=0\n" in hil_config.runtime_env(payload)

    payload["inventory"]["auto_register"] = "yes"
    assert "inventory.auto_register must be a boolean" in hil_config.validate_config(payload)

    # A rig configured before the setting existed gains the default on load.
    written = tmp_path / "config.yaml"
    aged = valid_config(tmp_path)
    written.write_text(yaml.safe_dump(aged), encoding="utf-8")
    assert hil_config.load_config(written)["inventory"]["auto_register"] is True

    schema = json.loads((RIG / "hil-config.schema.json").read_text())
    assert schema["properties"]["inventory"]["properties"]["auto_register"]["default"] is True


def test_a_telegram_channel_is_a_token_file_and_a_chat(tmp_path):
    """What the farm needs to start sending: the token in a file of its own,
    and the chat to send to."""
    payload = valid_config(tmp_path)
    payload["notify"] = {
        "enabled": True, "channel": "telegram",
        "token_file": "/etc/alteriom-hil/providers/telegram-token",
        "chat_id": "-1001234567890",
    }
    assert hil_config.validate_config(payload) == []
    text = hil_config.runtime_env(payload)
    assert "ALTERIOM_HIL_NOTIFY_CHANNEL=telegram\n" in text
    assert "ALTERIOM_HIL_NOTIFY_TOKEN_FILE=/etc/alteriom-hil/providers/telegram-token\n" in text
    assert "ALTERIOM_HIL_NOTIFY_CHAT_ID=-1001234567890\n" in text
    assert "ALTERIOM_HIL_NOTIFY_WEBHOOK_FILE" not in text, "one channel at a time"

    payload["notify"]["chat_id"] = "not-a-chat"
    assert any("chat id" in error for error in hil_config.validate_config(payload))

    payload["notify"] = {"enabled": True, "channel": "telegram", "chat_id": "42"}
    assert "notify.token_file must be an absolute path" in hil_config.validate_config(payload)

    payload["notify"] = {"enabled": True, "channel": "carrier-pigeon"}
    assert any("notify.channel must be one of" in error for error in hil_config.validate_config(payload))

    schema = json.loads((RIG / "hil-config.schema.json").read_text())
    # One channel, or a list of them: a rig that has always had one validates
    # exactly as it did, and one told to say things in two places says so.
    one, many = schema["properties"]["notify"]["oneOf"]
    assert one["properties"]["channel"]["enum"] == ["webhook", "telegram", "callmebot"]
    assert many["type"] == "array" and many["items"] == one
    assert "id" in one["properties"]


def test_a_rigs_owner_chooses_which_things_that_go_wrong_each_channel_says(tmp_path):
    """Which of the three a channel sends is its owner's, and so is whether it
    sends at all. Neither is a credential, so `notify tune` changes them
    without the bot token being handed over again -- and both name a channel,
    because a rig can hold several and "the channel" says nothing about which.
    """
    import json as _json

    payload = valid_config(tmp_path)
    payload["notify"] = [
        {"id": "1", "enabled": True, "channel": "telegram",
         "token_file": str(tmp_path / "telegram-token"), "chat_id": "8339907776"},
        {"id": "2", "enabled": True, "channel": "webhook",
         "webhook_url_file": str(tmp_path / "hook"), "format": "json"},
    ]
    assert hil_config.validate_config(payload) == []
    channels = hil_config.notify_channels(payload)
    assert [item["id"] for item in channels] == ["1", "2"]

    # One narrowed and the other turned off: the environment the services read
    # carries both, and says which is which.
    channels[0]["events"] = ["board_red"]
    channels[1]["enabled"] = False
    text = hil_config.runtime_env(hil_config.with_notify_channels(payload, channels))
    described = [line for line in text.splitlines() if line.startswith("ALTERIOM_HIL_NOTIFY_CHANNELS=")]
    assert len(described) == 1
    carried = _json.loads(described[0].split("=", 1)[1])
    assert [item["id"] for item in carried] == ["1"], "a channel that is off sends nothing"
    assert carried[0]["events"] == ["board_red"]
    # The first is still exported under the names a single channel has always
    # had, so anything reading those keeps working through the change.
    assert "ALTERIOM_HIL_NOTIFY_CHANNEL=telegram\n" in text
    assert "ALTERIOM_HIL_NOTIFY_EVENTS=board_red\n" in text

    # A rig that has always had one reads exactly as it did, and stays a
    # mapping when it is written back.
    one = valid_config(tmp_path)
    one["notify"] = {"enabled": True, "webhook_url_file": str(tmp_path / "hook")}
    assert hil_config.validate_config(one) == []
    [only] = hil_config.notify_channels(one)
    assert only["id"] == "1"
    assert isinstance(hil_config.with_notify_channels(one, [only])["notify"], dict)
    assert isinstance(hil_config.with_notify_channels(one, [only, {**only, "id": "2"}])["notify"], list)

    # Two channels cannot share an id, and each is still checked for what its
    # kind needs -- named, so the message says which one is wrong.
    payload["notify"] = [{"id": "1", "enabled": True, "channel": "telegram"},
                         {"id": "1", "enabled": True, "webhook_url_file": str(tmp_path / "hook")}]
    errors = hil_config.validate_config(payload)
    assert any("share the id 1" in error for error in errors)
    assert any("notify[1].token_file" in error for error in errors)
