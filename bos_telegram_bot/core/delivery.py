"""Doppelte Alarme erkennen.

Derselbe Alarm kann mehrfach beim Bot ankommen:
  - eine Automation (z.B. in Home Assistant) veröffentlicht ihn kurz nach dem Original noch einmal,
  - ein Multicast mit mehreren Empfängern wird auf das Topic jedes Empfängers veröffentlicht.

Der Bot merkt sich deshalb für kurze Zeit, welcher Alarm schon an welche Chats ging, und stellt ihn
nicht noch einmal zu. Nur im Speicher, nach einem Neustart beginnt das Gedächtnis leer. Als gelungen
zählt nur, was Telegram angenommen hat: Chats, bei denen das Senden scheiterte, bekommen bei der
nächsten Kopie einen neuen Versuch."""

import time
from collections import OrderedDict

from bos_telegram_bot.core.matching import split_list

DEFAULT_SECONDS = 300
MAX_ALARMS = 500


def alarm_key(payload: dict):
    """Kennung eines Alarms, die in allen Kopien gleich ist, oder None, wenn es keine gibt.

    BOSWatch3 setzt pro Alarm einen Zeitstempel. Bei einem Multicast enthält 'timestamp_list' die
    Zeitstempel aller Empfänger, und zwar in jeder Nachricht dieses Alarms identisch. Ohne eines der
    beiden Felder gibt es keine Erkennung, dann wird lieber doppelt als gar nicht zugestellt."""
    for field in ("timestamp_list", "timestamp"):
        parts = split_list(payload.get(field))
        if parts:
            return ",".join(parts)
    return None


class DeliveryMemory:
    """Merkt sich pro Alarm, an welche Chats er schon zugestellt wurde. Jeder Eintrag verfällt nach
    ttl_seconds, und es werden höchstens max_alarms Alarme gemerkt (der älteste fällt zuerst heraus)."""

    def __init__(self, ttl_seconds: float = DEFAULT_SECONDS, max_alarms: int = MAX_ALARMS, clock=time.monotonic):
        self.ttl = ttl_seconds
        self.max_alarms = max_alarms
        self.clock = clock
        self._alarms = OrderedDict()   # Kennung -> (Zeitpunkt des ersten Eintreffens, {Chat-IDs})

    def _purge(self) -> None:
        """Verfallene Einträge entfernen. Die Reihenfolge ist die des Eintreffens, ältere stehen vorn."""
        now = self.clock()
        while self._alarms:
            first_seen, _ = next(iter(self._alarms.values()))
            if now - first_seen < self.ttl:
                break
            self._alarms.popitem(last=False)

    def knows(self, key) -> bool:
        if key is None:
            return False
        self._purge()
        return key in self._alarms

    def register(self, key) -> bool:
        """Trägt einen Alarm ein. True, wenn er neu ist, False bei einer Wiederholung.
        Ohne Kennung ist jeder Alarm neu."""
        if key is None:
            return True
        self._purge()
        if key in self._alarms:
            return False
        self._alarms[key] = (self.clock(), set())
        while len(self._alarms) > self.max_alarms:
            self._alarms.popitem(last=False)
        return True

    def is_delivered(self, key, chat_id) -> bool:
        if key is None:
            return False
        self._purge()
        entry = self._alarms.get(key)
        return entry is not None and chat_id in entry[1]

    def mark_delivered(self, key, chat_id) -> None:
        entry = self._alarms.get(key) if key is not None else None
        if entry is not None:
            entry[1].add(chat_id)
