"""A CallMeBot link set from the portal, sealed end to end (docs/providers.md).

The browser encrypts the link to the rig's public key with WebCrypto's
RSA-OAEP/SHA-256; the rig decrypts it with ``openssl pkeyutl`` and the private
key only it holds. These hold the pieces to each other: the key the rig
reports, the admin CLI that makes it and prints its fingerprint, and -- where
node and openssl are installed -- the page's own sealing code against the
exact openssl command rig/node-control.sh runs.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from alteriom_hil import providers

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "runner"
# The rig's own scripts, examples and schemas, which are beside its package
# now (docs/public-release-plan.md, step 12f).
RIG = ROOT / "rig"
APP = RUNNER.parent / "rig" / "web" / "app.js"
FAKE_LINK = "https://api.callmebot.com/whatsapp.php?phone=+15550001234&apikey=987654"
# The decrypt node-control.sh runs, argument for argument.
OPENSSL_DECRYPT = ["pkeyutl", "-decrypt", "-pkeyopt", "rsa_padding_mode:oaep",
                   "-pkeyopt", "rsa_oaep_md:sha256", "-pkeyopt", "rsa_mgf1_md:sha256"]

needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl is not installed")


def _openssl(*args: str, data: bytes | None = None) -> bytes:
    return subprocess.run(["openssl", *args], input=data, capture_output=True, check=True).stdout


@pytest.fixture(scope="module")
def rig_key(tmp_path_factory):
    if shutil.which("openssl") is None:
        pytest.skip("openssl is not installed")
    folder = tmp_path_factory.mktemp("seal")
    private = folder / "provider-seal.key"
    public = folder / "provider-seal.pub"
    # As install-health-service.sh makes it.
    _openssl("genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:3072", "-out", str(private))
    public.write_bytes(_openssl("pkey", "-in", str(private), "-pubout"))
    return private, public


# ---- the key the rig reports ----------------------------------------------------------------


def test_the_reported_seal_key_is_the_der_public_key_and_its_sha256(rig_key):
    private, public = rig_key
    der = _openssl("pkey", "-pubin", "-in", str(public), "-outform", "DER")
    info = providers.seal_key_info(public)
    assert base64.b64decode(info["spki"]) == der
    assert info["fingerprint"] == hashlib.sha256(der).hexdigest()
    assert providers.spki_rsa_bits(der) == 3072
    assert "PRIVATE" not in json.dumps(info)
    assert providers.grouped_fingerprint("0123456789abcdef")[:9] == "0123 4567"


def test_no_key_or_a_key_the_portal_cannot_seal_to_is_not_reported(tmp_path, monkeypatch):
    assert providers.seal_key_info(tmp_path / "missing.pub") is None
    (tmp_path / "junk.pub").write_text("-----BEGIN PUBLIC KEY-----\nnot base64!\n-----END PUBLIC KEY-----\n")
    assert providers.seal_key_info(tmp_path / "junk.pub") is None
    monkeypatch.setenv(providers.ENV_SEAL_PUBLIC_KEY, str(tmp_path / "missing.pub"))
    assert providers.seal_key_info() is None
    if shutil.which("openssl"):
        small = tmp_path / "small.key"
        _openssl("genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(small))
        (tmp_path / "small.pub").write_bytes(_openssl("pkey", "-in", str(small), "-pubout"))
        assert providers.seal_key_info(tmp_path / "small.pub") is None, "a sealed link is sized for RSA-3072"
        ec = tmp_path / "ec.key"
        _openssl("genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", str(ec))
        der = _openssl("pkey", "-in", str(ec), "-pubout", "-outform", "DER")
        with pytest.raises(providers.ProviderError, match="not an RSA key"):
            providers.spki_rsa_bits(der)


# ---- alteriom-hil-admin providers seal-key ---------------------------------------------------------


@needs_openssl
def test_seal_key_is_made_once_private_and_rotated_only_when_asked(tmp_path, monkeypatch, capsys):
    sys.path.insert(0, str(RUNNER))
    from alteriom_hil import admin_cli

    private, public = tmp_path / "etc" / "provider-seal.key", tmp_path / "etc" / "provider-seal.pub"
    monkeypatch.setattr(admin_cli.providers, "DEFAULT_SEAL_KEY", str(private))
    monkeypatch.setattr(admin_cli.providers, "DEFAULT_SEAL_PUBLIC_KEY", str(public))
    monkeypatch.setattr(admin_cli, "require_root", lambda: None)

    assert admin_cli.command_providers_seal_key(Namespace(rotate=False, fingerprint=False)) == 0
    printed = capsys.readouterr().out
    assert private.stat().st_mode & 0o777 == 0o600 and public.stat().st_mode & 0o777 == 0o644
    assert "PRIVATE" not in public.read_text() and "BEGIN PRIVATE KEY" in private.read_text()
    first = providers.seal_key_info(public)["fingerprint"]
    assert providers.grouped_fingerprint(first) in printed and "created" in printed

    assert admin_cli.command_providers_seal_key(Namespace(rotate=False, fingerprint=True)) == 0
    assert capsys.readouterr().out.strip() == first
    # Run again, it keeps the key: a link sealed for it still opens.
    assert admin_cli.command_providers_seal_key(Namespace(rotate=False, fingerprint=False)) == 0
    assert providers.seal_key_info(public)["fingerprint"] == first and "created" not in capsys.readouterr().out
    # A public key lost or edited is made again from the private key (as root).
    public.write_text("garbage")
    if os.geteuid() == 0:
        assert admin_cli.command_providers_seal_key(Namespace(rotate=False, fingerprint=False)) == 0
        assert "rewrote" in capsys.readouterr().out and providers.seal_key_info(public)["fingerprint"] == first
    assert admin_cli.command_providers_seal_key(Namespace(rotate=True, fingerprint=False)) == 0
    assert providers.seal_key_info(public)["fingerprint"] != first and "rotated" in capsys.readouterr().out
    assert not list(private.parent.glob(".*.new")), "no staged key is left behind"


def test_seal_key_is_a_providers_subcommand():
    sys.path.insert(0, str(RUNNER))
    from alteriom_hil import admin_cli

    args = admin_cli.parser().parse_args(["providers", "seal-key", "--fingerprint"])
    assert args.func is admin_cli.command_providers_seal_key and args.fingerprint and not args.rotate
    install = (RIG / "install-health-service.sh").read_text(encoding="utf-8")
    assert "openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072" in install
    assert "umask 077" in install and 'sudo chmod 0600 "$SEAL_KEY"' in install and 'sudo chmod 0644 "$SEAL_PUB"' in install
    assert 'if ! sudo test -f "$SEAL_KEY"' in install, "an existing key is never replaced by an install"


# ---- the page's sealing, against the rig's openssl -----------------------------------------------


def _function(script: str, name: str) -> str:
    return f"{name}{script.split(name, 1)[1].split(chr(10) + '}' + chr(10), 1)[0]}\n}}\n"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed; the page's WebCrypto is not run")
def test_the_pages_webcrypto_seal_opens_with_the_rigs_openssl_command(rig_key, tmp_path):
    """rig/web/app.js's own sealProviderLink, run on node's WebCrypto (the
    browser's API), decrypted by the command node-control.sh runs."""
    private, public = rig_key
    script = APP.read_text(encoding="utf-8")
    source = (
        "const CALLMEBOT_HOST = \"api.callmebot.com\";\n"
        + _function(script, "async function sealProviderLink(")
        + _function(script, "function callmebotLinkProblem(")
        + "const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));\n"
        + "(async () => {\n"
        + "  const sealed = await sealProviderLink(input.spki, input.link);\n"
        + "  const problems = input.cases.map(item => callmebotLinkProblem(item));\n"
        + "  process.stdout.write(JSON.stringify({...sealed, problems}));\n"
        + "})().catch(error => { console.error(error.name); process.exit(3); });\n"
    )
    runner = tmp_path / "seal.cjs"
    runner.write_text(source, encoding="utf-8")
    info = providers.seal_key_info(public)
    cases = [
        FAKE_LINK,
        "https://api.callmebot.com/whatsapp.php?phone=%2B15550001234&apikey=abc-DEF_1",
        FAKE_LINK + "&text=hi",
        FAKE_LINK.replace("https", "http"),
        FAKE_LINK.replace("api.callmebot.com", "api.callmebot.com.evil.example"),
        FAKE_LINK.replace("whatsapp.php", "signal.php"),
        FAKE_LINK + "&apikey=1234",
        FAKE_LINK + "&extra=1",
        "https://api.callmebot.com/whatsapp.php?phone=12&apikey=987654",
        "https://api.callmebot.com/whatsapp.php?phone=+15550001234",
        FAKE_LINK + "#x",
        "https://user:pw@api.callmebot.com/whatsapp.php?phone=+15550001234&apikey=987654",
        FAKE_LINK + " ",
        "not a url",
        "",
    ]
    result = subprocess.run(["node", str(runner)], input=json.dumps({"spki": info["spki"], "link": FAKE_LINK, "cases": cases}),
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    answer = json.loads(result.stdout)
    assert answer["fingerprint"] == info["fingerprint"], "the page computes the fingerprint of the key it seals to"
    sealed = base64.b64decode(answer["sealed"], validate=True)
    assert len(sealed) == providers.SEALED_BYTES == 384
    opened = _openssl(*OPENSSL_DECRYPT, "-inkey", str(private), data=sealed)
    assert opened.decode("utf-8") == FAKE_LINK
    # The page refuses what the rig would refuse, and says why without the link.
    for case, problem in zip(cases, answer["problems"]):
        try:
            providers.parse_callmebot_link(case)
            rig_accepts = True
        except providers.ProviderError:
            rig_accepts = False
        assert (problem is None) == rig_accepts, (case, problem)
        assert problem is None or "987654" not in problem
