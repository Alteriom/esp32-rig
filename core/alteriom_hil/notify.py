"""Telling a person the farm broke.

The farm notices plenty -- a canary red on every board pauses its own queue, a
board fails its checks, the host goes unhealthy -- and until this said so only
where someone was already looking. The failure mode of an unattended rig is
not a loud crash; it is silence mistaken for a slow queue.

So there is one outbound path: a webhook, configured on the host
(``notify:`` in /etc/alteriom-hil/config.yaml), carrying three events:

- ``queue_paused`` -- the farm paused its own queue (a canary red on every
  board). An operator pausing it is not news to that operator, and is not sent.
- ``board_red`` -- a canary run found boards failing checks the rest passed.
- ``host_unhealthy`` -- the health check went unhealthy, and again when it
  recovers, so a notification that something broke is never the last word.

The webhook URL is a credential -- Slack and Discord put the token in it -- so
it lives in a file of its own, root-owned and readable by the service's group
like the API token, and is read when a message is sent: rotating it needs no
restart. A delivery never blocks or fails the work that caused it: it has a
short timeout, is sent from a thread, and its outcome is kept for the
configuration page instead of raised.
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlparse

EVENTS = ("queue_paused", "board_red", "host_unhealthy")
FORMATS = ("slack", "discord", "json")
TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class ChannelKind:
    """A way of carrying a message, and what it needs to do it.

    Declared rather than coded around, because a farm grows channels: a
    webhook for a team's chat, a Telegram bot for a phone, and the providers a
    board sends through during a release run. A page can offer one, a
    configuration can validate one and a rig can say whether it has one from
    this alone.
    """

    kind: str
    title: str
    # The setting holding the credential, always a path to a root-owned file:
    # a token in a YAML file is a token in a backup and in a dashboard.
    secret_setting: str
    secret_name: str
    # Settings that are not secret, in the order a form should ask for them.
    settings: tuple[str, ...] = ()
    # What it can do. A channel that can validate is one a board can send
    # through during a run; today CallMeBot can and a webhook cannot.
    notifies: bool = True
    validates: bool = False
    # How to get one, for the page that asks.
    how: str = ""


CHANNELS: dict[str, ChannelKind] = {
    "webhook": ChannelKind(
        "webhook", "Webhook (Slack, Discord, or JSON)",
        secret_setting="webhook_url_file", secret_name="the webhook URL",
        settings=("format",),
        how="Slack: an incoming webhook for the channel. Discord: a channel webhook.",
    ),
    "telegram": ChannelKind(
        "telegram", "Telegram",
        secret_setting="token_file", secret_name="the bot token",
        settings=("chat_id",),
        # A board can send through it too: an https GET with the text in the
        # query, which is what `sendToInternet()` does.
        validates=True,
        how="Talk to @BotFather to make a bot and get its token, then send the bot a "
            "message and read the chat id from "
            "https://api.telegram.org/bot<token>/getUpdates.",
    ),
    "callmebot": ChannelKind(
        "callmebot", "CallMeBot (WhatsApp)",
        secret_setting="url_file", secret_name="the CallMeBot link",
        # The link carries the number and the key; there is nothing else to
        # ask for. It has always carried a run's proof, and it can carry a
        # notification with the same request.
        validates=True,
        how="Message the CallMeBot bot on WhatsApp for an API key, then keep the link "
            "it gives you: https://api.callmebot.com/whatsapp.php?phone=…&apikey=…",
    ),
}
TELEGRAM_API = "https://api.telegram.org"


@dataclass(frozen=True)
class Notification:
    event: str  # one of EVENTS, or "test"
    title: str
    detail: str = ""
    link: str | None = None
    # How it reads: "bad", "warn", or "good" for a recovery.
    tone: str = "bad"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def check_url(url: str) -> str:
    """A webhook URL the farm will post to: https, or http on loopback only."""
    parsed = urlparse(url.strip())
    loopback = parsed.hostname in ("127.0.0.1", "localhost", "::1")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ValueError("the webhook URL must be https (http only to loopback)")
    if not parsed.hostname:
        raise ValueError("the webhook URL has no host")
    return url.strip()


def _get(url: str) -> int:
    """A channel whose whole message is its URL: CallMeBot takes a `text=`."""
    request = urllib.request.Request(
        url, method="GET", headers={"User-Agent": "alteriom-hil-farm"},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return response.status


def _post(url: str, body: bytes) -> int:
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "alteriom-hil-farm"},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return response.status


class Notifier:
    def __init__(
        self,
        url_file: str | Path,
        fmt: str = "slack",
        events: tuple[str, ...] | list[str] = EVENTS,
        public_host: str | None = None,
        post: Callable[[str, bytes], int] = _post,
        get: Callable[[str], int] = None,
        channel: str = "webhook",
        chat_id: str | None = None,
    ):
        if fmt not in FORMATS:
            raise ValueError(f"format must be one of {', '.join(FORMATS)}")
        if channel not in CHANNELS:
            raise ValueError(f"channel must be one of {', '.join(CHANNELS)}")
        if channel == "telegram" and not chat_id:
            raise ValueError("a Telegram channel needs the chat id to send to")
        self.channel = channel
        self.chat_id = str(chat_id) if chat_id else None
        # The file holding this channel's credential: a webhook URL, a bot
        # token. Read when a message is sent, so rotating it needs no restart.
        self.url_file = Path(url_file)
        self.format = fmt
        self.events = frozenset(events)
        self.host = socket.gethostname()
        # The dashboard's public name, for links; none when it has none.
        self.public_host = public_host
        self._post = post
        self._get = get or _get
        self._lock = threading.Lock()
        self.last: dict | None = None
        self.id = "1"

    @classmethod
    def from_env(cls, env: Mapping[str, str], **kwargs) -> "Notifier | None":
        """The notifier the runtime environment describes, or None when the
        host has not turned notifications on (hil_config.runtime_env)."""
        channel = env.get("ALTERIOM_HIL_NOTIFY_CHANNEL") or "webhook"
        url_file = {
            "telegram": env.get("ALTERIOM_HIL_NOTIFY_TOKEN_FILE"),
            "callmebot": env.get("ALTERIOM_HIL_NOTIFY_URL_FILE"),
        }.get(channel, env.get("ALTERIOM_HIL_NOTIFY_WEBHOOK_FILE"))
        if not url_file:
            return None
        events = [item for item in (env.get("ALTERIOM_HIL_NOTIFY_EVENTS") or "").split(",") if item]
        return cls(
            url_file,
            fmt=env.get("ALTERIOM_HIL_NOTIFY_FORMAT") or "slack",
            events=events or EVENTS,
            public_host=env.get("ALTERIOM_HIL_PUBLIC_HOST") or None,
            channel=channel,
            chat_id=env.get("ALTERIOM_HIL_NOTIFY_CHAT_ID") or None,
            **kwargs,
        )

    @classmethod
    def all_from_env(cls, env, **kwargs) -> list["Notifier"]:
        """Every notifier the runtime environment describes.

        A host held one channel and exported it under its own variables; it
        can hold several now, and they arrive as one JSON variable
        (hil_config.runtime_env). The single-channel variables are still
        exported for the first of them, so this falls back to reading those --
        a service started before an upgrade, or a host whose configuration
        predates the list.
        """
        import json as _json

        said = env.get("ALTERIOM_HIL_NOTIFY_CHANNELS")
        if not said:
            one = cls.from_env(env, **kwargs)
            return [one] if one is not None else []
        try:
            described = _json.loads(said)
        except (TypeError, ValueError):
            return []
        built = []
        for channel in described if isinstance(described, list) else []:
            if not isinstance(channel, dict):
                continue
            kind = channel.get("channel") or "webhook"
            url_file = (channel.get("token_file") if kind == "telegram"
                        else channel.get("url_file") if kind == "callmebot"
                        else channel.get("webhook_file"))
            if not url_file:
                continue
            try:
                notifier = cls(
                    url_file, fmt=channel.get("format") or "slack",
                    events=channel.get("events") or EVENTS,
                    public_host=env.get("ALTERIOM_HIL_PUBLIC_HOST") or None,
                    channel=kind, chat_id=channel.get("chat_id") or None, **kwargs,
                )
            except ValueError:
                continue  # a channel the file describes badly sends nothing
            notifier.id = str(channel.get("id") or len(built) + 1)
            built.append(notifier)
        return built

    def wants(self, event: str) -> bool:
        return event == "test" or event in self.events

    def body(self, note: Notification) -> dict:
        mark = {"bad": "🔴", "warn": "🟠", "good": "🟢"}.get(note.tone, "")
        lines = [f"{mark} {note.title}".strip()]
        if note.detail:
            lines.append(note.detail)
        if self.channel == "telegram":
            if note.link:
                lines.append(note.link)
            # Plain text: a title can carry a board id with an underscore in
            # it, and Markdown would either mangle it or fail the send.
            return {"chat_id": self.chat_id, "text": "\n".join(lines)[:4096],
                    "disable_web_page_preview": True}
        if self.format == "slack":
            if note.link:
                lines.append(f"<{note.link}|Open in the farm dashboard>")
            return {"text": "\n".join(lines)}
        if self.format == "discord":
            if note.link:
                lines.append(note.link)
            return {"content": "\n".join(lines)[:2000]}
        return {
            "event": note.event, "title": note.title, "detail": note.detail,
            "link": note.link, "tone": note.tone, "host": self.host, "at": _now(),
        }

    def send(self, note: Notification) -> dict | None:
        """Deliver now. Returns the outcome, or None for an event not asked for.
        Never raises: the work that caused a notification does not fail with it."""
        if not self.wants(note.event):
            return None
        outcome = {"event": note.event, "title": note.title, "at": _now(), "ok": False,
                   "status": None, "error": None, "channel": self.channel}
        try:
            url = self.endpoint(note)
            outcome["status"] = (
                self._get(url) if self.channel == "callmebot"
                else self._post(url, json.dumps(self.body(note)).encode("utf-8"))
            )
            outcome["ok"] = 200 <= int(outcome["status"]) < 300
            if not outcome["ok"]:
                outcome["error"] = f"{self.where()} answered {outcome['status']}"
        except urllib.error.HTTPError as exc:
            outcome["status"] = exc.code
            outcome["error"] = self.redact(f"{self.where()} answered {exc.code}")
        except OSError as exc:  # unreadable file, refused, timed out
            outcome["error"] = self.redact(str(exc) or exc.__class__.__name__)
        except ValueError as exc:
            outcome["error"] = self.redact(str(exc))
        with self._lock:
            self.last = outcome
        return outcome

    def send_later(self, note: Notification) -> threading.Thread | None:
        """Deliver from a thread, for callers that must not wait on a network."""
        if not self.wants(note.event):
            return None
        thread = threading.Thread(target=self.send, args=(note,), daemon=True, name="notify")
        thread.start()
        return thread

    def secret(self) -> str:
        return self.url_file.read_text(encoding="utf-8").strip()

    def endpoint(self, note: "Notification | None" = None) -> str:
        """Where this channel's message goes. For Telegram and CallMeBot the
        credential is in the URL itself, which is why nothing built here is
        ever put in a message."""
        if self.channel == "telegram":
            token = self.secret()
            if not token:
                raise ValueError("the Telegram bot token file is empty")
            return f"{TELEGRAM_API}/bot{token}/sendMessage"
        if self.channel == "callmebot":
            from . import providers

            link = providers.load_callmebot_link(self.url_file)
            return providers.message_url(link, self.text(note) if note else "")
        return check_url(self.url_file.read_text(encoding="utf-8"))

    def text(self, note: "Notification") -> str:
        """The message as one piece of text, for a channel that carries no
        structure: CallMeBot takes a `text=` and nothing else."""
        mark = {"bad": "🔴", "warn": "🟠", "good": "🟢"}.get(note.tone, "")
        lines = [f"{mark} {note.title}".strip()]
        if note.detail:
            lines.append(note.detail)
        if note.link:
            lines.append(note.link)
        return "\n".join(lines)[:900]

    def where(self) -> str:
        return {"telegram": "Telegram", "callmebot": "CallMeBot"}.get(self.channel, "the webhook")

    def redact(self, message: str) -> str:
        """The token out of anything said about a failure.

        Telegram puts the bot token in the path, so a refused request, a
        timeout and a redirect all name it in their message -- and those
        messages are kept for the configuration page and sent to the portal.
        """
        try:
            token = self.secret()
        except OSError:
            return message
        if not token:
            return message
        message = message.replace(token, "***")
        if self.channel == "callmebot":
            from . import providers

            try:
                for value in providers.secret_values(providers.parse_callmebot_link(token)):
                    message = message.replace(value, "***")
            except Exception:  # an unusable link has nothing to scrub
                pass
        return message

    def link(self, fragment: str) -> str | None:
        """A link into the dashboard, when the host has a public name.

        The dashboard is at /app; the root is the public site, which is not
        where a note about your own run should land you."""
        return f"https://{self.public_host}/app{fragment}" if self.public_host else None
