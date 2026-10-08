"""Gemeinsame Hilfen für die Tests: Fake-Bot, Fake-Updates, Beispiel-Konfiguration."""
import os
import tempfile
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import database as db
import settings


class FakeBot:
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
    settings.normalize_entries(config)
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
                "command_map": settings.build_command_map(entries),
                "active_fields": {e["for"] for e in entries} | {e["add"] for e in entries if e.get("add")},
                "field_labels": {
                    **{e["for"]: e["label"] for e in entries},
                    **{e["add"]: e["add_label"] for e in entries if e.get("add")},
                },
                "last_payload": None,
            },
        )

    @property
    def entries(self):
        return self.context.bot_data["entries"]

    def entry(self, for_key):
        return next(e for e in self.entries if e["for"] == for_key)

    def subs(self, chat_id):
        return db.get_chat_subs(self.db_path, chat_id)


def make_update(user_id=1, chat_id=None, chat_type="private", title=None, name="Test User"):
    """Fake-Update einer Textnachricht. Liefert (update, reply_text-Mock)."""
    reply = AsyncMock()
    update = NS(
        effective_user=NS(id=user_id, full_name=name, username="testuser"),
        effective_chat=NS(id=chat_id if chat_id is not None else user_id, type=chat_type, title=title),
        message=NS(reply_text=reply),
        callback_query=None,
    )
    return update, reply


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
