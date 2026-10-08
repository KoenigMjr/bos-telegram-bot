"""Integrationstest: das echte main() mit allem, was beim Start passiert (ohne Netzwerk)."""
import datetime as dt
import os
import tempfile
import unittest
from unittest.mock import patch

from telegram import Chat, Message, MessageEntity, Update, User
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler

import database as db
import main

CSV = "for,add,isRegex\n1234567,Rettungswagen Musterstadt A-Wehr,false\n^23456([0-9]{2})$,Feuerwehr Musterstadt,true\n"
ENV = {"TELEGRAM_BOT_TOKEN": "123:dummy", "ADMIN_USERS": "1,2", "MQTT_HOST": "mqtt.local"}


class WiringTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        previous = os.getcwd()
        os.chdir(tmp.name)
        self.addCleanup(os.chdir, previous)
        os.makedirs("data")
        for patcher in (patch("builtins.print"),):
            patcher.start()
            self.addCleanup(patcher.stop)

    def start(self, **env):
        """Führt main.main() aus, nur das Polling ist ersetzt. Liefert die Application."""
        captured = {}
        environment = {**ENV, **env}
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(Application, "run_polling", lambda self, *a, **k: captured.update(app=self)):
            main.main()
        return captured["app"]


class StartupTests(WiringTestCase):
    def test_registers_all_handlers(self):
        app = self.start(EXTRA_FIELDS="stadtteil")
        handlers_ = app.handlers[0]
        commands = sorted(c for h in handlers_ if isinstance(h, CommandHandler) for c in h.commands)
        self.assertEqual(commands, sorted(["abo", "adduser", "description", "lastraw", "message", "ric",
                                           "stadtteil", "start", "subrictext", "users"]))
        self.assertTrue(any(isinstance(h, CallbackQueryHandler) for h in handlers_))
        self.assertTrue(any(isinstance(h, MessageHandler) for h in handlers_))

    def test_every_question_knows_its_command(self):
        app = self.start()
        self.assertEqual(sorted(app.bot_data["prompt_handlers"]),
                         [("add", "ric"), ("adduser", ""), ("for", "message"), ("for", "ric"), ("for", "subricText")])

    def test_csv_is_detected_in_data_folder_and_loaded(self):
        with open("data/descriptions_ric.csv", "w", encoding="utf-8") as f:
            f.write(CSV)
        app = self.start()
        ric = next(e for e in app.bot_data["entries"] if e["for"] == "ric")
        self.assertTrue(ric["csv"].endswith("descriptions_ric.csv"))
        self.assertEqual(len(ric["csv_rows"]), 2)
        self.assertEqual(os.listdir("data").count("descriptions_ric.csv"), 1)

    def test_no_csv_means_empty_list_and_nothing_is_created(self):
        app = self.start()
        ric = next(e for e in app.bot_data["entries"] if e["for"] == "ric")
        self.assertEqual(ric["csv_rows"], [])
        self.assertEqual([f for f in os.listdir("data") if f.endswith(".csv")], [])

    def test_legacy_subscriptions_are_migrated(self):
        db.init_db("data/bot_db.sqlite3")
        db.add_sub("data/bot_db.sqlite3", 5, "subric_text", "a", "Sub-RIC: a", False)
        self.start()
        self.assertEqual([s["field"] for s in db.get_all_subs("data/bot_db.sqlite3")], ["subricText"])

    def test_bot_data_is_complete(self):
        data = self.start().bot_data
        self.assertEqual(data["admins"], [1, 2])
        self.assertEqual(data["config"]["mqtt"]["host"], "mqtt.local")
        self.assertEqual(data["active_fields"], {"ric", "description", "message", "subricText"})
        self.assertEqual(data["field_labels"]["description"], "Fahrzeug / Wache")
        self.assertIsNone(data["last_payload"])


class StartupErrorTests(WiringTestCase):
    def test_missing_token(self):
        with patch.dict(os.environ, {"ADMIN_USERS": "1"}, clear=True), self.assertRaises(SystemExit) as ctx:
            main.main()
        self.assertIn("TELEGRAM_BOT_TOKEN", str(ctx.exception))

    def test_missing_admins(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "123:x"}, clear=True), self.assertRaises(SystemExit) as ctx:
            main.main()
        self.assertIn("ADMIN_USERS ist leer", str(ctx.exception))

    def test_old_variable_name_gives_hint(self):
        env = {"TELEGRAM_BOT_TOKEN": "123:x", "ALLOWED_USERS": "1"}
        with patch.dict(os.environ, env, clear=True), self.assertRaises(SystemExit) as ctx:
            main.main()
        self.assertIn("umbenannt", str(ctx.exception))

    def test_old_config_format_stops_the_start_with_explanation(self):
        with open("data/config.yaml", "w", encoding="utf-8") as f:
            f.write("fields:\n  ric:\n    mode: lookup\n")
        with patch.dict(os.environ, ENV, clear=True), self.assertRaises(SystemExit) as ctx:
            main.main()
        self.assertIn("neues Format", str(ctx.exception))


class ReplyFilterTests(WiringTestCase):
    """Der registrierte Filter darf nur Antworten auf Nachrichten durchlassen."""

    def setUp(self):
        super().setUp()
        app = self.start()
        self.handler = next(h for h in app.handlers[0] if isinstance(h, MessageHandler))
        self.chat = Chat(id=1, type="private")
        self.user = User(id=1, first_name="Test", is_bot=False)

    def update(self, text, reply_to=None, command=False):
        entities = [MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))] if command else None
        message = Message(message_id=2, date=dt.datetime.now(dt.timezone.utc), chat=self.chat,
                          from_user=self.user, text=text, reply_to_message=reply_to, entities=entities)
        return Update(update_id=1, message=message)

    def original(self):
        return Message(message_id=1, date=dt.datetime.now(dt.timezone.utc), chat=self.chat,
                       from_user=self.user, text="✍️ Frage")

    def test_reply_with_text_passes(self):
        self.assertTrue(self.handler.check_update(self.update("301*", reply_to=self.original())))

    def test_plain_message_is_ignored(self):
        self.assertFalse(self.handler.check_update(self.update("301*")))

    def test_command_is_left_to_the_command_handlers(self):
        self.assertFalse(self.handler.check_update(self.update("/ric 301*", reply_to=self.original(), command=True)))


if __name__ == "__main__":
    unittest.main()
