"""Notification channels: console, JSONL log, email, ntfy push, webhooks."""

from __future__ import annotations

import html
import json
import logging
import smtplib
import ssl
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path

from .detect import money
from .models import Alert
from .storage import utcnow

log = logging.getLogger(__name__)

RESET = "\033[0m"
BOLD = "\033[1m"
SEVERITY_COLOR = {0: "\033[36m", 1: "\033[33m", 2: "\033[32m"}
SEVERITY_TAG = {0: "INFO", 1: "DEAL", 2: "HOT "}


class Notifier:
    name = "notifier"

    def send(self, alerts: list[Alert]) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class ConsoleNotifier(Notifier):
    name = "console"

    def __init__(self, color: bool = True) -> None:
        self.color = color

    def send(self, alerts: list[Alert]) -> None:
        for alert in alerts:
            tag = SEVERITY_TAG.get(alert.severity, "DEAL")
            if self.color:
                colour = SEVERITY_COLOR.get(alert.severity, "")
                head = f"{colour}{BOLD}[{tag}]{RESET} {BOLD}{alert.product_name}{RESET}"
            else:
                head = f"[{tag}] {alert.product_name}"
            print(head)
            print(f"       {alert.message}")
            if alert.stores:
                print(f"       Pickup: {', '.join(alert.stores[:5])}")
            if alert.url:
                print(f"       {alert.url}")
            print()


class FileNotifier(Notifier):
    """Append-only JSONL audit log -- handy for graphing or piping elsewhere."""

    name = "file"

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        if self.path.parent != Path(""):
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def send(self, alerts: list[Alert]) -> None:
        with open(self.path, "a", encoding="utf-8") as fh:
            for alert in alerts:
                record = alert.as_dict()
                record["ts"] = utcnow()
                fh.write(json.dumps(record) + "\n")


class EmailNotifier(Notifier):
    """One digest email per scan. Works with Gmail app passwords out of the box."""

    name = "email"

    def __init__(self, settings: dict) -> None:
        self.host = settings.get("smtp_host", "smtp.gmail.com")
        self.port = int(settings.get("smtp_port", 587))
        self.use_ssl = bool(settings.get("use_ssl", False))
        self.username = settings.get("username", "")
        self.password = settings.get("password", "")
        self.from_addr = settings.get("from_addr") or self.username
        self.to_addrs = list(settings.get("to_addrs") or [])

    def send(self, alerts: list[Alert]) -> None:
        if not self.to_addrs:
            log.warning("email notifier has no recipients; skipping")
            return

        msg = EmailMessage()
        msg["Subject"] = self._subject(alerts)
        msg["From"] = self.from_addr
        msg["To"] = ", ".join(self.to_addrs)
        msg.set_content(_plaintext(alerts))
        msg.add_alternative(_html_body(alerts), subtype="html")

        context = ssl.create_default_context()
        if self.use_ssl:
            with smtplib.SMTP_SSL(self.host, self.port, context=context, timeout=30) as smtp:
                smtp.login(self.username, self.password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.login(self.username, self.password)
                smtp.send_message(msg)

    @staticmethod
    def _subject(alerts: list[Alert]) -> str:
        hot = [a for a in alerts if a.severity >= 2]
        lead = (hot or alerts)[0]
        prefix = "Zephyrus deal" if len(alerts) == 1 else f"Zephyrus: {len(alerts)} alerts"
        price = money(lead.price) if lead.price is not None else ""
        return f"{prefix} -- {lead.product_name[:60]} {price}".strip()


class NtfyNotifier(Notifier):
    """Push straight to a phone via ntfy.sh -- no account, no app registration."""

    name = "ntfy"

    def __init__(self, settings: dict) -> None:
        self.server = str(settings.get("server", "https://ntfy.sh")).rstrip("/")
        self.topic = settings.get("topic", "")
        self.token = settings.get("token", "")

    def send(self, alerts: list[Alert]) -> None:
        if not self.topic:
            log.warning("ntfy notifier has no topic; skipping")
            return
        for alert in alerts:
            headers = {
                "Title": f"{alert.product_name[:70]}"[:200],
                "Priority": "high" if alert.severity >= 2 else "default",
                "Tags": "money_with_wings" if alert.severity >= 2 else "chart_with_downwards_trend",
                "Content-Type": "text/plain; charset=utf-8",
            }
            if alert.url:
                headers["Click"] = alert.url
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            body = alert.message
            if alert.stores:
                body += "\nPickup: " + ", ".join(alert.stores[:3])
            _post(f"{self.server}/{urllib.parse.quote(self.topic)}", body.encode("utf-8"), headers)


class WebhookNotifier(Notifier):
    """Slack, Discord, or any endpoint that accepts a JSON POST."""

    name = "webhook"

    def __init__(self, settings: dict) -> None:
        self.url = settings.get("url", "")

    def send(self, alerts: list[Alert]) -> None:
        if not self.url:
            log.warning("webhook notifier has no url; skipping")
            return
        payload = self._payload(alerts)
        _post(self.url, json.dumps(payload).encode("utf-8"),
              {"Content-Type": "application/json"})

    def _payload(self, alerts: list[Alert]) -> dict:
        lines = []
        for alert in alerts:
            line = f"*{alert.product_name}*\n{alert.message}"
            if alert.stores:
                line += f"\nPickup: {', '.join(alert.stores[:3])}"
            if alert.url:
                line += f"\n{alert.url}"
            lines.append(line)
        text = "\n\n".join(lines)

        if "hooks.slack.com" in self.url:
            return {"text": text}
        if "discord.com/api/webhooks" in self.url or "discordapp.com/api/webhooks" in self.url:
            # Discord rejects bodies over 2000 characters.
            return {"content": text[:1990]}
        return {"alerts": [a.as_dict() for a in alerts], "text": text}


# --------------------------------------------------------------------- helpers


def _post(url: str, body: bytes, headers: dict[str, str]) -> None:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"POST {url.split('?')[0]} failed: HTTP {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"POST {url.split('?')[0]} failed: {exc.reason}") from exc


def _plaintext(alerts: list[Alert]) -> str:
    parts = []
    for alert in alerts:
        block = [alert.product_name, alert.message]
        if alert.stores:
            block.append("Pickup: " + ", ".join(alert.stores))
        if alert.url:
            block.append(alert.url)
        parts.append("\n".join(block))
    return ("\n\n" + "-" * 50 + "\n\n").join(parts) + "\n"


def _html_body(alerts: list[Alert]) -> str:
    rows = []
    for alert in alerts:
        accent = "#16a34a" if alert.severity >= 2 else "#ca8a04"
        stores = (
            f'<div style="color:#475569;font-size:13px;margin-top:6px">In stock near Boston: '
            f'{html.escape(", ".join(alert.stores[:6]))}</div>' if alert.stores else ""
        )
        link = (
            f'<div style="margin-top:10px"><a href="{html.escape(alert.url)}" '
            f'style="color:#2563eb;text-decoration:none;font-weight:600">View on Best Buy &rarr;</a></div>'
            if alert.url else ""
        )
        rows.append(f"""
        <div style="border-left:4px solid {accent};background:#f8fafc;padding:14px 16px;margin:0 0 14px">
          <div style="font-weight:700;font-size:15px;color:#0f172a">{html.escape(alert.product_name)}</div>
          <div style="color:#334155;font-size:14px;margin-top:4px">{html.escape(alert.message)}</div>
          {stores}{link}
        </div>""")

    return f"""<html><body style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;
      background:#ffffff;padding:20px;max-width:640px;margin:0 auto">
      <h2 style="color:#0f172a;font-size:18px;margin:0 0 16px">
        ASUS Zephyrus price alerts &mdash; {len(alerts)} update{'s' if len(alerts) != 1 else ''}</h2>
      {''.join(rows)}
      <p style="color:#94a3b8;font-size:12px;margin-top:20px">
        Sent by zephyrus-price-tracker. Prices and availability come from the Best Buy
        Developer API and can change before you reach checkout.</p>
    </body></html>"""


def build_notifiers(config) -> list[Notifier]:
    """Instantiate every enabled notifier from config."""
    notifiers: list[Notifier] = []
    if config.get("notify.console.enabled", True):
        notifiers.append(ConsoleNotifier())
    if config.get("notify.file.enabled", False):
        notifiers.append(FileNotifier(config.get("notify.file.path", "alerts.jsonl")))
    if config.get("notify.email.enabled", False):
        notifiers.append(EmailNotifier(config.section("notify.email")))
    if config.get("notify.ntfy.enabled", False):
        notifiers.append(NtfyNotifier(config.section("notify.ntfy")))
    if config.get("notify.webhook.enabled", False):
        notifiers.append(WebhookNotifier(config.section("notify.webhook")))
    return notifiers


def dispatch(notifiers: list[Notifier], alerts: list[Alert]) -> list[str]:
    """Send to every channel. A broken channel never blocks the others.

    Returns the names of channels that failed.
    """
    if not alerts:
        return []
    failed = []
    for notifier in notifiers:
        try:
            notifier.send(alerts)
        except Exception as exc:
            failed.append(notifier.name)
            log.error("notifier %s failed: %s", notifier.name, exc)
    return failed
