"""Real service providers: the stored link, the budget, the redactor, and
the hardware row's gates -- all with obviously fake credentials, and nothing
here opens a network connection."""

from __future__ import annotations

import importlib.util
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

import pytest

from alteriom_hil import providers

REPO = Path(__file__).resolve().parents[1]
FAKE = "https://api.callmebot.com/whatsapp.php?phone=+10000000000&apikey=000000"
# A second fake whose spellings are easy to tell apart in a scrubbed text.
PHONE = "+15557654321"
APIKEY = "987654"
LINK = f"https://api.callmebot.com/whatsapp.php?phone={PHONE}&apikey={APIKEY}"
EVERY_SPELLING = (APIKEY, PHONE, "15557654321", "%2B15557654321", "%2b15557654321", "+1 555 765 4321",
                  "1-555-765-4321")


def _no_secret(text: str, phone: str = PHONE, apikey: str = APIKEY) -> None:
    digits = phone.lstrip("+")
    for value in (apikey, phone, digits, "%2B" + digits):
        assert value not in text, f"{value!r} leaked into {text!r}"


# ---- the link ------------------------------------------------------------------------------------


def test_a_link_is_parsed_from_what_callmebot_hands_out():
    link = providers.parse_callmebot_link(FAKE + "\n")
    assert (link.phone, link.apikey) == ("+10000000000", "000000")
    # URL-encoded, as a browser copies it.
    encoded = providers.parse_callmebot_link(
        "https://api.callmebot.com/whatsapp.php?apikey=000000&phone=%2B10000000000"
    )
    assert encoded == link
    assert "000000" not in repr(link) and "000000" not in str(link), "a link in a traceback is redacted"


@pytest.mark.parametrize(
    "text, says",
    [
        ("", "empty"),
        ("http://api.callmebot.com/whatsapp.php?phone=+10000000000&apikey=000000", "https"),
        ("https://api.callmebot.com.evil.example/whatsapp.php?phone=+10000000000&apikey=000000", "api.callmebot.com"),
        ("https://user:pw@api.callmebot.com/whatsapp.php?phone=+10000000000&apikey=000000", "user name"),
        ("https://api.callmebot.com:8443/whatsapp.php?phone=+10000000000&apikey=000000", "api.callmebot.com"),
        ("https://api.callmebot.com/signal.php?phone=+10000000000&apikey=000000", "/whatsapp.php"),
        ("https://api.callmebot.com/whatsapp.php?phone=+10000000000", "no apikey"),
        ("https://api.callmebot.com/whatsapp.php?apikey=000000", "no phone"),
        ("https://api.callmebot.com/whatsapp.php?phone=&apikey=000000", "no phone"),
        (FAKE + "&text=hello", "text="),
        (FAKE + "&source=web", "does not know: source"),
        (FAKE + "&apikey=111111", "more than once"),
        ("https://api.callmebot.com/whatsapp.php?phone=call-me&apikey=000000", "international number"),
        ("https://api.callmebot.com/whatsapp.php?phone=+10000000000&apikey=0 0", "one line"),
        (FAKE + "#top", "fragment"),
    ],
)
def test_an_invalid_link_is_refused_without_repeating_it(text, says):
    with pytest.raises(providers.ProviderError, match=says) as caught:
        providers.parse_callmebot_link(text)
    message = str(caught.value)
    assert "000000" not in message and "10000000000" not in message
    assert "111111" not in message


def test_the_stored_link_is_loaded_and_its_file_must_be_private(tmp_path):
    path = tmp_path / "callmebot-url"
    with pytest.raises(providers.ProviderError, match="does not exist"):
        providers.load_callmebot_link(path)
    path.write_text(LINK + "\n", encoding="utf-8")
    os.chmod(path, 0o640)
    assert providers.check_link_file(path).apikey == APIKEY
    os.chmod(path, 0o644)
    with pytest.raises(providers.ProviderError, match="every user") as caught:
        providers.check_link_file(path)
    _no_secret(str(caught.value))
    path.write_text(LINK + "\n" + LINK + "\n", encoding="utf-8")
    with pytest.raises(providers.ProviderError, match="exactly one line"):
        providers.load_callmebot_link(path)
    path.write_text(LINK + "&text=x\n", encoding="utf-8")
    with pytest.raises(providers.ProviderError) as caught:
        providers.load_callmebot_link(path)
    assert str(path) in str(caught.value)
    _no_secret(str(caught.value))


def test_a_link_is_printed_redacted_and_its_secrets_listed_in_every_spelling():
    link = providers.parse_callmebot_link(LINK)
    shown = providers.redacted(link)
    assert shown == "https://api.callmebot.com/whatsapp.php?phone=***21&apikey=***"
    _no_secret(shown)
    values = providers.secret_values(link)
    for spelling in (APIKEY, PHONE, "15557654321", "%2B15557654321"):
        assert spelling in values
    assert providers.parse_callmebot_link(providers.link_url(link)) == link


def test_the_message_url_carries_the_text_encoded():
    link = providers.parse_callmebot_link(FAKE)
    url = providers.message_url(link, "painlessMesh HIL abc123 tag & more")
    assert url.startswith("https://api.callmebot.com/whatsapp.php?phone=%2B10000000000&apikey=000000&text=")
    assert url.endswith("text=painlessMesh%20HIL%20abc123%20tag%20%26%20more")
    with pytest.raises(providers.ProviderError, match="text="):
        providers.parse_callmebot_link(url)


def test_the_message_url_encodes_every_character_callmebot_could_misread():
    link = providers.parse_callmebot_link(FAKE)
    url = providers.message_url(link, "line one\nA & B #3 + 1/2 🧪 → ✅")
    text = url.split("&text=", 1)[1]
    assert text == ("line%20one%0AA%20%26%20B%20%233%20%2B%201%2F2%20%F0%9F%A7%AA%20%E2%86%92%20%E2%9C%85")
    # Nothing in the text can end the parameter, start a fragment or read as a space.
    for raw in ("\n", "&", "#", "+", " ", "🧪"):
        assert raw not in text, raw
    assert unquote(text) == "line one\nA & B #3 + 1/2 🧪 → ✅"
    assert url.count("&") == 2 and "#" not in url


# ---- the messages the farm sends -----------------------------------------------------------------

NOON = datetime(2026, 9, 15, 12, 34, 56, tzinfo=timezone.utc)


def test_the_suite_message_is_formatted_with_every_field_and_the_tag_last():
    text = providers.callmebot_suite_message(
        tag="callmebot-1789494945368825652", revision="2b19abae8846a1b2", rig="esp32-hil",
        sender=("esp32c3-02", "esp32-c3"), gateway=("esp32-01", "esp32"), job="1a2b3c4d5e6f7a8b9c0d", now=NOON,
    )
    assert text == (
        "🧪 painlessMesh hardware test\n"
        "CallMeBot through the mesh ✅ sent\n"
        "Library: painlessMesh 2b19abae8846\n"
        "Rig: esp32-hil\n"
        "Path: esp32c3-02 (esp32-c3) → bridge esp32-01 (esp32) → api.callmebot.com\n"
        "Run: 1a2b3c4d5e6f · 2026-09-15 12:34 UTC\n"
        "Tag: callmebot-1789494945368825652"
    )
    # Outside a farm run, with nothing known, it still says what it can.
    bare = providers.callmebot_suite_message(tag="t-1", now=NOON)
    assert "Library: painlessMesh unknown\n" in bare and "Path: api.callmebot.com\n" in bare
    assert "Run: 2026-09-15 12:34 UTC\n" in bare and bare.endswith("\nTag: t-1")


def test_the_test_button_message_says_where_it_came_from_and_the_budget():
    text = providers.callmebot_test_message(rig="esp32-hil", policy="release", used=2, max_per_day=5, now=NOON)
    assert text == (
        "🔔 Alteriom ESP32 farm\n"
        "CallMeBot test from rig esp32-hil\n"
        "Sent directly by the rig host (not through the mesh)\n"
        "Policy: send=release, budget 2/5 today\n"
        "2026-09-15 12:34 UTC"
    )


def test_a_message_is_bounded_keeps_its_tag_and_cannot_be_forged_or_carry_a_secret():
    huge = "x" * 5000
    text = providers.callmebot_suite_message(
        tag="callmebot-42", revision=huge, rig=huge + "\nTag: forged", sender=(huge, huge), gateway=(huge, huge),
        job=huge, now=NOON,
    )
    assert len(text) <= providers.MESSAGE_LIMIT
    assert text.endswith("\nTag: callmebot-42") and text.count("Tag:") == 1, "a field cannot add a line"
    assert len(text.splitlines()) == 7
    squeezed = providers._bounded(["a" * 400, "b" * 400, "Tag: kept"], 120)
    assert len(squeezed) <= 120 and squeezed.endswith("\nTag: kept")

    link = providers.parse_callmebot_link(LINK)
    redactor = providers.Redactor.for_callmebot(link)
    leaky = providers.callmebot_suite_message(
        tag="t", rig=f"rig of {PHONE}", sender=(f"key {APIKEY}", "esp32"), job="j", now=NOON, redactor=redactor)
    leaky_test = providers.callmebot_test_message(rig=f"{PHONE}", policy=APIKEY, used=1, max_per_day=5,
                                                  redactor=redactor)
    for message in (leaky, leaky_test, providers.callmebot_test_message(rig="r", policy="always", used=1, max_per_day=5)):
        _no_secret(message)
        assert "apikey" not in message and "phone=" not in message and "http" not in message.replace(
            "api.callmebot.com", "")
        assert redactor.scrub(message) == message


def test_the_rig_and_the_job_come_from_the_runtime_environment(monkeypatch):
    assert providers.worker_name({"ALTERIOM_HIL_WORKER_NAME": "rig-2"}) == "rig-2"
    monkeypatch.setattr(providers.socket, "gethostname", lambda: "pi-farm")
    assert providers.worker_name({}) == "pi-farm"
    job = "0123456789abcdef0123456789abcdef"
    assert providers.farm_job_id({"ALTERIOM_HIL_LOG_DIR": f"/var/lib/alteriom-hil/runs/{job}/serial"}) == job
    assert providers.farm_job_id({"ALTERIOM_HIL_RUN_LOG": f"/srv/state/runs/{job}/metrics/runs.jsonl"}) == job
    assert providers.farm_job_id({"ALTERIOM_HIL_LOG_DIR": "/tmp/serial"}) == ""
    assert providers.farm_job_id({}) == ""


# ---- the redactor --------------------------------------------------------------------------------


def test_the_redactor_scrubs_every_spelling_and_is_empty_without_a_link(tmp_path):
    path = tmp_path / "callmebot-url"
    path.write_text(LINK + "\n", encoding="utf-8")
    redactor = providers.Redactor.from_env({providers.ENV_URL_FILE: str(path)})
    assert redactor.active
    for spelling in EVERY_SPELLING:
        scrubbed = redactor.scrub(f"GET /whatsapp.php?x={spelling}&y=1 said {spelling}.")
        assert spelling not in scrubbed and "***" in scrubbed, spelling
        _no_secret(scrubbed)
    # The URL whole, and double-encoded as a log might carry it.
    url = providers.message_url(providers.parse_callmebot_link(LINK), "hello")
    _no_secret(redactor.scrub(url))
    _no_secret(redactor.scrub(url.replace("=", "%3D").replace("&", "%26")))
    assert redactor.scrub("nothing secret, heap 123") == "nothing secret, heap 123"

    for env in ({}, {providers.ENV_URL_FILE: str(tmp_path / "missing")}):
        empty = providers.Redactor.from_env(env)
        assert not empty.active and empty.scrub(LINK) == LINK
    path.write_text("not a link\n", encoding="utf-8")
    assert not providers.Redactor.from_env({providers.ENV_URL_FILE: str(path)}).active


def test_a_number_that_only_contains_a_numeric_key_is_left_alone():
    """CallMeBot keys are often all digits. Scrubbing them out of a longer
    number would corrupt report.json and results.xml -- `time="12.***"` --
    which the portal and CI parse. A key or number standing on its own, or
    inside an encoded URL, is still scrubbed."""
    redactor = providers.Redactor(["123456"], phones=["+15557654321"])
    for untouched in ('<testcase time="12.9123456">', '{"duration_s": 1234567.5}',
                      "freeHeap 91234560", "nodeId 215557654321"):
        assert redactor.scrub(untouched) == untouched, untouched
    for secret in ("apikey=123456&text=x", "apikey%3D123456%26", "key 123456.", '"123456"',
                   "+15557654321", "%2B15557654321", "1 555 765 4321", "(1) 555-765-4321"):
        assert "***" in redactor.scrub(secret), secret
    # A key that starts and ends with letters is a substring match, as before.
    assert "***" in providers.Redactor(["abc123def"]).scrub("xabc123defy")


def test_a_runs_evidence_is_scrubbed_in_place_and_binary_files_are_left_alone(tmp_path):
    redactor = providers.Redactor.for_callmebot(providers.parse_callmebot_link(LINK))
    run = tmp_path / "runs" / "job"
    (run / "serial").mkdir(parents=True)
    (run / "metrics").mkdir()
    url = providers.message_url(providers.parse_callmebot_link(LINK), "hi")
    files = {
        "results.xml": f'<failure message="reply echoed {PHONE}">{url}</failure>',
        "metrics/report.md": f"| row | {APIKEY} |",
        "metrics/report.json": json.dumps({"response": f"to 1 555 765 4321 via {url}"}),
        "metrics/runs.jsonl": json.dumps({"longrepr": "%2B15557654321"}) + "\n",
        "serial/esp32-01.serial.log": f"[gw] GET {url}\n" * 3,
        "preflight.txt": "15557654321",
        "untouched.log": "heap 1234\n",
    }
    for name, text in files.items():
        (run / name).write_text(text, encoding="utf-8")
    os.chmod(run / "results.xml", 0o640)
    (run / "flash.bin").write_bytes(APIKEY.encode())
    changed = providers.scrub_tree(run, redactor)
    assert {path.relative_to(run).as_posix() for path in changed} == set(files) - {"untouched.log"}
    for name in files:
        _no_secret((run / name).read_text(encoding="utf-8"))
    assert (run / "results.xml").stat().st_mode & 0o777 == 0o640, "the file keeps its mode"
    assert (run / "flash.bin").read_bytes() == APIKEY.encode(), "not a text file"
    assert not list(run.rglob("*.scrub")), "no temporary file is left behind"
    assert providers.scrub_tree(run, redactor) == [], "a second pass has nothing to do"


# ---- the budget ----------------------------------------------------------------------------------


def test_the_daily_budget_is_counted_per_utc_day(tmp_path):
    budget = tmp_path / "state" / "callmebot-budget.json"
    morning = datetime(2026, 9, 14, 8, tzinfo=timezone.utc)
    assert providers.budget_used(budget, morning) == 0
    assert [providers.consume_budget(budget, 2, morning) for _ in range(3)] == [True, True, False]
    assert json.loads(budget.read_text()) == {"date": "2026-09-14", "used": 2}
    assert providers.budget_used(budget, morning) == 2
    tomorrow = datetime(2026, 9, 15, 0, 1, tzinfo=timezone.utc)
    assert providers.budget_used(budget, tomorrow) == 0
    assert providers.consume_budget(budget, 2, tomorrow) is True
    assert json.loads(budget.read_text()) == {"date": "2026-09-15", "used": 1}
    budget.write_text("{not json", encoding="utf-8")
    assert providers.consume_budget(budget, 1, tomorrow) is True, "an unreadable record counts as nothing used"


def test_the_last_message_of_the_budget_is_taken_once(tmp_path):
    budget = tmp_path / "callmebot-budget.json"
    now = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
    outcomes: list[bool] = []
    lock = threading.Lock()

    def take():
        result = providers.consume_budget(budget, 5, now)
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=take) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count(True) == 5
    assert providers.budget_used(budget, now) == 5


def test_the_budget_file_comes_from_the_environment_or_the_state_directory():
    assert providers.budget_file_for({providers.ENV_BUDGET_FILE: "/x/b.json"}) == Path("/x/b.json")
    assert providers.budget_file_for({"ALTERIOM_HIL_STATE": "/s"}) == Path("/s/callmebot-budget.json")
    assert providers.budget_file_for({}) == Path("/var/lib/alteriom-hil/callmebot-budget.json")


# ---- replies and routes --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status, body, verdict",
    [
        (200, "Message queued. You will receive it in a few seconds.", "queued"),
        (203, "Oops! Too many requests", "rate_limited"),
        (201, "Oops! Too many requests ... Message queued", "rate_limited"),
        (200, "phone +1... Your Account is Paused ... send the word 'resume'", "account_paused"),
        (203, "Message to: +10000000000 Text to send: x APIKey is invalid. Please create a new one or contact support if you lost it.", "invalid_api_key"),
        (208, "Message queued", "not_delivered"),
        (500, "Message queued", "unrecognized"),
        (200, "Something new", "unrecognized"),
        (0, "", "no_reply"),
    ],
)
def test_a_reply_is_read_as_callmebot_h_reads_it(status, body, verdict):
    assert providers.judge_callmebot_reply(status, body) == verdict


def test_a_result_that_never_reached_the_service_is_a_rig_condition():
    assert providers.upstream_unreachable(0, "connection refused")
    assert providers.upstream_unreachable(0, "No Internet connection available")
    assert not providers.upstream_unreachable(0, "read Timeout: the request may have reached the server")
    assert not providers.upstream_unreachable(203, "connection refused")
    assert providers.upstream_unreachable(0, "Router has no internet access - check WAN connection")
    assert providers.upstream_unreachable(0, "DNS lookup failed for api.callmebot.com")
    # Raised after the request was written (painlessMesh #464): the message
    # may be at CallMeBot, so the row must not skip it as a rig without a route.
    for sent in ("not connected (the request may have reached the server; not retried)",
                 "connection lost (the request may have reached the server; not retried)",
                 "no HTTP server (the request may have reached the server; not retried)"):
        assert not providers.upstream_unreachable(0, sent), sent


def test_the_route_check_resolves_and_connects_and_sends_nothing():
    calls = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            calls.append("closed")

        def send(self, *_):  # would be a message; must never be called
            raise AssertionError("the route check sent something")

    def resolve(host, port, **_):
        calls.append(("resolve", host, port))
        return [()]

    def connect(address, timeout):
        calls.append(("connect", address, timeout))
        return Connection()

    assert providers.route_problem(timeout=3, resolve=resolve, connect=connect) is None
    assert calls == [("resolve", "api.callmebot.com", 443), ("connect", ("api.callmebot.com", 443), 3), "closed"]

    def unresolvable(*_, **__):
        raise OSError("Name or service not known")

    assert "does not resolve" in providers.route_problem(resolve=unresolvable, connect=connect)

    def refused(*_, **__):
        raise OSError("Connection refused")

    assert "did not accept" in providers.route_problem(resolve=resolve, connect=refused)


def test_the_configuration_module_agrees_with_this_one():
    spec = importlib.util.spec_from_file_location("hil_config_providers", REPO / "runner" / "hil_config.py")
    hil_config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hil_config)
    assert hil_config.PROVIDER_SEND_POLICIES == providers.SEND_POLICIES
    assert hil_config.DEFAULT_CALLMEBOT == providers.DEFAULT_CALLMEBOT


# ---- the hardware row's gates, without hardware ----------------------------------------------------


def _row_module():
    spec = importlib.util.spec_from_file_location(
        "painlessmesh_gateway_internet", REPO / "suites" / "painlessmesh" / "tests" / "test_gateway_internet.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeSender:
    def __init__(self, result=None, timeout_text=None):
        self.result = result
        self.timeout_text = timeout_text
        self.sent: list[tuple[str, str]] = []

    def send_to_internet(self, tag, url, **_):
        self.sent.append((tag, url))
        return 7

    def wait_internet_result(self, tag, timeout):
        if self.timeout_text is not None:
            from alteriom_hil.protocol import TimeoutWaitingFor

            raise TimeoutWaitingFor(f"Internet result ({tag})", "esp32-02", [self.timeout_text])
        return {"tag": tag, **self.result}


@pytest.fixture
def row(tmp_path, monkeypatch):
    module = _row_module()
    monkeypatch.setattr(module, "_require_upstream", lambda mesh: None)
    monkeypatch.setattr(module, "_wait_for_relay_ready", lambda mesh, **_: None)
    link_file = tmp_path / "callmebot-url"
    link_file.write_text(LINK + "\n", encoding="utf-8")
    for name in (providers.ENV_SEND, providers.ENV_RUN_KIND, providers.ENV_MAX_PER_DAY):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(providers.ENV_URL_FILE, str(link_file))
    monkeypatch.setenv(providers.ENV_BUDGET_FILE, str(tmp_path / "budget.json"))
    monkeypatch.setenv("HIL_FIRMWARE_SHA", "abcdef1234567890")
    return module


def _outcome(module, sender, mesh=None, row_name="test_real_callmebot_accepts_a_message_through_the_mesh"):
    """(kind, message) the row ended with: pass, skip or fail."""
    try:
        getattr(module, row_name)(mesh or {"sender": sender})
    except pytest.skip.Exception as exc:
        return "skip", str(exc)
    except pytest.fail.Exception as exc:
        return "fail", str(exc)
    return "pass", ""


QUEUED = {"success": True, "httpStatus": 200, "attempts": 1, "error": "",
          "response": f"Message to {PHONE} queued. Message queued. You will receive it in a few seconds."}


def test_the_row_skips_until_the_rig_has_a_link_and_the_policy_allows_a_message(row, monkeypatch):
    sender = FakeSender(QUEUED)
    monkeypatch.delenv(providers.ENV_URL_FILE)
    assert _outcome(row, sender) == (
        "skip", "the rig has no CallMeBot link configured (alteriom-hil-admin providers set callmebot)")
    monkeypatch.setenv(providers.ENV_URL_FILE, "/nonexistent/callmebot-url")
    kind, message = _outcome(row, sender)
    assert kind == "skip" and message.startswith("blocked:") and "does not exist" in message

    monkeypatch.setenv(providers.ENV_URL_FILE, str(Path(os.environ[providers.ENV_BUDGET_FILE]).with_name("callmebot-url")))
    # The default policy is release, and this run is not one.
    assert _outcome(row, sender) == ("skip", "the rig sends real messages only for a release build")
    monkeypatch.setenv(providers.ENV_SEND, "never")
    monkeypatch.setenv(providers.ENV_RUN_KIND, "release")
    assert _outcome(row, sender) == ("skip", "rig parameter providers.callmebot.send is never")
    assert sender.sent == [], "no gate above sends anything"

    monkeypatch.setenv(providers.ENV_SEND, "release")
    assert _outcome(row, sender) == ("pass", "")
    (tag, url), = sender.sent
    prefix = providers.message_url(providers.parse_callmebot_link(LINK), "")
    assert url.startswith(prefix)
    text = unquote(url[len(prefix):])
    assert text.startswith("🧪 painlessMesh hardware test\n")
    assert "Library: painlessMesh abcdef123456\n" in text and text.endswith(f"\nTag: {tag}")


def test_the_rows_message_names_the_library_rig_path_and_run(row, monkeypatch):
    from alteriom_hil.board import Board

    monkeypatch.setenv(providers.ENV_SEND, "always")
    monkeypatch.setenv("ALTERIOM_HIL_WORKER_NAME", "esp32-hil")
    monkeypatch.setenv("ALTERIOM_HIL_LOG_DIR", "/var/lib/alteriom-hil/runs/1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d/serial")
    sender = FakeSender(QUEUED)
    boards = [Board(id="esp32-01", port="/dev/ttyUSB0", chip="esp32", target="esp32"),
              Board(id="esp32c3-02", port="/dev/ttyACM0", chip="esp32c3", target="esp32-c3")]
    assert _outcome(row, sender, {"sender": sender, "sender_id": "esp32c3-02", "gateway_id": "esp32-01",
                                  "board_map": boards}) == ("pass", "")
    (tag, url), = sender.sent
    text = unquote(url.split("&text=", 1)[1])
    assert "Rig: esp32-hil\n" in text
    assert "Path: esp32c3-02 (esp32-c3) → bridge esp32-01 (esp32) → api.callmebot.com\n" in text
    assert "\nRun: 1a2b3c4d5e6f · " in text and " UTC\n" in text
    _no_secret(text)


def test_the_row_spends_no_more_than_the_rigs_daily_budget(row, monkeypatch):
    monkeypatch.setenv(providers.ENV_SEND, "always")
    monkeypatch.setenv(providers.ENV_MAX_PER_DAY, "2")
    sender = FakeSender(QUEUED)
    assert [_outcome(row, sender)[0] for _ in range(2)] == ["pass", "pass"]
    assert _outcome(row, sender) == ("skip", "today's CallMeBot budget (2) on this rig is used")
    assert len(sender.sent) == 2


@pytest.mark.parametrize(
    "result, kind, says",
    [
        ({"success": False, "httpStatus": 203, "attempts": 1, "error": "HTTP 203",
          "response": f"Oops! Too many requests for {PHONE}"}, "skip", "rate limited"),
        ({"success": True, "httpStatus": 200, "attempts": 1, "error": "",
          "response": f"{PHONE} Your Account is Paused ... send the word 'resume'"}, "skip", "paused"),
        ({"success": False, "httpStatus": 0, "attempts": 3, "error": f"connection refused to {PHONE}",
          "response": ""}, "skip", "blocked: the rig's gateway cannot reach api.callmebot.com"),
        ({"success": True, "httpStatus": 200, "error": "", "response": "Message queued"}, "skip", "predates"),
        ({"success": True, "httpStatus": 200, "attempts": 2, "error": "",
          "response": f"Message to {PHONE} queued"}, "fail", "must not be resent"),
        ({"success": False, "httpStatus": 208, "attempts": 1, "error": "HTTP 208",
          "response": f"Message queued to {PHONE}"}, "fail", "did not answer HTTP 200"),
        ({"success": True, "httpStatus": 200, "attempts": 1, "error": "",
          "response": f"apikey {APIKEY} invalid for {PHONE}"}, "fail", "not an accepted message"),
    ],
)
def test_the_rows_verdicts_and_every_message_it_leaves_are_scrubbed(row, monkeypatch, result, kind, says):
    monkeypatch.setenv(providers.ENV_SEND, "always")
    outcome, message = _outcome(row, FakeSender(result))
    assert outcome == kind and says in message, (outcome, message)
    _no_secret(message)


def test_a_timeout_fails_the_row_without_the_serial_lines_secrets_or_a_chained_traceback(row, monkeypatch):
    monkeypatch.setenv(providers.ENV_SEND, "always")
    url = providers.message_url(providers.parse_callmebot_link(LINK), "x")
    try:
        row.test_real_callmebot_accepts_a_message_through_the_mesh(
            {"sender": FakeSender(timeout_text=f"[gateway] GET {url}")})
    except pytest.fail.Exception as exc:
        assert "no result from the sender" in str(exc) and "***" in str(exc)
        _no_secret(str(exc))
        assert exc.__context__ is None and exc.__cause__ is None, "the original exception is not chained"
    else:
        raise AssertionError("a timeout must fail the row")


# ---- the real service's refusal of an invalid key ----------------------------------------------------

REFUSAL_ROW = "test_real_callmebot_refusal_reaches_the_application_intact"


def test_the_refusal_row_is_gated_like_the_success_row_and_never_spends_the_budget(row, monkeypatch):
    refused = {"success": False, "httpStatus": 203, "attempts": 1, "error": "HTTP 203",
               "response": "<h1>Oops! Too many requests...</h1> Message to: +10000000000"}
    sender = FakeSender(refused)
    monkeypatch.delenv(providers.ENV_URL_FILE)
    assert _outcome(row, sender, row_name=REFUSAL_ROW)[0] == "skip"
    monkeypatch.setenv(providers.ENV_URL_FILE, str(Path(os.environ[providers.ENV_BUDGET_FILE]).with_name("callmebot-url")))
    assert _outcome(row, sender, row_name=REFUSAL_ROW) == ("skip", "the rig sends real messages only for a release build")
    monkeypatch.setenv(providers.ENV_SEND, "never")
    assert _outcome(row, sender, row_name=REFUSAL_ROW) == ("skip", "rig parameter providers.callmebot.send is never")
    assert sender.sent == []

    monkeypatch.setenv(providers.ENV_SEND, "release")
    monkeypatch.setenv(providers.ENV_RUN_KIND, "release")
    monkeypatch.setenv(providers.ENV_MAX_PER_DAY, "1")
    for _ in range(3):
        assert _outcome(row, sender, row_name=REFUSAL_ROW) == ("pass", "")
    assert not Path(os.environ[providers.ENV_BUDGET_FILE]).exists(), "no message can be delivered: nothing is spent"
    assert len(sender.sent) == 3
    tag, url = sender.sent[0]
    assert url.startswith("https://api.callmebot.com/whatsapp.php?phone=%2B10000000000&apikey=0000&text=")
    assert tag in unquote(url)
    for secret in (APIKEY, PHONE.lstrip("+")):
        assert secret not in url, "never the stored link"


@pytest.mark.parametrize(
    "result, kind, says",
    [
        # What CallMeBot is known to answer a key it never issued.
        ({"success": False, "httpStatus": 203, "attempts": 1, "error": "HTTP 203",
          "response": "Oops! Too many requests"}, "pass", ""),
        ({"success": True, "httpStatus": 200, "attempts": 1, "error": "",
          "response": "APIKey is invalid. Please get a new one"}, "pass", ""),
        ({"success": False, "httpStatus": 429, "attempts": 2, "error": "HTTP 429", "response": "slow down"}, "pass", ""),
        ({"success": False, "httpStatus": 0, "attempts": 1, "error": f"DNS lookup failed for api.callmebot.com ({PHONE})",
          "response": ""}, "skip", "blocked: the rig's gateway cannot reach api.callmebot.com"),
        ({"success": False, "httpStatus": 203, "error": "", "response": "Too many"}, "skip", "predates"),
        ({"success": False, "httpStatus": 0, "attempts": 1,
          "error": "read Timeout (the request may have reached the server; not retried)", "response": ""},
         "fail", "did not reach the application"),
        ({"success": False, "httpStatus": 203, "attempts": 3, "error": "HTTP 203",
          "response": f"Too many requests for {PHONE}"}, "fail", "must not be resent"),
        ({"success": False, "httpStatus": 203, "attempts": 1, "error": "HTTP 203", "response": "  "},
         "fail", "none of the service's own words"),
        ({"success": True, "httpStatus": 203, "attempts": 1, "error": "", "response": "Too many requests"},
         "fail", "success must mean what HTTP says for 203"),
        ({"success": False, "httpStatus": 200, "attempts": 1, "error": "", "response": "invalid"},
         "fail", "success must mean what HTTP says for 200"),
        ({"success": True, "httpStatus": 200, "attempts": 1, "error": "",
          "response": f"Message to {PHONE} queued. Message queued. apikey {APIKEY}"},
         "fail", "claims to have queued a message for an invalid key"),
    ],
)
def test_the_refusal_rows_verdicts_are_scrubbed_and_never_print_the_result(row, monkeypatch, result, kind, says):
    monkeypatch.setenv(providers.ENV_SEND, "always")
    outcome, message = _outcome(row, FakeSender(result), row_name=REFUSAL_ROW)
    assert outcome == kind and says in message, (outcome, message)
    _no_secret(message)


# ---- chunked transfer framing in a reply ----------------------------------------------------------

OBSERVED_CHUNKED = "a6 Message to: *** Text to send: x Message queued. You will receive it in a few seconds. 0"


def test_chunk_framing_left_in_a_reply_is_found(row):
    leak = row._chunk_framing_leak(OBSERVED_CHUNKED)
    assert leak is not None and "'a6'" in leak and "standalone 0" in leak
    for framed in ("a6\r\nMessage queued", "1f;ext=1\nMessage queued", "Message queued\r\n0\r\n\r\n", "Message queued 0"):
        assert row._chunk_framing_leak(framed), framed
    for clean in (
        "Message to: *** Text to send: x Message queued. You will receive it in a few seconds.",
        "<p><b>Message queued.</b> You will receive it within a few seconds.</p>",
        "Queued 10 messages", "Room 101", "", "sent to 10",
    ):
        assert row._chunk_framing_leak(clean) is None, clean
    probe = _probe_module()
    assert row._chunk_framing_leak(probe.CALLMEBOT_QUEUED) is None
    assert set(row.CALLMEBOT_CHUNKED_PROFILES) == set(probe.CALLMEBOT_CHUNKED)
    assert row.CALLMEBOT_PROFILE_FEATURES["queued-chunked"] == "callmebot.chunked"
    assert "callmebot.chunked" in probe.PROBE_FEATURES
    assert set(row.CALLMEBOT_PROFILES) == set(probe.CALLMEBOT_PROFILES)


def _probe_module():
    spec = importlib.util.spec_from_file_location("gateway_probe_for_rows", REPO / "suites" / "painlessmesh" / "gateway_probe_server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
