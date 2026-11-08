"""Оповещения о критичных событиях (опционально): Telegram-бот. Отправка в фоне, ошибки не влияют на систему."""
import json
import logging
import threading
import urllib.request

from .service import Notifier

log = logging.getLogger("greenhouse.notify")


class TelegramNotifier(Notifier):
    def __init__(self, token: str, chat_id: str) -> None:
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id

    def notify(self, text: str) -> None:
        threading.Thread(target=self._send, args=(text,), daemon=True).start()

    def _send(self, text: str) -> None:
        try:
            body = json.dumps({"chat_id": self.chat_id, "text": "[greenhouse] " + text[:500]}).encode()
            req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:
            log.warning("telegram notify failed: %s", e)
