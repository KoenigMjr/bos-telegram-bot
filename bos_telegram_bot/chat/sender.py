"""Versand der Alarmnachrichten an Telegram.

Der Empfang von MQTT wartet nie auf Telegram: Eine Nachricht wird nur eingereiht (submit) und von hier aus
zugestellt. Dabei gilt:

  - Pro Chat gibt es eine eigene Warteschlange. Die Reihenfolge je Chat bleibt erhalten, und ein Chat, bei dem
    Telegram hängt, hält die anderen nicht auf.
  - Zwischen zwei Nachrichten an denselben Chat liegt mindestens min_interval Sekunden (Telegram empfiehlt etwa
    eine Nachricht pro Sekunde und Chat), über alle Chats zusammen gehen höchstens max_per_second Nachrichten pro
    Sekunde raus (Telegram erlaubt etwa 30).
  - Scheitert ein Versuch, wird bis zu attempts-mal versucht, mit den vorgegebenen Pausen. Verlangt Telegram eine
    Wartezeit (RetryAfter), wird genau sie eingehalten. Nach max_wait Sekunden gibt der Sender bei der Nachricht auf.
  - Dauerhafte Fehler (der Bot wurde aus der Gruppe entfernt, ungültige Nachricht) werden nicht wiederholt.
  - Die Warteschlange ist begrenzt. Ist sie voll, fällt die älteste wartende Nachricht heraus.

Jede Nachricht meldet ihr Ergebnis genau einmal an die Funktion on_result(chat_id, angenommen, grund)."""

import asyncio
import logging
import time
from collections import deque
from itertools import count

from telegram.error import BadRequest, ChatMigrated, Forbidden, InvalidToken, RetryAfter

from bos_telegram_bot.core import sending as sending_settings

log = logging.getLogger(__name__)

MAX_QUEUE = 500
SHUTDOWN_SECONDS = 10.0

# Diese Fehler ändern sich durch Wiederholen nicht. BadRequest ist in der Bibliothek eine Unterklasse von
# NetworkError, deshalb muss es VOR den wiederholbaren Fehlern geprüft werden.
_PERMANENT = (Forbidden, BadRequest, ChatMigrated, InvalidToken)


def _describe(error) -> str:
    return f"{type(error).__name__}: {error}"


def _seconds(value) -> float:
    """RetryAfter liefert je nach Version der Bibliothek Sekunden oder ein timedelta."""
    total = getattr(value, "total_seconds", None)
    return float(total()) if callable(total) else float(value)


def _hint(error) -> str:
    if isinstance(error, Forbidden):
        return "Der Bot darf dort nicht schreiben (entfernt oder blockiert?). Das Abo bleibt bestehen und lässt sich mit /abo entfernen."
    if isinstance(error, ChatMigrated):
        return f"Die Gruppe ist zur Supergruppe geworden, die neue Chat-ID ist {error.new_chat_id}. Das Abo muss dort neu angelegt werden."
    if isinstance(error, BadRequest):
        return "Telegram hat die Nachricht abgelehnt, ein neuer Versuch ändert daran nichts."
    return "Ein neuer Versuch ändert daran nichts."


class _Item:
    __slots__ = ("seq", "chat_id", "text", "on_result", "enqueued", "finished")

    def __init__(self, seq, chat_id, text, on_result, enqueued):
        self.seq, self.chat_id, self.text, self.on_result, self.enqueued = seq, chat_id, text, on_result, enqueued
        self.finished = False


class Sender:
    def __init__(self, bot, *, min_interval, max_per_second, attempts, retry_pauses, max_wait,
                 max_queue=MAX_QUEUE, clock=time.monotonic, sleep=asyncio.sleep):
        self.bot = bot
        self.min_interval = min_interval
        self.gap = 1.0 / max_per_second
        self.attempts = attempts
        self.retry_pauses = list(retry_pauses) or [0.0]
        self.max_wait = max_wait
        self.max_queue = max_queue
        self.clock = clock
        self._sleep = sleep
        self._queues = {}            # Chat -> wartende Nachrichten
        self._workers = {}           # Chat -> laufende Aufgabe
        self._last_start = {}        # Chat -> Zeitpunkt des letzten Sendens
        self._next_slot = 0.0        # frühester Zeitpunkt für das nächste Senden über alle Chats
        self._outstanding = 0        # wartend + gerade in Arbeit
        self._idle_waiters = []
        self._seq = count(1)
        self._closed = False

    @classmethod
    def from_config(cls, bot, settings: dict, **overrides):
        """Aus dem Block 'sending:' der Konfiguration (auch unvollständig, es gelten dann die Standardwerte)."""
        s = sending_settings.normalized(settings)
        return cls(bot, min_interval=s["min_interval_seconds"], max_per_second=s["max_per_second"],
                   attempts=s["attempts"], retry_pauses=s["retry_pause_seconds"], max_wait=s["max_wait_seconds"],
                   **overrides)

    # ------------------------------------------------------------------ Einreihen und Abfragen
    def now(self) -> float:
        return self.clock()

    @property
    def pending(self) -> int:
        """Nachrichten, die noch warten oder gerade gesendet werden."""
        return self._outstanding

    @property
    def queued(self) -> int:
        """Nachrichten, die noch auf ihren Versand warten."""
        return sum(len(q) for q in self._queues.values())

    def submit(self, chat_id, text, on_result) -> None:
        """Reiht eine Nachricht ein. on_result(chat_id, angenommen, grund) wird genau einmal aufgerufen."""
        item = _Item(next(self._seq), chat_id, text, on_result, self.clock())
        self._outstanding += 1
        if self._closed:
            self._finish(item, False, "Versand ist beendet")
            return
        if self.queued >= self.max_queue:
            self._drop_oldest()
        queue = self._queues.setdefault(chat_id, deque())
        queue.append(item)
        log.debug("Chat %s: in der Warteschlange (%d wartend)", chat_id, len(queue))
        if chat_id not in self._workers:
            self._workers[chat_id] = asyncio.create_task(self._worker(chat_id))

    def _drop_oldest(self) -> None:
        heads = [q[0] for q in self._queues.values() if q]
        if not heads:
            return
        oldest = min(heads, key=lambda item: item.seq)
        self._queues[oldest.chat_id].popleft()
        log.error("Warteschlange voll (%d Nachrichten): die älteste, an Chat %s, wird verworfen", self.max_queue, oldest.chat_id)
        self._finish(oldest, False, "Warteschlange voll")

    def _finish(self, item, accepted, reason) -> None:
        if item.finished:
            return
        item.finished = True
        try:
            item.on_result(item.chat_id, accepted, reason)
        except Exception:
            log.exception("Auswertung des Sendeergebnisses für Chat %s fehlgeschlagen", item.chat_id)
        self._outstanding -= 1
        if self._outstanding == 0:
            for waiter in self._idle_waiters:
                if not waiter.done():
                    waiter.set_result(None)
            self._idle_waiters.clear()

    # ------------------------------------------------------------------ Versand
    async def _worker(self, chat_id) -> None:
        try:
            while self._queues.get(chat_id):
                item = self._queues[chat_id].popleft()
                try:
                    await self._deliver(item)
                except asyncio.CancelledError:
                    self._finish(item, False, "Versand abgebrochen (der Bot wird beendet)")
                    raise
                except Exception as error:                  # darf nie den Worker beenden
                    log.exception("Unerwarteter Fehler beim Senden an Chat %s", chat_id)
                    self._finish(item, False, _describe(error))
        finally:
            if not self._queues.get(chat_id):
                self._queues.pop(chat_id, None)
            self._workers.pop(chat_id, None)

    async def _wait_turn(self, chat_id) -> None:
        """Wartet, bis dieser Chat wieder dran ist und die Gesamtgrenze es erlaubt."""
        wait = self._last_start.get(chat_id, float("-inf")) + self.min_interval - self.clock()
        if wait > 0:
            await self._sleep(wait)
        now = self.clock()
        slot = max(now, self._next_slot)
        self._next_slot = slot + self.gap              # den Platz sofort belegen, bevor ein anderer Chat ihn bekommt
        if slot > now:
            await self._sleep(slot - now)
        self._last_start[chat_id] = self.clock()

    def _pause_after(self, attempt) -> float:
        return self.retry_pauses[min(attempt - 1, len(self.retry_pauses) - 1)]

    async def _deliver(self, item) -> None:
        first_try = None
        for attempt in range(1, self.attempts + 1):
            await self._wait_turn(item.chat_id)
            started = self.clock()
            if first_try is None:
                first_try = started
            try:
                await self.bot.send_message(chat_id=item.chat_id, text=item.text, parse_mode="HTML")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                problem = _describe(error)
                if isinstance(error, _PERMANENT):
                    log.error("Senden an Chat %s nicht möglich (%s). %s", item.chat_id, problem, _hint(error))
                    self._finish(item, False, problem)
                    return
                pause = _seconds(error.retry_after) if isinstance(error, RetryAfter) else self._pause_after(attempt)
                if attempt >= self.attempts:
                    log.error("Senden an Chat %s aufgegeben nach %d Versuchen (%s)", item.chat_id, attempt, problem)
                    self._finish(item, False, problem)
                    return
                if self.clock() - first_try + pause > self.max_wait:
                    log.error("Senden an Chat %s aufgegeben: Die Höchstwartezeit von %g s wäre überschritten (%s)",
                              item.chat_id, self.max_wait, problem)
                    self._finish(item, False, problem)
                    return
                log.warning("Senden an Chat %s fehlgeschlagen (%s), Versuch %d/%d, neuer Versuch in %g s",
                            item.chat_id, problem, attempt, self.attempts, pause)
                await self._sleep(pause)
            else:
                log.debug("Chat %s: gesendet in %.2f s (Versuch %d, %.1f s in der Warteschlange)",
                          item.chat_id, self.clock() - started, attempt, first_try - item.enqueued)
                self._finish(item, True, None)
                return

    # ------------------------------------------------------------------ Warten und Beenden
    async def drain(self) -> None:
        """Kehrt zurück, sobald nichts mehr wartet und nichts mehr gesendet wird."""
        while self._outstanding:
            waiter = asyncio.get_running_loop().create_future()
            self._idle_waiters.append(waiter)
            try:
                await waiter
            finally:
                if waiter in self._idle_waiters:
                    self._idle_waiters.remove(waiter)

    async def close(self, timeout=SHUTDOWN_SECONDS) -> int:
        """Nimmt nichts Neues mehr an, stellt noch bis zu timeout Sekunden zu und bricht dann ab. Liefert die
        Zahl der Nachrichten, die nicht mehr zugestellt werden konnten."""
        self._closed = True
        if self._outstanding:
            drain = asyncio.ensure_future(self.drain())
            timer = asyncio.ensure_future(self._sleep(timeout))
            await asyncio.wait({drain, timer}, return_when=asyncio.FIRST_COMPLETED)
            for task in (drain, timer):
                if not task.done():
                    task.cancel()
            await asyncio.gather(drain, timer, return_exceptions=True)
        undelivered = self._outstanding
        workers = list(self._workers.values())
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        for queue in list(self._queues.values()):
            while queue:
                self._finish(queue.popleft(), False, "Beim Beenden nicht zugestellt")
        self._queues.clear()
        self._workers.clear()
        if undelivered:
            log.error("Beim Beenden nicht zugestellt: %d Nachricht(en)", undelivered)
        return undelivered
