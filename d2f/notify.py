"""Alerts: one Alert model, several channels (stdlib only).

Channels (D2F_NOTIFY_CHANNELS, comma separated; default "console,fusion"):
  console   coloured line in the terminal
  fusion    Text Commands window in Fusion (through the bridge)
  discord   DISCORD_WEBHOOK_URL
  telegram  TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID

Routing by severity follows docs/errors-and-alerts.md.
"""

import datetime
import json
import os
import sys
import time
import urllib.request

ICON = {"INFO": "ℹ️", "WARN": "🟡", "ERROR": "🔴", "BLOCK": "🟠", "DONE": "✅"}
ROUTES = {
    "INFO": {"console", "fusion"},
    "WARN": {"console", "fusion"},
    "ERROR": {"console", "fusion", "discord", "telegram"},
    "BLOCK": {"console", "fusion", "discord", "telegram"},
    "DONE": {"console", "fusion", "discord", "telegram"},
}
DEDUP_WINDOW_S = 60.0


def make_alert(run_id, code, severity, stage, title, message, where=None, question=None,
               suggestion=None, attachments=None):
    return {"run_id": run_id, "code": code, "severity": severity, "stage": stage, "title": title,
            "message": message, "where": where or {}, "question": question, "suggestion": suggestion,
            "attachments": attachments or [],
            "time": datetime.datetime.now().astimezone().isoformat(timespec="seconds")}


def format_text(a, part_name=""):
    head = "%s [%s] %s" % (ICON.get(a["severity"], "•"), a["code"] or a["severity"], a["title"])
    if part_name:
        head += " — " + part_name
    lines = [head, a["message"]]
    w = a.get("where") or {}
    if w.get("feature") or w.get("check"):
        lines.append("위치: %s" % " / ".join(str(w[k]) for k in ("feature", "check", "parameter") if w.get(k)))
    if a.get("suggestion"):
        lines.append("제안: " + a["suggestion"])
    q = a.get("question")
    if q:
        lines.append("❓ " + q.get("text", ""))
        for i, opt in enumerate(q.get("options") or [], 1):
            lines.append("   %d) %s" % (i, opt))
    return "\n".join(lines)


class Notifier:
    def __init__(self, part_name="", bridge=None, channels=None):
        env = os.environ.get("D2F_NOTIFY_CHANNELS", "console,fusion")
        self.channels = set(channels or [c.strip() for c in env.split(",") if c.strip()])
        self.part_name = part_name
        self.bridge = bridge
        self.sent = []
        self.failures = []
        self._recent = {}

    def send(self, alert):
        key = (alert["run_id"], alert["code"], (alert.get("where") or {}).get("feature"),
               (alert.get("where") or {}).get("check"), alert["message"])
        now = time.time()
        if alert["severity"] != "DONE" and now - self._recent.get(key, 0) < DEDUP_WINDOW_S:
            return
        self._recent[key] = now
        targets = ROUTES.get(alert["severity"], {"console"}) & self.channels
        text = format_text(alert, self.part_name)
        for ch in sorted(targets):
            try:
                getattr(self, "_to_" + ch)(alert, text)
            except Exception as e:
                self.failures.append({"channel": ch, "error": str(e)})
                if ch != "console":
                    self._to_console({"severity": "WARN"}, "🟡 [E505] %s 알림 전송 실패: %s" % (ch, e))
        self.sent.append(alert)

    # ---- channels
    def _to_console(self, alert, text):
        colour = {"ERROR": "31", "BLOCK": "33", "WARN": "33", "DONE": "32"}.get(alert["severity"])
        if colour and sys.stdout.isatty():
            text = "\x1b[%sm%s\x1b[0m" % (colour, text)
        print(text + "\n")

    def _to_fusion(self, alert, text):
        if self.bridge is None:
            return
        self.bridge.alert(alert["severity"], alert["code"] or "", alert["title"],
                          alert["message"], modal=bool(alert.get("modal")))

    def _to_discord(self, alert, text):
        url = os.environ.get("DISCORD_WEBHOOK_URL")
        if not url:
            return
        _post_json(url, {"content": text[:1900]})

    def _to_telegram(self, alert, text):
        token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
        if not (token and chat):
            return
        _post_json("https://api.telegram.org/bot%s/sendMessage" % token,
                   {"chat_id": chat, "text": text[:4000]})


def _post_json(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()
