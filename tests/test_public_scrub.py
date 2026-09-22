"""What goes public says nothing about this farm in particular.

`core/` and `rig/` are the two distributions that become the public rig
repository (docs/public-release-plan.md, step 15). A rig is a thing somebody
builds for their own project; the code that ships to them must not carry the
names of our hosts, our people, our portal or our private consumer.

This is the other half of `test_rig_package.py`. That one holds the boundary
between the halves; this one holds what may cross it. Both are ratchets: the
counts may shrink and may not grow, so a name that arrives arrives on purpose
and a name that leaves cannot come back.

What is checked is the *shipped* tree -- the source, the scripts, the
dashboard bundle, the examples -- not the tests, the docs or `runner/`, which
stay here.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = (ROOT / "core", ROOT / "rig")
# What a local install or a build left behind is not code anybody wrote.
BUILT = {"build", "__pycache__", ".venv", "node_modules"}
SHIPPED = {".py", ".js", ".sh", ".css", ".html", ".json", ".yaml", ".yml", ".conf", ".rules", ".toml"}

# This farm's own names, by the shape they take. A rig owner reading the
# public repository should find none of them.
#
#   the hosts      the Raspberry Pis and the machine beside them
#   the people     whoever runs this farm
#   the portal     the farm we happen to run, at the address we happen to use
#   the consumer   a private project the farm tests for
OURS = {
    "a host of ours": re.compile(r"\b(?:esp32-hil|rig-?02|alteriom03)\b", re.I),
    "a person of ours": re.compile(r"\bsparck\b", re.I),
    "our portal": re.compile(r"\bespfarm\b|\balteriom\.net\b", re.I),
    "our private consumer": re.compile(r"\balteriom-firmware\b", re.I),
}

# What is still there, why, and how much of it. Each of these is a decision
# rather than an oversight; anything else fails.
#
#   harden-pi.sh   the sshd drop-in is named for the project, not the host:
#                  /etc/ssh/sshd_config.d/00-esp32-hil-hardening.conf. Renaming
#                  it on a rig that has the old one would leave both.
#   the consumer   named in four comments that explain why a shape exists --
#                  a one-board profile, a private repository, a core-3 build.
#                  Each is rewritten about a consumer in general before the
#                  lift; the plan says so and this count is the work left.
KNOWN = {
    ("rig/harden-pi.sh", "a host of ours"): 1,
    ("core/alteriom_hil/service.py", "our private consumer"): 1,
    ("rig/alteriom_hil/report.py", "our private consumer"): 1,
    ("rig/alteriom_hil/rig_manager.py", "our private consumer"): 2,
}


def shipped_files() -> list[Path]:
    found = []
    for top in PUBLIC:
        for path in sorted(top.rglob("*")):
            if not path.is_file() or path.suffix not in SHIPPED:
                continue
            if BUILT & set(path.parts) or any(part.endswith("egg-info") for part in path.parts):
                continue
            found.append(path)
    return found


def ours_by_file() -> dict:
    found: dict = {}
    for path in shipped_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for what, pattern in OURS.items():
            count = sum(1 for line in text.splitlines() if pattern.search(line))
            if count:
                found[(path.relative_to(ROOT).as_posix(), what)] = count
    return found


def test_the_public_half_names_this_farm_only_where_it_is_known_to():
    """A ratchet, as the boundary's are: the counts shrink and cannot grow."""
    found = ours_by_file()
    grown = {key: (KNOWN.get(key, 0), count) for key, count in found.items()
             if count > KNOWN.get(key, 0)}
    assert not grown, (
        f"the public half names this farm more than it did (was, is): {grown}. "
        "What ships to somebody else's rig is about a rig in general: write the "
        "evidence without the host, the person or the address."
    )
    shrunk = {key: (count, found.get(key, 0)) for key, count in KNOWN.items()
              if found.get(key, 0) < count}
    assert not shrunk, (
        f"the public half names this farm less than the table says (was, is): {shrunk}. "
        "Lower the count in KNOWN, or remove the line at zero."
    )


def test_no_address_of_ours_is_shipped():
    """An address a rig owner cannot reach and should not know.

    The rig's own network is not one of these: 10.42.0.1 is the access point a
    rig raises for the boards, 192.168.1.0/24 is an example LAN in a comment,
    and 127.0.0.1 and 0.0.0.0 are what a service binds. Anything else routable
    is ours.
    """
    allowed = re.compile(r"^(?:127\.|0\.0\.0\.0|255\.|10\.42\.|192\.168\.|169\.254\.|8\.8\.8\.8|224\.)")
    # Every octet a real one, so that `7.59.4.07` -- a point on an SVG path,
    # and the dashboard is full of them -- is not read as an address.
    address = re.compile(r"\b(?:(?:0|[1-9]\d{0,2})\.){3}(?:0|[1-9]\d{0,2})\b")
    # And a marked-up geometry is not an address however it is punctuated.
    geometry = re.compile(r"""<(?:svg|path|polygon|polyline|circle|rect)\b|viewBox=|\bd="|points=""")
    offenders: dict = {}
    for path in shipped_files():
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if geometry.search(line):
                continue
            for match in address.findall(line):
                if allowed.match(match) or any(int(part) > 255 for part in match.split(".")):
                    continue
                offenders.setdefault(path.relative_to(ROOT).as_posix(), []).append(f"{number}: {match}")
    assert not offenders, (
        f"the public half ships an address of ours: {offenders}. A rig owner's "
        "network is not ours; say what the address is for, not what it is."
    )


def test_the_example_configuration_is_somebody_elses():
    """It is the file a rig owner copies, so every path in it is a placeholder.

    It carried this farm's user, its clone, its runner unit and its backup
    host, which is exactly what somebody would paste into their own rig and
    then wonder about.
    """
    example = (ROOT / "rig" / "hil-config.example.yaml").read_text(encoding="utf-8")
    for ours in ("/home/sparck", "sparck@", "espfarm.alteriom.net", "esp32-hil",
                 "alteriom03", "alteriom-esp32-farm"):
        assert ours not in example, f"the example configuration still says {ours!r}"
    # And what replaced them is a value, not a shape: the file is validated
    # against the schema and by the service, and a rig owner copies it.
    assert "/home/rig/" in example and "example.org" in example
