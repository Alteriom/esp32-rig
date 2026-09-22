"""The CI client submits whichever profile it is told to.

It used to hardcode {"profile": "painlessmesh"} and require simulation
evidence, so the only tool for asking the farm to run something could ask for
exactly one thing. The farm's second consumer was submitted by hand over ssh
for its first several runs because of it.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

CLIENT_PATH = Path(__file__).resolve().parents[1] / "runner" / "ci_farm_client.py"
SPEC = importlib.util.spec_from_file_location("ci_farm_client", CLIENT_PATH)
ci_farm_client = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules.setdefault("ci_farm_client", ci_farm_client)
SPEC.loader.exec_module(ci_farm_client)


def parse(argv: list[str]):
    """The parsed arguments, the way main() would see them, without a farm."""
    import argparse

    captured = {}
    real_parse = argparse.ArgumentParser.parse_args

    def grab(self, args=None, namespace=None):
        ns = real_parse(self, args, namespace)
        captured["ns"] = ns
        # Stop main() before it reads the token file and talks to the service.
        raise SystemExit(0)

    argparse.ArgumentParser.parse_args = grab
    try:
        with pytest.raises(SystemExit):
            ci_farm_client.main(argv)
    finally:
        argparse.ArgumentParser.parse_args = real_parse
    return captured["ns"]


BASE = ["--token-file", "t", "--ref", "abc123", "--target", "esp32", "--out", "o.json", "--evidence-dir", "e"]


@pytest.fixture(autouse=True)
def _no_actor_from_the_job_running_these_tests(monkeypatch):
    """The client sends who started the run, read from GITHUB_ACTOR -- which
    the runner executing *these tests* sets too. Every exact request below
    would pass on a laptop and fail in hal-ci. A test that wants an actor
    sets one."""
    monkeypatch.delenv("GITHUB_ACTOR", raising=False)
    monkeypatch.delenv("GITHUB_TRIGGERING_ACTOR", raising=False)


def test_a_key_goes_only_over_https_or_to_loopback():
    ci_farm_client.check_base_url("http://127.0.0.1:8090")
    ci_farm_client.check_base_url("https://espfarm.alteriom.net")
    with pytest.raises(SystemExit, match="must be https"):
        ci_farm_client.check_base_url("http://espfarm.alteriom.net")


def test_the_profile_is_whatever_was_asked_for():
    ns = parse(BASE + ["--profile", "alteriom-firmware"])
    assert ci_farm_client.build_request(ns) == {
        "profile": "alteriom-firmware",
        "ref": "abc123",
        "targets": ["esp32"],
    }


def test_the_default_profile_keeps_the_pre_profile_behaviour():
    """painlessMesh's pipeline calls this without --profile and must keep
    getting painlessMesh."""
    assert ci_farm_client.build_request(parse(BASE))["profile"] == "painlessmesh"


def test_simulation_evidence_is_attached_only_when_given(tmp_path):
    evidence = tmp_path / "sim.json"
    evidence.write_text(json.dumps({"schema": 1, "painlessmesh_sha": "a" * 40}), encoding="utf-8")

    without = ci_farm_client.build_request(parse(BASE))
    assert "simulation" not in without, "a profile with no simulator gate must not send an empty one"

    with_it = ci_farm_client.build_request(parse(BASE + ["--simulation-evidence", str(evidence)]))
    assert with_it["simulation"]["painlessmesh_sha"] == "a" * 40


def test_several_targets_are_a_list():
    ns = parse(BASE + ["--target", "esp32-s3", "--target", "esp8266"])
    assert ci_farm_client.build_request(ns)["targets"] == ["esp32", "esp32-s3", "esp8266"]


def _build_failed_job(log_tail: str) -> dict:
    """What the service hands back when the consumer's build fails: no
    report, no junit, the reason only in log_tail."""
    return {
        "id": "e62f548a",
        "status": "failed",
        "progress": [
            {"name": "build", "label": "Build artifacts", "status": "failed", "summary": "Artifact build failed"},
            {"name": "discover", "label": "Discover hardware", "status": "pending"},
        ],
        "result": {
            "failed_stage": "build",
            "detail": "Build command failed: Command '[python, scripts/build_hil.py]' returned non-zero exit status 1.",
        },
        "artifacts": {
            "junit": {"available": False, "path": "/nowhere/results.xml"},
            "report_markdown": {"available": False, "path": "/nowhere/report.md"},
        },
        "log_tail": log_tail,
    }


def test_evidence_keeps_every_family_image_and_one_copy_of_each_serial_log(tmp_path):
    """Every family's image is called flash-image.bin, so a three-family run
    handed the consumer one binary -- whichever was copied last -- with no
    family on it; and each serial log arrived twice, flat and under serial/.
    The evidence directory now mirrors the artifact directory (manifest
    beside <family>/flash-image.bin, so it is flashable as it is), keeps the
    logs in one place, and names the job log for what it is."""
    farm = tmp_path / "farm"
    images = {}
    for family, payload in (("esp32", b"classic"), ("esp32-c3", b"riscv"), ("esp32-s3", b"xtensa-s3")):
        image = farm / "artifacts" / family / "flash-image.bin"
        image.parent.mkdir(parents=True)
        image.write_bytes(payload)
        images[family] = image
    manifest = farm / "artifacts" / "manifest.json"
    manifest.write_text('{"schema": 2}', encoding="utf-8")
    serial = farm / "runs" / "serial" / "esp32-03.serial.log"
    serial.parent.mkdir(parents=True)
    serial.write_text("rst:0x1\n", encoding="utf-8")
    queue = farm / "runs" / "mqtt" / "queue.jsonl"
    queue.parent.mkdir(parents=True)
    queue.write_text('{"topic": "alteriom/gateways/G1/status"}\n', encoding="utf-8")
    log = farm / "logs" / "0123abcd.log"
    log.parent.mkdir(parents=True)
    log.write_text("$ pio run\n", encoding="utf-8")
    report = farm / "runs" / "metrics" / "report.md"
    report.parent.mkdir(parents=True)
    report.write_text("# HIL\n", encoding="utf-8")

    job = {
        "id": "0123abcd",
        "status": "passed",
        "result": {"report": str(report)},
        "artifacts": {
            **{f"firmware:{family}": {"available": True, "path": str(path)} for family, path in images.items()},
            "manifest": {"available": True, "path": str(manifest)},
            "serial:esp32-03": {"available": True, "path": str(serial)},
            "mqtt:queue": {"available": True, "path": str(queue)},
            "log": {"available": True, "path": str(log)},
            "report_markdown": {"available": True, "path": str(report)},
            "preflight": {"available": False, "path": str(farm / "runs" / "preflight.json")},
        },
    }
    evidence = tmp_path / "hil-evidence"
    ci_farm_client.copy_evidence(job, evidence)

    for family, payload in (("esp32", b"classic"), ("esp32-c3", b"riscv"), ("esp32-s3", b"xtensa-s3")):
        assert (evidence / family / "flash-image.bin").read_bytes() == payload, family
    assert not (evidence / "flash-image.bin").exists(), "no unlabelled image"
    assert (evidence / "manifest.json").is_file()
    assert [p.relative_to(evidence).as_posix() for p in evidence.rglob("*.serial.log")] == ["serial/esp32-03.serial.log"]
    assert (evidence / "mqtt" / "queue.jsonl").is_file(), "the queue capture sits beside the serial logs"
    assert (evidence / "farm-job.log").read_text(encoding="utf-8") == "$ pio run\n"
    assert not (evidence / "0123abcd.log").exists()
    assert (evidence / "report.md").read_text(encoding="utf-8") == "# HIL\n"
    assert not (evidence / "preflight.json").exists(), "unavailable artifacts are not invented"


def test_evidence_is_downloaded_by_name_from_wherever_the_farm_is(tmp_path, monkeypatch):
    """The paths in a job are the service's. The first canary through the
    portal passed on every board and then failed in the client, which went
    looking for a portal path on the Pi. Every artifact comes through the API
    now, named as the service lists it, and lands where a local copy did."""
    served = {
        "firmware:esp32-c3": b"riscv",
        "serial:esp32-03": b"rst:0x1\n",
        "log": b"$ pio run\n",
    }
    urls = []

    class Body:
        def __init__(self, data):
            self.data = data

        def read(self, *_):
            return self.data

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def fake(request, timeout=None):
        urls.append(request.full_url)
        assert request.get_header("Authorization") == "Bearer k"
        from urllib.parse import unquote
        name = unquote(request.full_url.rsplit("/", 1)[1])
        if name not in served:
            from urllib.error import HTTPError
            raise HTTPError(request.full_url, 404, "gone", {}, io.BytesIO(b'{"error": "pruned"}'))
        return Body(served[name])

    monkeypatch.setattr(ci_farm_client, "urlopen", fake)
    portal = "/var/lib/alteriom-hil"  # a path on the portal, not here
    job = {
        "id": "f" * 32,
        "status": "passed",
        "artifacts": {
            "firmware:esp32-c3": {"available": True, "path": f"{portal}/artifacts/x/esp32-c3/flash-image.bin"},
            "serial:esp32-03": {"available": True, "path": f"{portal}/runs/x/serial/esp32-03.serial.log"},
            "log": {"available": True, "path": f"{portal}/logs/x.log"},
            "board_health": {"available": True, "path": f"{portal}/runs/x/serial/board-health.json"},
        },
    }
    evidence = tmp_path / "hil-evidence"
    fetch = ci_farm_client.artifact_fetcher("https://espfarm.example", "k", job["id"])
    ci_farm_client.copy_evidence(job, evidence, fetch)

    assert (evidence / "esp32-c3" / "flash-image.bin").read_bytes() == b"riscv"
    assert (evidence / "serial" / "esp32-03.serial.log").read_bytes() == b"rst:0x1\n"
    assert (evidence / "farm-job.log").read_bytes() == b"$ pio run\n"
    assert f"https://espfarm.example/api/v1/jobs/{'f' * 32}/artifacts/serial%3Aesp32-03" in urls
    # One the service no longer has is left out, not the run's verdict.
    assert not (evidence / "board-health.json").exists()


def test_a_queued_run_says_why_it_waits_once_per_reason(tmp_path, monkeypatch, capsys):
    """Through the portal a run can wait for a worker, and a poll that prints
    nothing for most of an hour reads, in the job log, as a hang. The client
    says the farm's own reason, when it changes."""
    job_id = "a" * 32
    polls = iter(["queued", "queued", "queued", "passed"])
    reasons = iter(["no worker is connected", "no worker is connected", "waiting for 1 x esp32-c3: in use by run 1a2b3c4d"])

    class Body:
        def __init__(self, payload):
            self.data = json.dumps(payload).encode()

        def read(self, *_):
            return self.data

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def fake(request, timeout=None):
        path = request.full_url.split("://", 1)[1].split("/", 1)[1]
        if path == "api/v1/suites":
            return Body({"id": job_id, "status": "queued", "progress": []})
        if path == f"api/v1/jobs/{job_id}":
            return Body({"id": job_id, "status": next(polls), "progress": [], "artifacts": {}})
        if path == "api/v1/status":
            return Body({"queue": {"waiting": {job_id: next(reasons)}}})
        raise AssertionError(path)

    monkeypatch.setattr(ci_farm_client, "urlopen", fake)
    token = tmp_path / "key"
    token.write_text("k\n")
    code = ci_farm_client.main([
        "--base-url", "https://espfarm.example", "--token-file", str(token), "--ref", "a" * 40,
        "--target", "esp32", "--out", str(tmp_path / "run.json"), "--evidence-dir", str(tmp_path / "e"),
        "--poll-seconds", "0",
    ])
    assert code == 0
    printed = [line for line in capsys.readouterr().out.splitlines() if line.startswith("Queued:")]
    assert printed == [
        "Queued: no worker is connected",
        "Queued: waiting for 1 x esp32-c3: in use by run 1a2b3c4d",
    ]


def test_a_run_that_died_before_its_report_still_gets_a_page(tmp_path):
    """A build failure used to reach the consumer as "No report was produced;
    see the job log and farm-job.json" -- the compile error was in that JSON's
    log_tail, one artifact download away. The page carries the failed stage,
    the service's detail and the end of the log, error line included."""
    tail = "\n".join(
        ["$ pio run -e universal-sensor-prod", "Compiling .pio/build/x.o"]
        + [f"warning: something harmless {i}" for i in range(20)]
        + [
            "src/config_manager.h:206:10: error: 'void ConfigManager::setMeshCredentials' cannot be overloaded",
            "*** [.pio/build/universal-sensor-prod/src/common/command_package_handler.cpp.o] Error 1",
            "========================= [FAILED] Took 19.27 seconds =========================",
            "FAILED: Artifact build failed",
        ]
    )
    evidence = tmp_path / "hil-evidence"
    ci_farm_client.copy_evidence(_build_failed_job(tail), evidence)

    page = (evidence / "report.md").read_text(encoding="utf-8")
    assert page.startswith("# Farm run e62f548a: failed at Build artifacts")
    assert "Artifact build failed" in page
    assert "returned non-zero exit status 1" in page
    assert "error: 'void ConfigManager::setMeshCredentials'" in page
    assert page.rstrip().endswith("```"), "the log tail is fenced so the summary renders it verbatim"


def test_the_page_keeps_only_the_end_of_a_long_log(tmp_path):
    tail = "\n".join(f"line {i}" for i in range(1000))
    page = ci_farm_client.failure_report(_build_failed_job(tail))
    assert f"Last {ci_farm_client.LOG_TAIL_LINES} lines" in page
    assert "line 999" in page and "line 0\n" not in page


def test_the_farms_own_report_is_never_replaced(tmp_path):
    """A run that reached its report stage has a real report, even when the
    verdict is failed; the failure page only fills the gap when there is none."""
    real = tmp_path / "metrics" / "report.md"
    real.parent.mkdir()
    real.write_text("# HIL alteriom-firmware\n\n3 failed, 6 passed\n", encoding="utf-8")
    job = _build_failed_job("irrelevant")
    job["artifacts"]["report_markdown"] = {"available": True, "path": str(real)}
    job["result"]["report"] = str(real)

    evidence = tmp_path / "hil-evidence"
    ci_farm_client.copy_evidence(job, evidence)
    assert (evidence / "report.md").read_text(encoding="utf-8").startswith("# HIL alteriom-firmware")


def test_a_passed_run_has_no_failure_page():
    job = _build_failed_job("whatever")
    job["status"] = "passed"
    assert ci_farm_client.failure_report(job) is None


def test_the_branch_travels_beside_the_ref_when_given():
    """A consumer validates a commit, so --ref is a SHA; --branch is what the
    dashboard shows for it. Absent, nothing is sent: the service refuses an
    empty one, and callers that predate the field send none."""
    assert "branch" not in ci_farm_client.build_request(parse(BASE))
    body = ci_farm_client.build_request(parse(BASE + ["--branch", "feature/c3-console"]))
    assert body["branch"] == "feature/c3-console"
    assert body["ref"] == "abc123"


def test_suite_settings_travel_as_env():
    """Which family plays the gateway is the run's parameter, not the
    suite's constant: a dispatch hands it over as KEY=VALUE and the service
    exports it to the suite. Absent, nothing is sent."""
    assert "env" not in ci_farm_client.build_request(parse(BASE))
    body = ci_farm_client.build_request(
        parse(BASE + ["--suite-env", "ALTERIOM_HIL_GATEWAY_FAMILY=esp32-c6", "--suite-env", "ALTERIOM_HIL_UPLINK_TIMEOUT=180"])
    )
    assert body["env"] == {"ALTERIOM_HIL_GATEWAY_FAMILY": "esp32-c6", "ALTERIOM_HIL_UPLINK_TIMEOUT": "180"}


def test_a_setting_without_an_equals_sign_is_refused_before_submission():
    with pytest.raises(SystemExit, match="KEY=VALUE"):
        ci_farm_client.build_request(parse(BASE + ["--suite-env", "ALTERIOM_HIL_GATEWAY_FAMILY"]))


def test_a_blank_ref_is_left_to_the_profile():
    """hil-run.yml passes --ref only when its input was set, and the service
    fills a missing ref from the profile's default_ref. The first end-to-end
    dispatch of the generic trigger died with "the following arguments are
    required: --ref" because the client still demanded one."""
    without_ref = [arg for arg in BASE if arg not in ("--ref", "abc123")]
    body = ci_farm_client.build_request(parse(without_ref))
    assert "ref" not in body
    assert body["profile"] == "painlessmesh"


class _Response:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode()

    def read(self, *_):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _flaky_urlopen(failures: list[Exception], body: dict):
    """urlopen that raises each of `failures` in turn, then answers `body`."""
    calls = []

    def fake(request, timeout=None):
        calls.append(request.get_method())
        if failures:
            raise failures.pop(0)
        return _Response(body)

    return fake, calls


def _refused():
    from urllib.error import URLError

    return URLError(ConnectionRefusedError(111, "Connection refused"))


def test_a_poll_rides_out_the_service_restarting_under_it(monkeypatch):
    """A host deploy restarts the farm service right after a job finishes --
    while the client is polling for that job's final status. A few refused
    connections must not turn a passed run into a failed workflow."""
    fake, calls = _flaky_urlopen([_refused(), _refused()], {"status": "passed"})
    monkeypatch.setattr(ci_farm_client, "urlopen", fake)
    naps = []
    job = ci_farm_client.request_json("http://farm", "t", "/api/v1/jobs/1", sleep=naps.append, clock=lambda: 0.0)
    assert job == {"status": "passed"}
    assert calls == ["GET", "GET", "GET"]
    assert naps == [ci_farm_client.TRANSIENT_RETRY_INTERVAL, 2 * ci_farm_client.TRANSIENT_RETRY_INTERVAL]


def test_the_retry_gives_up_when_the_service_stays_down(monkeypatch):
    fake, calls = _flaky_urlopen([_refused()] * 5, {"status": "passed"})
    monkeypatch.setattr(ci_farm_client, "urlopen", fake)
    ticks = iter([0.0, 0.0, 200.0])  # deadline set; first retry allowed; then past it
    with pytest.raises(RuntimeError, match="unreachable"):
        ci_farm_client.request_json(
            "http://farm", "t", "/api/v1/jobs/1", retry_seconds=120, sleep=lambda _: None, clock=lambda: next(ticks)
        )
    assert calls == ["GET", "GET"]


def test_a_submission_is_repeated_only_when_it_never_arrived(monkeypatch):
    """A refused connection never reached the service, so resubmitting is
    safe. A timeout after sending may already have queued the job; a second
    copy would take the rig twice, so that one is an error."""
    from urllib.error import URLError

    fake, calls = _flaky_urlopen([_refused()], {"id": "j1", "status": "queued"})
    monkeypatch.setattr(ci_farm_client, "urlopen", fake)
    job = ci_farm_client.request_json(
        "http://farm", "t", "/api/v1/suites", {"profile": "p"}, sleep=lambda _: None, clock=lambda: 0.0
    )
    assert job["id"] == "j1" and calls == ["POST", "POST"]

    fake, calls = _flaky_urlopen([URLError(TimeoutError("timed out"))], {"id": "j2"})
    monkeypatch.setattr(ci_farm_client, "urlopen", fake)
    with pytest.raises(RuntimeError, match="unreachable"):
        ci_farm_client.request_json(
            "http://farm", "t", "/api/v1/suites", {"profile": "p"}, sleep=lambda _: None, clock=lambda: 0.0
        )
    assert calls == ["POST"]


# ---- a portal rolling out: the ingress answers for it ----------------------------------------------


def _ingress(code: int):
    import io
    from urllib.error import HTTPError

    reasons = {502: "Bad Gateway", 503: "Service Temporarily Unavailable", 504: "Gateway Timeout", 400: "Bad Request"}
    return HTTPError("https://portal/x", code, reasons.get(code, "Error"), {}, io.BytesIO(reasons.get(code, "").encode()))


def test_a_portal_rolling_out_is_waited_for_even_for_a_submission(monkeypatch, capsys):
    """Deploy run 34953173738 (#141): the canary submission met the ingress's
    503 while the portal's pod was recreated. The request never reached the
    portal, so it is repeated -- a POST too -- with growing pauses."""
    fake, calls = _flaky_urlopen([_ingress(503), _ingress(503)], {"id": "j1", "status": "queued"})
    monkeypatch.setattr(ci_farm_client, "urlopen", fake)
    naps = []
    job = ci_farm_client.request_json(
        "https://portal", "t", "/api/v1/suites", {"profile": "p"}, sleep=naps.append, clock=lambda: 0.0
    )
    assert job["id"] == "j1" and calls == ["POST", "POST", "POST"]
    assert naps == [5.0, 10.0]
    out = capsys.readouterr().out
    assert "portal unavailable (HTTP 503), retrying in 5s" in out and "portal unavailable (HTTP 503), retrying in 10s" in out

    for code in (502, 504):
        fake, calls = _flaky_urlopen([_ingress(code)], {"status": "passed"})
        monkeypatch.setattr(ci_farm_client, "urlopen", fake)
        assert ci_farm_client.request_json("https://portal", "t", "/api/v1/jobs/1", sleep=lambda _: None,
                                           clock=lambda: 0.0) == {"status": "passed"}
        assert calls == ["GET", "GET"], code


def test_a_portal_that_stays_down_gives_up_with_its_own_error_after_about_three_minutes(monkeypatch):
    fake, calls = _flaky_urlopen([_ingress(503) for _ in range(50)], {"status": "passed"})
    monkeypatch.setattr(ci_farm_client, "urlopen", fake)
    now = [0.0]
    naps = []

    def sleep(seconds):
        naps.append(seconds)
        now[0] += seconds

    with pytest.raises(RuntimeError, match="farm API returned HTTP 503: Service Temporarily Unavailable") as caught:
        ci_farm_client.request_json("https://portal", "t", "/api/v1/suites", {"profile": "p"},
                                    sleep=sleep, clock=lambda: now[0])
    assert sum(naps) == ci_farm_client.TRANSIENT_RETRY_SECONDS == 180.0
    assert naps[:5] == [5.0, 10.0, 20.0, 40.0, 60.0] and max(naps) == 60.0, "bounded exponential backoff"
    assert len(calls) == len(naps) + 1
    assert caught.value.__cause__ is not None


def test_a_status_the_portal_itself_answered_is_not_retried(monkeypatch):
    fake, calls = _flaky_urlopen([_ingress(400)], {"id": "j1"})
    monkeypatch.setattr(ci_farm_client, "urlopen", fake)
    naps = []
    with pytest.raises(RuntimeError, match="HTTP 400"):
        ci_farm_client.request_json("https://portal", "t", "/api/v1/suites", {"profile": "p"},
                                    sleep=naps.append, clock=lambda: 0.0)
    assert calls == ["POST"] and naps == []


def test_a_bundle_upload_rides_out_a_rollout_but_not_a_refusal(tmp_path, monkeypatch):
    fake, calls = _flaky_urlopen([_ingress(503), _refused()], {"id": "b" * 32})
    monkeypatch.setattr(ci_farm_client, "urlopen", fake)
    assert ci_farm_client.supply_bundle("https://portal", "t", _built_bundle(tmp_path), {},
                                        sleep=lambda _: None, clock=lambda: 0.0) == "b" * 32
    assert calls == ["POST", "POST", "POST"]

    from urllib.error import URLError

    # Dropped after sending: the farm may be storing it, so it is not sent twice.
    fake, calls = _flaky_urlopen([URLError(TimeoutError("timed out"))], {"id": "b" * 32})
    monkeypatch.setattr(ci_farm_client, "urlopen", fake)
    with pytest.raises(RuntimeError, match="unreachable"):
        ci_farm_client.supply_bundle("https://portal", "t", _built_bundle(tmp_path / "again"), {},
                                     sleep=lambda _: None, clock=lambda: 0.0)
    assert calls == ["POST"]


def test_the_release_client_waits_for_a_portal_rolling_out_too(monkeypatch, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location("ci_farm_release_retry", CLIENT_PATH.parent / "ci_farm_release.py")
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    fake, calls = _flaky_urlopen([_ingress(503), _ingress(502)], {"commit": "c" * 40, "bytes": 1, "sha256": "d" * 64})
    monkeypatch.setattr(release, "urlopen", fake)
    naps = []
    answer = release.call("https://portal", "t", "/api/v1/releases?commit=c", b"bundle", sleep=naps.append, clock=lambda: 0.0)
    assert answer["bytes"] == 1 and calls == ["POST", "POST", "POST"] and naps == [5.0, 10.0]
    assert "portal unavailable (HTTP 503), retrying in 5s" in capsys.readouterr().out

    fake, calls = _flaky_urlopen([_ingress(400)], {})
    monkeypatch.setattr(release, "urlopen", fake)
    with pytest.raises(RuntimeError, match="HTTP 400"):
        release.call("https://portal", "t", "/api/v1/releases", b"bundle", sleep=naps.append, clock=lambda: 0.0)
    assert calls == ["POST"]

    now = [0.0]
    fake, calls = _flaky_urlopen([_ingress(504) for _ in range(50)], {})
    monkeypatch.setattr(release, "urlopen", fake)
    with pytest.raises(RuntimeError, match="HTTP 504"):
        release.call("https://portal", "t", "/api/v1/releases", sleep=lambda s: now.__setitem__(0, now[0] + s),
                     clock=lambda: now[0])
    assert now[0] == release.RETRY_SECONDS


# ---------------------------------------------------------------------------
# Supplying a bundle this CI run built (docs/artifact-first-plan.md, step 2)
# ---------------------------------------------------------------------------


def _built_bundle(tmp_path):
    """An artifact directory as build_artifacts.py leaves it."""
    directory = tmp_path / "hil-firmware"
    (directory / "esp32").mkdir(parents=True)
    (directory / "manifest.json").write_text('{"schema": 2}', encoding="utf-8")
    (directory / "esp32" / "flash-image.bin").write_bytes(b"\xe9image")
    return directory


def test_an_archive_holds_the_bundle_under_one_directory(tmp_path):
    import io
    import tarfile

    payload = ci_farm_client.bundle_archive(_built_bundle(tmp_path))
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        names = sorted(member.name for member in archive.getmembers())
    assert names[0] == "hil-firmware"
    assert "hil-firmware/manifest.json" in names
    assert "hil-firmware/esp32/flash-image.bin" in names
    # The farm drops that one directory, so the bundle lands laid out as flashed.
    assert not any(name.startswith("/") or ".." in name for name in names)


def test_where_the_bundle_came_from_is_read_from_the_running_job(monkeypatch):
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "Alteriom/alteriom-esp32-farm")
    monkeypatch.setenv("GITHUB_RUN_ID", "34670860918")
    monkeypatch.setenv(
        "GITHUB_WORKFLOW_REF",
        "Alteriom/alteriom-esp32-farm/.github/workflows/hil-painlessmesh.yml@refs/heads/main",
    )
    fields = ci_farm_client.producer_fields(parse(BASE))
    assert fields == {
        "profile": "painlessmesh",
        "repo": "https://github.com/Alteriom/alteriom-esp32-farm",
        "workflow": ".github/workflows/hil-painlessmesh.yml",
        "run_id": "34670860918",
        "commit": "abc123",
        # Neither was given and no actor is in the environment: sent empty,
        # and the farm records none rather than a guess.
        "branch": "",
        "actor": "",
        "run_url": "https://github.com/Alteriom/alteriom-esp32-farm/actions/runs/34670860918",
    }


def test_a_supplied_bundle_is_posted_whole_and_its_id_goes_in_the_submission(tmp_path, monkeypatch):
    sent = {}

    def fake(request, timeout=None):
        sent["url"] = request.full_url
        sent["method"] = request.get_method()
        sent["body"] = request.data
        sent["auth"] = request.get_header("Authorization")
        return _Response({"id": "b" * 32, "revision": "abc123"})

    monkeypatch.setattr(ci_farm_client, "urlopen", fake)
    fields = {"profile": "painlessmesh", "repo": "https://github.com/o/r", "commit": "abc123"}
    bundle_id = ci_farm_client.supply_bundle(
        "http://127.0.0.1:8090", "token", _built_bundle(tmp_path), fields
    )

    assert bundle_id == "b" * 32
    assert sent["method"] == "POST"
    assert sent["url"].startswith("http://127.0.0.1:8090/api/v1/artifacts?")
    assert "commit=abc123" in sent["url"] and "profile=painlessmesh" in sent["url"]
    assert sent["auth"] == "Bearer token"
    assert sent["body"][:2] == b"\x1f\x8b", "gzip, the archive itself"

    # The submission then names it, so the farm flashes that bundle instead of
    # building on the rig.
    assert ci_farm_client.build_request(parse(BASE), bundle_id)["artifact"] == "b" * 32
    assert "artifact" not in ci_farm_client.build_request(parse(BASE))


def test_a_farm_that_refuses_a_bundle_says_why(tmp_path, monkeypatch):
    import io
    from urllib.error import HTTPError

    def refuse(request, timeout=None):
        raise HTTPError(
            request.full_url, 400, "Bad Request", {},
            io.BytesIO(b'{"error": "painlessmesh takes bundles from another workflow"}'),
        )

    monkeypatch.setattr(ci_farm_client, "urlopen", refuse)
    with pytest.raises(RuntimeError, match="takes bundles from another workflow"):
        ci_farm_client.supply_bundle("http://127.0.0.1:8090", "token", _built_bundle(tmp_path), {})


# ---- a run that covered less than its profile asks for ----


def test_a_pass_that_skipped_a_family_says_so_under_the_verdict():
    """The farm marks a family optional when its board is off the rig, and a
    run without it passes. "passed" is the only line most people read, so the
    families it did not exercise go directly under it."""
    passed = {
        "id": "j" * 32,
        "status": "passed",
        "result": {
            "summary": "Validated 5 boards; did not cover esp32-s3 (0 of 1)",
            "not_covered": [{"target": "esp32-s3", "wanted": 1, "got": 0, "optional": True}],
        },
    }
    note = ci_farm_client.coverage_note(passed)
    assert note is not None
    assert "esp32-s3 (0 of 1 boards)" in note
    assert "Families not covered" in note, "it points at the section in the report"
    # A run that covered everything says nothing about coverage at all.
    assert ci_farm_client.coverage_note({"status": "passed", "result": {"summary": "Validated 6 boards"}}) is None
    assert ci_farm_client.coverage_note({"status": "passed"}) is None
