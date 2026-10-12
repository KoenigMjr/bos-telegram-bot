"""Doppelte Alarme erkennen.

Derselbe Alarm kann mehrfach beim Bot ankommen:
  - eine Automation (z.B. in Home Assistant) veröffentlicht ihn kurz nach dem Original noch einmal,
  - ein Multicast mit mehreren Empfängern wird auf das Topic jedes Empfängers veröffentlicht.

Der Bot merkt sich deshalb für kurze Zeit, welcher Alarm schon an welche Chats ging, und stellt ihn
nicht noch einmal zu. Nur im Speicher, nach einem Neustart beginnt das Gedächtnis leer.

Pro Alarm und Chat gibt es drei Zustände:
  - offen:      noch nichts passiert
  - in Arbeit:  die Nachricht wartet in der Versandschlange oder wird gerade gesendet (claim)
  - zugestellt: Telegram hat sie angenommen (mark_delivered)
Scheitert der Versand endgültig, wird die Reservierung freigegeben (release): Der Chat bekommt bei der
nächsten Kopie des Alarms einen neuen Versuch. Ohne den Zustand "in Arbeit" würde eine Kopie, die eintrifft,
während das Original noch wartet, die Nachricht ein zweites Mal einreihen."""

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


class _Alarm:
    """Was zu einem Alarm bekannt ist: wann er zuerst ankam und welche Chats bedient werden bzw. wurden."""
    __slots__ = ("first_seen", "delivered", "pending")

    def __init__(self, first_seen):
        self.first_seen = first_seen
        self.delivered = set()
        self.pending = set()


class DeliveryMemory:
    """Merkt sich pro Alarm, an welche Chats er schon zugestellt wurde oder gerade zugestellt wird. Jeder
    Eintrag verfällt nach ttl_seconds, und es werden höchstens max_alarms Alarme gemerkt (der älteste fällt
    zuerst heraus)."""

    def __init__(self, ttl_seconds: float = DEFAULT_SECONDS, max_alarms: int = MAX_ALARMS, clock=time.monotonic):
        self.ttl = ttl_seconds
        self.max_alarms = max_alarms
        self.clock = clock
        self._alarms = OrderedDict()   # Kennung -> _Alarm

    def _purge(self) -> None:
        """Verfallene Einträge entfernen. Die Reihenfolge ist die des Eintreffens, ältere stehen vorn."""
        now = self.clock()
        while self._alarms:
            if now - next(iter(self._alarms.values())).first_seen < self.ttl:
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
        self._alarms[key] = _Alarm(self.clock())
        while len(self._alarms) > self.max_alarms:
            self._alarms.popitem(last=False)
        return True

    def is_delivered(self, key, chat_id) -> bool:
        if key is None:
            return False
        self._purge()
        entry = self._alarms.get(key)
        return entry is not None and chat_id in entry.delivered

    def is_pending(self, key, chat_id) -> bool:
        if key is None:
            return False
        self._purge()
        entry = self._alarms.get(key)
        return entry is not None and chat_id in entry.pending

    def claim(self, key, chat_id) -> bool:
        """Reserviert den Chat für diesen Alarm. True, wenn der Aufrufer die Nachricht einreihen soll,
        False, wenn sie schon zugestellt wurde oder gerade zugestellt wird. Ohne Kennung wird nichts
        nachgehalten und jeder Aufruf bekommt True."""
        if key is None:
            return True
        self._purge()
        entry = self._alarms.get(key)
        if entry is None:
            return True
        if chat_id in entry.delivered or chat_id in entry.pending:
            return False
        entry.pending.add(chat_id)
        return True

    def release(self, key, chat_id) -> None:
        """Gibt die Reservierung frei, weil der Versand endgültig gescheitert ist."""
        entry = self._alarms.get(key) if key is not None else None
        if entry is not None:
            entry.pending.discard(chat_id)

    def mark_delivered(self, key, chat_id) -> None:
        entry = self._alarms.get(key) if key is not None else None
        if entry is not None:
            entry.pending.discard(chat_id)
            entry.delivered.add(chat_id)
