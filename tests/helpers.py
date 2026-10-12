"""Gemeinsame Hilfen für die Tests: Fake-Bot, Fake-Updates, Beispiel-Konfiguration."""
import asyncio
import heapq
import itertools
import os
import tempfile
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from bos_telegram_bot.storage import database as db
from bos_telegram_bot.chat import admin as chat_admin
from bos_telegram_bot.chat import fields as chat_fields
from bos_telegram_bot import config as cfg
from bos_telegram_bot.core.template import DEFAULT_TEMPLATE

BOT_ID = 999

_real_sleep = asyncio.sleep      # Tests ersetzen asyncio.sleep mitunter global, die virtuelle Uhr braucht das echte
_message_ids = itertools.count(100)


class FakeBot:
    id = BOT_ID

    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})


def make_entries(extra=None, csv_rows=None):
    """Standard-Einträge wie in der mitgelieferten config.yaml, ohne Dateizugriff."""
    fields = [
        {"for": "ric", "label": "RIC", "match": "exact", "add": "description", "add_label": "Fahrzeug / Wache"},
        {"for": "message", "label": "Alarmstichwort"},
        {"for": "subricText", "label": "Sub-RIC", "match": "exact"},
    ] + (extra or [])
    config = {"fields": fields}
    cfg.normalize_entries(config)
    for entry in config["fields"]:
        entry["csv_rows"] = list(csv_rows or []) if entry.get("add") else []
    return config["fields"]


class Env:
    """Eine komplette Testumgebung: temporäre Datenbank, Kontext, Fake-Bot."""

    def __init__(self, test, admins=(1,), csv_rows=None, extra=None):
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.db_path = os.path.join(tmp.name, "bot.sqlite3")
        db.init_db(self.db_path)

        entries = make_entries(extra, csv_rows)
        self.bot = FakeBot()
        self.context = NS(
            bot=self.bot,
            user_data={},
            args=[],
            bot_data={
                "config": {"files": {"db_path": self.db_path}},
                "admins": list(admins),
                "entries": entries,
                "command_map": cfg.build_command_map(entries),
                "active_fields": {e["for"] for e in entries} | {e["add"] for e in entries if e.get("add")},
                "field_labels": {
                    **{e["for"]: e["label"] for e in entries},
                    **{e["add"]: e["add_label"] for e in entries if e.get("add")},
                },
                "last_payload": None,
                "prompt_handlers": self._prompt_handlers(entries),
                "notification_template": DEFAULT_TEMPLATE,
            },
        )

    @staticmethod
    def _prompt_handlers(entries):
        """Wie in app.main(): welcher Befehl gehört zu welcher Rückfrage."""
        result = {("adduser", ""): chat_admin.adduser_handler}
        for cmd in cfg.build_command_map(entries):
            make = chat_fields.make_for_handler if cmd["kind"] == "for" else chat_fields.make_add_handler
            result[(cmd["kind"], cmd["entry"]["for"])] = make(cmd["entry"])
        return result

    @property
    def entries(self):
        return self.context.bot_data["entries"]

    def entry(self, for_key):
        return next(e for e in self.entries if e["for"] == for_key)

    def subs(self, chat_id):
        return db.get_chat_subs(self.db_path, chat_id)


def make_update(user_id=1, chat_id=None, chat_type="private", title=None, name="Test User",
                text=None, reply_to=None):
    """Fake-Update einer Textnachricht. Liefert (update, reply_text-Mock).
    Der Mock gibt eine Nachricht mit eigener message_id zurück, wie Telegram es tut."""
    reply = AsyncMock(return_value=NS(message_id=next(_message_ids)))
    update = NS(
        effective_user=NS(id=user_id, full_name=name, username="testuser"),
        effective_chat=NS(id=chat_id if chat_id is not None else user_id, type=chat_type, title=title),
        message=NS(reply_text=reply, text=text, reply_to_message=reply_to),
        callback_query=None,
    )
    return update, reply


def bot_message(text, message_id=1):
    """Eine Nachricht des Bots, auf die geantwortet wird."""
    return NS(from_user=NS(id=BOT_ID), text=text, message_id=message_id)


def make_callback(data, user_id=1, chat_id=None):
    """Fake-Update eines Button-Klicks. Liefert (update, query)."""
    query = NS(data=data, from_user=NS(id=user_id), answer=AsyncMock(), edit_message_text=AsyncMock())
    update = NS(
        effective_user=NS(id=user_id, full_name="Test User", username="testuser"),
        effective_chat=NS(id=chat_id if chat_id is not None else user_id, type="private", title=None),
        callback_query=query,
        message=None,
    )
    return update, query


def sent_text(mock):
    """Text des letzten Aufrufs eines reply_text/edit_message_text-Mocks."""
    return mock.call_args[0][0]


class Clock:
    """Eine Uhr, die nur vorgeht, wenn der Test es sagt."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class VirtualTime:
    """Eine Uhr samt sleep, die nur vorgeht, wenn alle Aufgaben warten (run_until). So lässt sich der Versand mit
    Abständen, Pausen und Wiederholungen exakt prüfen, ohne echte Sekunden abzuwarten."""

    def __init__(self, start=1000.0):
        self.now = start
        self._sleepers = []                       # (Weckzeit, Nummer, Future)
        self._numbers = itertools.count()

    def clock(self):
        return self.now

    async def sleep(self, seconds):
        if seconds <= 0:
            await _real_sleep(0)
            return
        future = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (self.now + seconds, next(self._numbers), future))
        await future

    @staticmethod
    async def settle():
        """Lässt alle Aufgaben laufen, bis sie warten (auf die Uhr oder auf etwas anderes)."""
        for _ in range(40):
            await _real_sleep(0)

    async def run_until(self, done, limit=10000):
        """Lässt die Zeit zum nächsten Wecker springen, bis done() wahr ist."""
        for _ in range(limit):
            await self.settle()
            if done():
                return
            while self._sleepers and self._sleepers[0][2].done():       # abgebrochene Schläfer überspringen
                heapq.heappop(self._sleepers)
            if not self._sleepers:
                raise AssertionError("Stillstand: Nichts wartet auf die Uhr, aber es ist noch nicht fertig")
            wake, _, future = heapq.heappop(self._sleepers)
            self.now = max(self.now, wake)
            future.set_result(None)
        raise AssertionError("Zu viele Schritte, der Ablauf endet nicht")

    async def run_until_idle(self, sender):
        await self.run_until(lambda: sender.pending == 0)


class ScriptedBot:
    """Ein Bot, dessen Antworten der Test vorgibt. Pro Chat wird bei jedem Aufruf der nächste Eintrag der Liste
    verwendet: eine Ausnahme wird ausgelöst, ein Future abgewartet (so kann ein Aufruf 'hängen'), None heißt Erfolg.
    Ist die Liste leer, klappt der Aufruf."""

    id = BOT_ID

    def __init__(self, clock=None, script=None, latency=0.0, sleep=None):
        self.clock = clock or (lambda: 0.0)
        self.script = {chat: list(outcomes) for chat, outcomes in (script or {}).items()}
        self.latency = latency
        self._sleep = sleep
        self.calls = []

    async def send_message(self, chat_id, text, **kwargs):
        self.calls.append({"chat_id": chat_id, "text": text, "at": self.clock(), **kwargs})
        outcomes = self.script.get(chat_id)
        outcome = outcomes.pop(0) if outcomes else None
        if self.latency and self._sleep:
            await self._sleep(self.latency)
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, asyncio.Future):
            await outcome

    def times(self, chat_id=None):
        return [c["at"] for c in self.calls if chat_id is None or c["chat_id"] == chat_id]

    def texts(self, chat_id=None):
        return [c["text"] for c in self.calls if chat_id is None or c["chat_id"] == chat_id]


class SendHarness:
    """Der echte Sender mit virtueller Zeit hinter einem beliebigen Bot. handle() verteilt einen Alarm und wartet,
    bis alle Nachrichten ein Ergebnis haben, damit Tests danach ohne Warten prüfen können."""

    def __init__(self, bot, **settings):
        from bos_telegram_bot.chat.sender import Sender
        self.time = VirtualTime()
        self.bot = bot
        self.sender = Sender.from_config(bot, settings, clock=self.time.clock, sleep=self.time.sleep)

    async def handle(self, db_path, active_fields, template, payload, delivered=None):
        from bos_telegram_bot.mqtt import dispatch
        await dispatch.handle_payload(db_path, active_fields, template, payload, self.sender, delivered)
        await self.time.run_until_idle(self.sender)
