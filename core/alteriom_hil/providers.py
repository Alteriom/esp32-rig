"""Real third-party services a rig validates, and keeping their secrets out
of everything a run leaves behind.

The painlessMesh suite emulates CallMeBot on the rig's own gateway probe, which
proves the library carries a service's reply intact. It cannot prove the real
service accepts what the library sends. A rig whose owner stores their own
CallMeBot link can prove that too -- but a CallMeBot link *is* a credential:
the ``apikey`` sends WhatsApp messages as its owner, and the ``phone`` is the
owner's number. So:

- The link lives in one file on the rig, root-owned, group the service's,
  mode 0640 (``alteriom-hil-admin providers set callmebot`` writes it from
  stdin). Only its path reaches the runtime environment.
- Every message a validation sends is counted against a per-rig daily budget
  (``consume_budget``), so a loop or a burst of release builds cannot get the
  owner's number rate-limited or paused.
- Every run's log and evidence is scrubbed of the link's values
  (``Redactor``) before anything leaves the rig: the job log as it is written,
  serial captures as they are dumped, and every text file of the run's
  evidence before it is packaged. CallMeBot's replies echo the recipient's
  number, and a timeout's diagnostic carries the last serial lines, so
  "the row never prints the URL" is not enough on its own.

Future providers (Telegram, ...) follow the same shape: a loader that refuses
an invalid secret without echoing it, ``secret_values`` for the redactor, and
a budget of their own.

Plain stdlib, importable by the runner, the suites and the admin CLI.
"""

from __future__ import annotations

import json
import os
import re
import socket
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping
from urllib.parse import quote, unquote, urlencode, urlsplit

CALLMEBOT_HOST = "api.callmebot.com"
CALLMEBOT_PATH = "/whatsapp.php"
CALLMEBOT_PORT = 443
DEFAULT_CALLMEBOT_URL_FILE = "/etc/alteriom-hil/providers/callmebot-url"
DEFAULT_BUDGET_NAME = "callmebot-budget.json"
DEFAULT_STATE_DIR = "/var/lib/alteriom-hil"
# never: the rig never sends a real message. release: only a run the dispatch
# marked as a release build (ALTERIOM_HIL_RUN_KIND=release). always: every run
# that reaches the row -- for bring-up, and budget-limited like the rest.
SEND_POLICIES = ("never", "release", "always")
DEFAULT_CALLMEBOT = {"url_file": DEFAULT_CALLMEBOT_URL_FILE, "send": "release", "max_per_day": 5}
MAX_PER_DAY_LIMIT = 50

TELEGRAM_HOST = "api.telegram.org"
DEFAULT_TELEGRAM_URL_FILE = "/etc/alteriom-hil/providers/telegram-link"
DEFAULT_TELEGRAM_BUDGET_NAME = "telegram-budget.json"
DEFAULT_TELEGRAM = {"url_file": DEFAULT_TELEGRAM_URL_FILE, "send": "release", "max_per_day": 5}

# The runtime environment names (runner/hil_config.py writes them).
ENV_URL_FILE = "ALTERIOM_HIL_CALLMEBOT_URL_FILE"
ENV_SEND = "ALTERIOM_HIL_CALLMEBOT_SEND"
ENV_MAX_PER_DAY = "ALTERIOM_HIL_CALLMEBOT_MAX_PER_DAY"
ENV_BUDGET_FILE = "ALTERIOM_HIL_CALLMEBOT_BUDGET_FILE"
# The one run setting a dispatch may hand the suite about providers.
ENV_RUN_KIND = "ALTERIOM_HIL_RUN_KIND"

_PHONE = re.compile(r"\+?[0-9]{6,20}\Z")
_APIKEY = re.compile(r"[A-Za-z0-9_-]{4,64}\Z")
# What stands between a phone number's digits when a service formats it for a
# person: "+1 555 123-4567", "(555) 123.4567".
_PHONE_SEPARATOR = r"[ ().-]{0,2}"

REDACTED = "***"


class ProviderError(ValueError):
    """A provider's secret is missing, unreadable or invalid.

    The message says what is wrong and never includes the secret: it is
    printed by the CLI, shown by the health check and becomes a skip reason in
    a run's evidence.
    """


# ---- CallMeBot links ---------------------------------------------------------------------------


@dataclass(frozen=True, repr=False)
class CallMeBotLink:
    """A CallMeBot WhatsApp link without its message: who to send to, and the
    key that allows it. ``repr`` is redacted, so a link that ends up in a
    traceback or an f-string does not carry its secrets with it."""

    phone: str
    apikey: str

    def __repr__(self) -> str:
        return f"CallMeBotLink({redacted(self)})"

    __str__ = __repr__


def parse_callmebot_link(text: str) -> CallMeBotLink:
    """The link the owner copied from CallMeBot, checked.

    https to exactly api.callmebot.com/whatsapp.php, a phone and an apikey and
    nothing else: the row appends the text itself, and a parameter this does
    not know is a value the redactor would not know to scrub.
    """
    if not isinstance(text, str):
        raise ProviderError("the CallMeBot link must be text")
    value = text.strip()
    if not value:
        raise ProviderError("the CallMeBot link is empty")
    if any(character.isspace() for character in value):
        raise ProviderError("the CallMeBot link must be one line with no spaces")
    try:
        split = urlsplit(value)
        port = split.port
    except ValueError:
        raise ProviderError("the CallMeBot link is not a URL") from None
    if split.scheme != "https":
        raise ProviderError("the CallMeBot link must use https")
    if split.username is not None or split.password is not None:
        raise ProviderError("the CallMeBot link must not carry a user name or password")
    if (split.hostname or "") != CALLMEBOT_HOST or port not in (None, CALLMEBOT_PORT):
        raise ProviderError(f"the CallMeBot link must point at {CALLMEBOT_HOST}")
    if split.path != CALLMEBOT_PATH:
        raise ProviderError(f"the CallMeBot link must be the WhatsApp API, {CALLMEBOT_PATH}")
    if split.fragment:
        raise ProviderError("the CallMeBot link must not have a #fragment")
    # Not parse_qsl: it reads "+" as a space, and CallMeBot's own examples
    # write the number as phone=+34123123123.
    pairs = []
    for part in split.query.split("&") if split.query else []:
        name, sep, raw = part.partition("=")
        if not sep or not name:
            raise ProviderError("the CallMeBot link's query string is malformed")
        pairs.append((unquote(name), unquote(raw)))
    names = [name for name, _ in pairs]
    params = dict(pairs)
    if "text" in params:
        raise ProviderError("the CallMeBot link must not include text=; the validation adds its own message")
    unknown = sorted(set(params) - {"phone", "apikey"})
    if unknown:
        # Named only when a name is plainly a word: a mangled paste could put
        # part of the key where a name should be.
        shown = [name for name in unknown if re.fullmatch(r"[a-z_]{1,20}", name)]
        listed = ", ".join(shown) if len(shown) == len(unknown) else f"{len(unknown)} unrecognised"
        raise ProviderError(f"the CallMeBot link has parameters this rig does not know: {listed}")
    for name in ("phone", "apikey"):
        if names.count(name) > 1:
            raise ProviderError(f"the CallMeBot link names {name} more than once")
    phone = params.get("phone", "")
    apikey = params.get("apikey", "")
    if not phone:
        raise ProviderError("the CallMeBot link has no phone")
    if not apikey:
        raise ProviderError("the CallMeBot link has no apikey")
    if not _PHONE.fullmatch(phone):
        raise ProviderError("the CallMeBot link's phone must be an international number: + and 6 to 20 digits")
    if not _APIKEY.fullmatch(apikey):
        raise ProviderError("the CallMeBot link's apikey must be 4 to 64 letters, digits, - or _")
    return CallMeBotLink(phone=phone, apikey=apikey)


def load_callmebot_link(path: str | os.PathLike) -> CallMeBotLink:
    """The link stored on the rig. Raises ProviderError, naming the file and
    the problem, never the contents."""
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ProviderError(f"{file} does not exist") from None
    except PermissionError:
        raise ProviderError(f"{file} is not readable by this user") from None
    except (OSError, UnicodeDecodeError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else exc.__class__.__name__
        raise ProviderError(f"cannot read {file}: {reason}") from None
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) > 1:
        raise ProviderError(f"{file} must hold exactly one line, the link")
    try:
        return parse_callmebot_link(lines[0] if lines else "")
    except ProviderError as exc:
        raise ProviderError(f"{file}: {exc}") from None


def file_mode_problem(path: str | os.PathLike) -> str | None:
    """Why the file holding a secret is too open, or None.

    Every account on the rig can read a world-readable file; the convention
    for /etc/alteriom-hil secrets is root, the service's group, 0640.
    """
    try:
        mode = Path(path).stat().st_mode
    except OSError:
        return None  # the loader names a missing or unreadable file
    if mode & (stat.S_IROTH | stat.S_IWOTH):
        return f"{path} is readable or writable by every user (mode {stat.S_IMODE(mode):04o}); make it 0640"
    return None


def check_link_file(path: str | os.PathLike) -> CallMeBotLink:
    """The link, only if its file is also private: what the CLI's `check` and
    the health check hold a rig to."""
    link = load_callmebot_link(path)
    problem = file_mode_problem(path)
    if problem:
        raise ProviderError(problem)
    return link


def redacted(link: CallMeBotLink) -> str:
    """The link as it may be printed: the key hidden, the number down to its
    last two digits -- enough for an owner to recognise which one is stored."""
    digits = re.sub(r"[^0-9]", "", link.phone)
    return f"https://{CALLMEBOT_HOST}{CALLMEBOT_PATH}?phone={REDACTED}{digits[-2:]}&apikey={REDACTED}"


@dataclass(frozen=True)
class TelegramLink:
    """A Telegram bot and the chat it may write to. Like a CallMeBot link: a
    credential that sends as its owner, so ``repr`` is redacted and the value
    itself lives in one file on the rig."""

    token: str
    chat_id: str

    def __repr__(self) -> str:  # pragma: no cover - a guard, not a behaviour
        return "TelegramLink(token='***', chat_id='***')"


_TELEGRAM_TOKEN = re.compile(r"[0-9]{5,16}:[A-Za-z0-9_-]{20,64}\Z")
_TELEGRAM_CHAT = re.compile(r"-?[0-9]{1,20}\Z")


def parse_telegram_link(text: str) -> TelegramLink:
    """What the owner stores: ``<token> <chat_id>``, or the sendMessage URL
    Telegram's own documentation shows. Checked without echoing it."""
    said = " ".join(str(text or "").split())
    if not said:
        raise ProviderError("the link is empty")
    if said.startswith("http"):
        parts = urlsplit(said)
        if parts.scheme != "https" or parts.hostname != TELEGRAM_HOST:
            raise ProviderError(f"a Telegram link is https to {TELEGRAM_HOST}")
        token = parts.path.split("/")[1][3:] if parts.path.startswith("/bot") else ""
        chat = dict(
            pair.split("=", 1) for pair in parts.query.split("&") if "=" in pair
        ).get("chat_id", "")
        said = f"{token} {unquote(chat)}"
    token, _, chat_id = said.partition(" ")
    token, chat_id = token.strip(), chat_id.strip()
    if not _TELEGRAM_TOKEN.fullmatch(token):
        raise ProviderError("the bot token looks like 123456789:AA… (from @BotFather)")
    if not _TELEGRAM_CHAT.fullmatch(chat_id):
        raise ProviderError("the chat id is a number, like 12345678 or -1001234567890")
    return TelegramLink(token=token, chat_id=chat_id)


def load_telegram_link(path: str | os.PathLike) -> TelegramLink:
    """The link stored on the rig, or ProviderError naming the file."""
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ProviderError(f"{file} does not exist") from None
    except PermissionError:
        raise ProviderError(f"{file} is not readable by this user") from None
    except (OSError, UnicodeDecodeError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else exc.__class__.__name__
        raise ProviderError(f"cannot read {file}: {reason}") from None
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) > 1:
        raise ProviderError(f"{file} must hold exactly one line: the token and the chat id")
    try:
        return parse_telegram_link(lines[0] if lines else "")
    except ProviderError as exc:
        raise ProviderError(f"{file}: {exc}") from None


def telegram_link_text(link: TelegramLink) -> str:
    """The one canonical line the rig stores."""
    return f"{link.token} {link.chat_id}"


def telegram_redacted(link: TelegramLink) -> str:
    """For a page and a log: the bot by its number, never its secret half."""
    return f"bot {link.token.split(':')[0]}:*** to chat {link.chat_id}"


def telegram_message_url(link: TelegramLink, text: str) -> str:
    """The request that sends ``text``: an https GET a board can make, which
    is what makes this the same kind of thing as a CallMeBot link."""
    query = urlencode([("chat_id", link.chat_id), ("text", text)], quote_via=quote, safe="")
    return f"https://{TELEGRAM_HOST}/bot{link.token}/sendMessage?{query}"


def telegram_secret_values(link: TelegramLink) -> list[str]:
    """Every spelling of the token a log could carry. The chat id is not a
    secret -- it is in the message a person receives -- but the token is, and
    so is the whole URL that contains it."""
    return [link.token, quote(link.token, safe=""), link.token.split(":", 1)[-1]]


def secret_values(link: CallMeBotLink) -> list[str]:
    """Every spelling of the link's secrets a log could carry: the key, and
    the number as given, as digits and URL-encoded."""
    digits = re.sub(r"[^0-9]", "", link.phone)
    values = [link.apikey, link.phone, digits, quote(link.phone, safe="")]
    if link.phone.startswith("+"):
        values += ["%2b" + digits, "+" + digits]
    values.append(quote(link.apikey, safe=""))
    seen: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return seen


def link_url(link: CallMeBotLink) -> str:
    """The link in one canonical spelling: what the rig stores."""
    query = urlencode([("phone", link.phone), ("apikey", link.apikey)], quote_via=quote)
    return f"https://{CALLMEBOT_HOST}{CALLMEBOT_PATH}?{query}"


def message_url(link: CallMeBotLink, text: str) -> str:
    """The request that sends ``text`` to the link's number.

    Every character of the text is percent-encoded as UTF-8 except
    ``A-Z a-z 0-9 _.-~``: a newline is ``%0A``, ``&`` ``%26``, ``#`` ``%23``,
    ``+`` ``%2B`` (a bare ``+`` would reach CallMeBot's PHP as a space) and an
    emoji its UTF-8 bytes.
    """
    query = urlencode([("phone", link.phone), ("apikey", link.apikey), ("text", text)], quote_via=quote, safe="")
    return f"https://{CALLMEBOT_HOST}{CALLMEBOT_PATH}?{query}"


# ---- the messages the farm sends ---------------------------------------------------------------
# What the owner reads on WhatsApp: which run, which rig, which way the message
# went, and a tag that matches it to the run's evidence. Built only from what
# the run already publishes -- a revision, board ids, the rig's name -- and
# passed through the redactor as well, so a secret cannot ride in on a field.

MESSAGE_LIMIT = 500
_FIELD_LIMIT = 64
ENV_WORKER_NAME = "ALTERIOM_HIL_WORKER_NAME"
# A run's directory is <state>/runs/<job id>/; the suite is told two paths in it.
_RUN_DIR_ENVS = (("ALTERIOM_HIL_LOG_DIR", 1), ("ALTERIOM_HIL_RUN_LOG", 2))
_JOB_ID = re.compile(r"[A-Za-z0-9_-]{6,64}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def _field(value, redactor: "Redactor | None" = None, limit: int = _FIELD_LIMIT) -> str:
    """One value on one line: control characters (a newline a field carried
    would forge a line) and runs of whitespace become a space, clipped."""
    text = " ".join(_CONTROL.sub(" ", str(value if value is not None else "")).split())
    if redactor is not None:
        text = redactor.scrub(text)
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _bounded(lines: list[str], limit: int = MESSAGE_LIMIT) -> str:
    """The lines joined, shortening the longest line but the last (the tag,
    which matches the message to its run) until the text fits ``limit``."""
    lines = [line for line in lines if line]
    if len(lines) == 1:
        return lines[0][:limit]
    while len("\n".join(lines)) > limit and len(lines) > 1:
        index = max(range(len(lines) - 1), key=lambda i: len(lines[i]))
        line = lines[index]
        if len(line) <= 16:
            lines.pop(index)
            continue
        excess = len("\n".join(lines)) - limit
        lines[index] = line[:max(15, len(line) - excess - 1)].rstrip() + "…"
    return "\n".join(lines)[:limit]


def _utc_stamp(now: datetime | None) -> str:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def worker_name(env: Mapping[str, str] | None = None) -> str:
    """The rig's name as its portal knows it, else this host's name."""
    values = os.environ if env is None else env
    return (values.get(ENV_WORKER_NAME) or "").strip() or socket.gethostname()


def farm_job_id(env: Mapping[str, str] | None = None) -> str:
    """The farm job running this suite, read from the run directory the farm
    points the suite's logs into; "" outside a farm run."""
    values = os.environ if env is None else env
    for name, depth in _RUN_DIR_ENVS:
        raw = values.get(name)
        if not raw:
            continue
        parts = Path(raw).parts
        if len(parts) > depth + 1 and parts[-depth - 2] == "runs" and _JOB_ID.fullmatch(parts[-depth - 1]):
            return parts[-depth - 1]
    return ""


def _board(board_id: str, family: str | None, redactor) -> str:
    shown = _field(board_id, redactor, 32) or "?"
    return f"{shown} ({_field(family, redactor, 24)})" if family else shown


def callmebot_suite_message(
    *,
    tag: str,
    revision: str = "",
    rig: str = "",
    sender: tuple[str, str | None] | None = None,
    gateway: tuple[str, str | None] | None = None,
    job: str = "",
    now: datetime | None = None,
    redactor: "Redactor | None" = None,
) -> str:
    """The message the hardware row sends through the mesh. ``sender`` and
    ``gateway`` are (board id, family)."""
    path = [_board(*sender, redactor)] if sender else []
    if gateway:
        path.append(f"bridge {_board(*gateway, redactor)}")
    path.append(CALLMEBOT_HOST)
    # A job id and a commit are shown by their first twelve characters, as
    # the portal and git do.
    job_shown = _field(str(job or "")[:12], redactor)
    lines = [
        "🧪 painlessMesh hardware test",
        "CallMeBot through the mesh ✅ sent",
        f"Library: painlessMesh {_field(str(revision or '')[:12], redactor) or 'unknown'}",
        f"Rig: {_field(rig, redactor) or 'unknown'}",
        f"Path: {' → '.join(path)}",
        f"Run: {job_shown + ' · ' if job_shown else ''}{_utc_stamp(now)}",
        f"Tag: {_field(tag, redactor, 80)}",
    ]
    return _bounded(lines)


def callmebot_test_message(
    *,
    rig: str,
    policy: str,
    used: int,
    max_per_day: int,
    now: datetime | None = None,
    redactor: "Redactor | None" = None,
) -> str:
    """The message ``alteriom-hil-admin providers test callmebot`` sends from
    the rig host itself."""
    lines = [
        "🔔 Alteriom ESP32 farm",
        f"CallMeBot test from rig {_field(rig, redactor) or 'unknown'}",
        "Sent directly by the rig host (not through the mesh)",
        f"Policy: send={_field(policy, redactor, 16)}, budget {int(used)}/{int(max_per_day)} today",
        _utc_stamp(now),
    ]
    return _bounded(lines)


# ---- the rig's seal key ------------------------------------------------------------------------
# An owner can set the link from the portal's page for the rig without the
# portal ever holding it: the browser encrypts the link to this rig's public
# key (RSA-OAEP, SHA-256), the portal relays the ciphertext, and only the rig,
# holding the private key, decrypts it -- straight into `providers set`
# (rig/node-control.sh). The portal relays the public key too, so the
# fingerprint an owner compares on the rig (`providers seal-key`) is what
# stops a portal that swapped in a key of its own.

DEFAULT_SEAL_KEY = "/etc/alteriom-hil/provider-seal.key"
DEFAULT_SEAL_PUBLIC_KEY = "/etc/alteriom-hil/provider-seal.pub"
# Where a test (or an unusual host) points the reported public key instead.
ENV_SEAL_PUBLIC_KEY = "ALTERIOM_HIL_PROVIDER_SEAL_PUB"
SEAL_KEY_BITS = 3072
# RSA-OAEP output is exactly the modulus' length.
SEALED_BYTES = SEAL_KEY_BITS // 8
_RSA_ENCRYPTION_OID = bytes.fromhex("2a864886f70d010101")


def _der_item(data: bytes, offset: int) -> tuple[int, int, int]:
    """(tag, start of contents, end of contents) of the DER item at offset."""
    if offset + 2 > len(data):
        raise ProviderError("the seal key is truncated")
    tag = data[offset]
    length = data[offset + 1]
    start = offset + 2
    if length & 0x80:
        count = length & 0x7F
        if count == 0 or count > 4 or start + count > len(data):
            raise ProviderError("the seal key has a malformed length")
        length = int.from_bytes(data[start:start + count], "big")
        start += count
    end = start + length
    if end > len(data):
        raise ProviderError("the seal key is truncated")
    return tag, start, end


def spki_rsa_bits(der: bytes) -> int:
    """The modulus size of an RSA SubjectPublicKeyInfo; ProviderError when it
    is not one."""
    tag, start, end = _der_item(der, 0)
    if tag != 0x30 or end != len(der):
        raise ProviderError("the seal key is not a SubjectPublicKeyInfo")
    tag, algorithm_start, algorithm_end = _der_item(der, start)
    if tag != 0x30:
        raise ProviderError("the seal key is not a SubjectPublicKeyInfo")
    tag, oid_start, oid_end = _der_item(der, algorithm_start)
    if tag != 0x06 or der[oid_start:oid_end] != _RSA_ENCRYPTION_OID:
        raise ProviderError("the seal key is not an RSA key")
    tag, bits_start, bits_end = _der_item(der, algorithm_end)
    if tag != 0x03 or bits_start >= bits_end or der[bits_start] != 0:
        raise ProviderError("the seal key has no public key")
    tag, key_start, _ = _der_item(der, bits_start + 1)
    if tag != 0x30:
        raise ProviderError("the seal key has no RSA public key")
    tag, modulus_start, modulus_end = _der_item(der, key_start)
    if tag != 0x02:
        raise ProviderError("the seal key has no modulus")
    return int.from_bytes(der[modulus_start:modulus_end], "big").bit_length()


def pem_to_der(text: str) -> bytes:
    """The DER inside one PEM PUBLIC KEY block."""
    import base64
    import binascii

    begin, end = "-----BEGIN PUBLIC KEY-----", "-----END PUBLIC KEY-----"
    if begin not in text or end not in text:
        raise ProviderError("the seal key is not a PEM public key")
    body = text.split(begin, 1)[1].split(end, 1)[0]
    try:
        return base64.b64decode("".join(body.split()), validate=True)
    except (binascii.Error, ValueError):
        raise ProviderError("the seal key's PEM is not base64") from None


def fingerprint(der: bytes) -> str:
    """sha256 over the DER SubjectPublicKeyInfo, lowercase hex: what the
    portal's page and `providers seal-key` both show."""
    import hashlib

    return hashlib.sha256(der).hexdigest()


def grouped_fingerprint(value: str) -> str:
    """A fingerprint as a person compares it: groups of four."""
    return " ".join(value[index:index + 4] for index in range(0, len(value), 4))


def seal_key_info(path: str | os.PathLike | None = None) -> dict | None:
    """``{"spki": base64 DER, "fingerprint": hex}`` of the rig's public seal
    key, for the configuration a node reports; None when there is none, or it
    is not an RSA-3072 key (the portal sizes a sealed link to that)."""
    import base64

    file = Path(path or os.environ.get(ENV_SEAL_PUBLIC_KEY) or DEFAULT_SEAL_PUBLIC_KEY)
    try:
        der = pem_to_der(file.read_text(encoding="ascii"))
        if spki_rsa_bits(der) != SEAL_KEY_BITS:
            return None
    except (OSError, UnicodeDecodeError, ProviderError):
        return None
    return {"spki": base64.b64encode(der).decode("ascii"), "fingerprint": fingerprint(der)}


# ---- what a CallMeBot reply means --------------------------------------------------------------
# Mirrors painlessMesh's examples/sendToInternet/callmebot.h (`callmebot::judge`),
# which is what a user's sketch reads a reply with. Kept in step with it by
# hand: if CallMeBot's wording changes there, it changes here.

HTTP_SUCCESS = (200, 201, 202, 204)


def judge_callmebot_reply(http_status: int, response: str) -> str:
    """One of queued, rate_limited, account_paused, invalid_api_key,
    not_delivered, no_reply, unrecognized -- the verdicts of callmebot.h, in its
    order."""
    status = int(http_status or 0)
    body = str(response or "")
    if status == 0:
        return "no_reply"
    # Refusals first: they arrive under success statuses too.
    if "Too many requests" in body:
        return "rate_limited"
    if "Account is Paused" in body or "send the word 'resume'" in body:
        return "account_paused"
    # The real reply to a key CallMeBot does not know (HTTP 203, seen from this
    # rig on 2026-09-15).
    if "APIKey is invalid" in body:
        return "invalid_api_key"
    # A 208 carrying "Message queued" still did not arrive in the field.
    if status == 208:
        return "not_delivered"
    if status in HTTP_SUCCESS and "Message queued" in body:
        return "queued"
    return "unrecognized"


# What the gateway's error says when the request never left the rig's own
# network: DNS, no route, nothing listening, or no gateway with an uplink. A
# rig whose AP has no Internet is a rig condition, not a library verdict.
_UNREACHABLE = (
    "connection refused",
    "dns",
    "resolve",
    "no route",
    "unreachable",
    "no internet",
    "captive portal",
    "no active mesh",
    "no gateway",
)
# What painlessMesh (#464) appends to a transport error raised after the
# request was written -- not connected, connection lost, a reply that was not
# HTTP, a read timeout. The service may have the message: that is not a rig
# without a route, and skipping it would hide a possible delivery.
_MAY_HAVE_ARRIVED = "may have reached the server"


def upstream_unreachable(http_status: int, error: str) -> bool:
    """True when a gateway result says the service was never reached."""
    if int(http_status or 0) != 0:
        return False
    text = str(error or "").lower()
    if _MAY_HAVE_ARRIVED in text:
        return False
    return any(marker in text for marker in _UNREACHABLE)


# ---- the route to the service -----------------------------------------------------------------


def route_problem(
    host: str = CALLMEBOT_HOST,
    port: int = CALLMEBOT_PORT,
    timeout: float = 3.0,
    resolve: Callable = socket.getaddrinfo,
    connect: Callable = socket.create_connection,
) -> str | None:
    """Why this host cannot open a TCP connection to the service, or None.

    Resolves and connects; sends nothing -- not even a TLS hello -- so a check
    can never become a message. The rig's gateway boards reach the service
    through the host's AP, so a host that cannot is a rig that cannot.
    """
    try:
        resolve(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        return f"{host} does not resolve: {exc}"
    try:
        with connect((host, port), timeout=timeout):
            pass
    except OSError as exc:
        return f"{host}:{port} did not accept a TCP connection within {timeout:g}s: {exc}"
    return None


# ---- the daily budget --------------------------------------------------------------------------


def budget_file_for(env: Mapping[str, str]) -> Path:
    """The budget the runtime environment names, or the default under the
    farm's state directory."""
    named = env.get(ENV_BUDGET_FILE)
    if named:
        return Path(named)
    return Path(env.get("ALTERIOM_HIL_STATE") or DEFAULT_STATE_DIR) / DEFAULT_BUDGET_NAME


def _today(now: datetime | None) -> str:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).date().isoformat()


def _read_budget(text: str, today: str) -> int:
    try:
        record = json.loads(text) if text.strip() else {}
    except ValueError:
        record = {}
    if not isinstance(record, dict) or record.get("date") != today:
        return 0
    used = record.get("used")
    return used if isinstance(used, int) and not isinstance(used, bool) and used >= 0 else 0


def consume_budget(budget_file: str | os.PathLike, max_per_day: int, now: datetime | None = None) -> bool:
    """Take one message from today's budget (UTC). False when it is spent.

    Locked, so two runs on one rig -- or a run and an operator's check --
    cannot both take the last message. A file from another day, or one that
    does not parse, counts as nothing used.
    """
    import fcntl  # POSIX only; the rig is Linux

    path = Path(budget_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    today = _today(now)
    with open(path, "a+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            stream.seek(0)
            used = _read_budget(stream.read(), today)
            if used >= int(max_per_day):
                return False
            stream.seek(0)
            stream.truncate()
            stream.write(json.dumps({"date": today, "used": used + 1}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            return True
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def budget_used(budget_file: str | os.PathLike, now: datetime | None = None) -> int:
    """How many messages today's budget has spent (0 when there is no file)."""
    try:
        text = Path(budget_file).read_text(encoding="utf-8")
    except OSError:
        return 0
    return _read_budget(text, _today(now))


# ---- scrubbing ---------------------------------------------------------------------------------

# The text a run's evidence is made of. Firmware images and archives are not
# text and carry nothing a run printed.
TEXT_SUFFIXES = (".log", ".txt", ".json", ".jsonl", ".xml", ".md", ".html", ".csv", ".yaml", ".yml")


def _digit_bounded(pattern: str, value: str) -> str:
    """``pattern`` that cannot match inside a longer run of digits."""
    if value[:1].isdigit():
        pattern = r"(?<![0-9])" + pattern
    if value[-1:].isdigit():
        pattern = pattern + r"(?![0-9])"
    return pattern


class Redactor:
    """Replaces every configured secret with ``***``.

    Substring replacement, so a key inside a double-encoded URL
    (``apikey%3D123456``) is still found. A secret that begins or ends in a
    digit must not have another digit beside it, though: CallMeBot keys are
    often all digits, and scrubbing "123456" out of a duration such as
    12.9123456 would corrupt report.json and results.xml, which the portal
    and CI parse. A number that is exactly the key is still scrubbed.
    """

    def __init__(self, secrets: Iterable[str] = (), phones: Iterable[str] = ()):
        # Longest first, so a phone's URL-encoded form is replaced whole
        # rather than leaving "%2B" beside a scrubbed number.
        literal = sorted({value for value in secrets if value and len(value) >= 4}, key=len, reverse=True)
        patterns = [_digit_bounded(re.escape(value), value) for value in literal]
        for phone in phones:
            digits = re.sub(r"[^0-9]", "", phone or "")
            if len(digits) >= 6:
                # The number as a person would write it, spaced or dashed.
                patterns.append(r"(?<![0-9])\+?" + _PHONE_SEPARATOR.join(digits) + r"(?![0-9])")
        self._pattern = re.compile("|".join(patterns)) if patterns else None

    @classmethod
    def for_callmebot(cls, link: CallMeBotLink) -> "Redactor":
        return cls(secret_values(link), phones=[link.phone])

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Redactor":
        """Built from every provider the runtime environment configures.

        Silently empty when there is none, or its file cannot be read here:
        a process that cannot read the secret cannot have printed it.
        """
        values = os.environ if env is None else env
        secrets: list[str] = []
        phones: list[str] = []
        url_file = values.get(ENV_URL_FILE)
        if url_file:
            try:
                link = load_callmebot_link(url_file)
            except ProviderError:
                link = None
            if link is not None:
                secrets += secret_values(link)
                phones.append(link.phone)
        return cls(secrets, phones=phones)

    @property
    def active(self) -> bool:
        return self._pattern is not None

    def scrub(self, text: str) -> str:
        if self._pattern is None or not text:
            return text
        return self._pattern.sub(REDACTED, text)


def scrub_file(path: str | os.PathLike, redactor: Redactor) -> bool:
    """Scrub one text file in place, line by line so size does not matter.
    True when something was replaced; the file is rewritten only then, keeping
    its mode."""
    source = Path(path)
    if not redactor.active or source.is_symlink() or not source.is_file():
        return False
    changed = False
    handle, temporary_name = tempfile.mkstemp(dir=source.parent, prefix=f".{source.name}.", suffix=".scrub")
    temporary = Path(temporary_name)
    try:
        with open(source, "r", encoding="utf-8", errors="surrogateescape", newline="") as reader, \
                os.fdopen(handle, "w", encoding="utf-8", errors="surrogateescape", newline="") as writer:
            for line in reader:
                cleaned = redactor.scrub(line)
                changed = changed or cleaned != line
                writer.write(cleaned)
        if changed:
            os.chmod(temporary, stat.S_IMODE(source.stat().st_mode))
            temporary.replace(source)
    finally:
        if temporary.exists():
            temporary.unlink()
    return changed


def scrub_tree(root: str | os.PathLike, redactor: Redactor, suffixes: Iterable[str] = TEXT_SUFFIXES) -> list[Path]:
    """Scrub every text file under ``root``; the files that changed."""
    base = Path(root)
    if not redactor.active or not base.is_dir():
        return []
    wanted = tuple(suffix.lower() for suffix in suffixes)
    changed = []
    for path in sorted(base.rglob("*")):
        if path.is_symlink() or not path.is_file() or not path.name.lower().endswith(wanted):
            continue
        try:
            if scrub_file(path, redactor):
                changed.append(path)
        except OSError:
            continue
    return changed
