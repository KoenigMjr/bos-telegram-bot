"""Integrationstest: das echte main() mit allem, was beim Start passiert (ohne Netzwerk)."""
import datetime as dt
import io
import logging
import os
import tempfile
import unittest
from unittest.mock import patch

from telegram import Chat, Message, MessageEntity, Update, User
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler

from bos_telegram_bot.storage import database as db
from bos_telegram_bot import app as bot_app
from bos_telegram_bot import config as cfg
from bos_telegram_bot import logs
from bos_telegram_bot.core.delivery import DeliveryMemory
from tests.helpers import make_entries
from bos_telegram_bot.core.template import DEFAULT_TEMPLATE

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
        # Die Logzeilen landen im Puffer statt auf der Konsole, und das Logging wird danach zurückgesetzt.
        self.output = io.StringIO()
        capture = patch("sys.stdout", self.output)
        capture.start()
        self.addCleanup(capture.stop)
        self.addCleanup(logs.reset_logging)

    def start(self, **env):
        """Führt bot_app.main() aus, nur das Polling ist ersetzt. Liefert die Application."""
        captured = {}
        environment = {**ENV, **env}
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(Application, "run_polling", lambda self, *a, **k: captured.update(app=self)):
            bot_app.main()
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
        self.assertEqual(data["notification_template"], DEFAULT_TEMPLATE)


TOKEN = "123456789:ABCDEFghijklmnopqrstuvwxyz_0123456"


class LoggingStartupTests(WiringTestCase):
    def lines(self):
        return self.output.getvalue().splitlines()

    def test_info_logs_one_summary_line_with_commands_and_level(self):
        self.start()
        (summary,) = [line for line in self.lines() if "gestartet" in line]
        self.assertTrue(summary.endswith(
            "INFO    app: BOS-Telegram-Bot gestartet: 2 Admin(s), Befehle /ric, /description, /message, /subrictext, Log-Level INFO"
        ), summary)

    def test_info_hides_the_details(self):
        self.start()
        text = self.output.getvalue()
        self.assertNotIn("Befehl /ric", text)
        self.assertNotIn("Nachrichtenvorlage", text)
        self.assertNotIn("DEBUG", text)

    def test_debug_from_the_environment_adds_the_details(self):
        self.start(LOG_LEVEL="debug")
        text = self.output.getvalue()
        self.assertIn("Befehl /ric -> Feld 'ric'", text)
        self.assertIn("Befehl /description -> Feld 'description'", text)
        self.assertIn("Nachrichtenvorlage:", text)
        self.assertIn("Konfiguration: MQTT mqtt.local:1883, Topic homeassistant/boswatch/alarm/+", text)
        self.assertIn("Log-Level DEBUG", text)

    def test_level_from_the_config_file(self):
        with open("data/config.yaml", "w", encoding="utf-8") as f:
            f.write("logging:\n  level: warning\n")
        self.start()
        self.assertEqual(logging.getLogger("bos_telegram_bot").level, logging.WARNING)
        self.assertNotIn("gestartet", self.output.getvalue())

    def test_environment_beats_the_config_file(self):
        with open("data/config.yaml", "w", encoding="utf-8") as f:
            f.write("logging:\n  level: warning\n")
        self.start(LOG_LEVEL="debug")
        self.assertEqual(logging.getLogger("bos_telegram_bot").level, logging.DEBUG)

    def test_an_invalid_level_stops_the_start_with_an_explanation(self):
        with self.assertRaises(SystemExit) as ctx:
            self.start(LOG_LEVEL="laut")
        self.assertIn("logging.level", str(ctx.exception))
        self.assertIn("DEBUG, INFO, WARNING, ERROR, CRITICAL", str(ctx.exception))

    def test_the_token_never_appears_in_the_output(self):
        self.start(TELEGRAM_BOT_TOKEN=TOKEN, LOG_LEVEL="debug")
        logging.getLogger("httpx").info('HTTP Request: POST https://api.telegram.org/bot%s/getMe "HTTP/1.1 200 OK"', TOKEN)
        logging.getLogger("bos_telegram_bot.x").error("Fehler mit Token %s", TOKEN)
        text = self.output.getvalue()
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("ABCDEFghij", text)
        self.assertIn("https://api.telegram.org/bot<TOKEN>/getMe", text)
        self.assertIn("Fehler mit Token <TOKEN>", text)

    def test_the_short_dummy_token_of_the_tests_is_hidden_too(self):
        self.start()
        logging.getLogger("bos_telegram_bot.x").info("Token 123:dummy")
        self.assertNotIn("123:dummy", self.output.getvalue())

    def test_the_config_file_in_use_is_logged(self):
        with open("data/config.yaml", "w", encoding="utf-8") as f:
            f.write("logging:\n  level: info\n")
        self.start()
        self.assertTrue(any("Eigene Anpassungen geladen: " in line and line.split()[2] == "INFO" for line in self.lines()))

    def test_a_migration_is_logged(self):
        db.init_db("data/bot_db.sqlite3")
        db.add_sub("data/bot_db.sqlite3", 5, "subric_text", "a", "Sub-RIC: a", False)
        self.start()
        self.assertIn("1 Abo(s) von Feld 'subric_text' auf 'subricText' umgestellt", self.output.getvalue())

    def test_starting_twice_does_not_duplicate_lines(self):
        self.start()
        self.output.truncate(0)
        self.output.seek(0)
        self.start()
        self.assertEqual(len([line for line in self.lines() if "gestartet" in line]), 1)


class DuplicateDetectionStartupTests(WiringTestCase):
    def write_config(self, text):
        with open("data/config.yaml", "w", encoding="utf-8") as f:
            f.write(text)

    def test_a_memory_for_duplicate_alarms_is_created_by_default(self):
        memory = self.start().bot_data["delivered"]
        self.assertIsInstance(memory, DeliveryMemory)
        self.assertEqual(memory.ttl, 300)

    def test_the_time_comes_from_the_config(self):
        self.write_config("duplicates:\n  remember_seconds: 45\n")
        self.assertEqual(self.start().bot_data["delivered"].ttl, 45)

    def test_zero_means_no_memory_at_all(self):
        self.write_config("duplicates:\n  remember_seconds: 0\n")
        self.assertIsNone(self.start().bot_data["delivered"])

    def test_debug_tells_what_is_active(self):
        self.start(LOG_LEVEL="debug")
        self.assertIn("Doppelte Alarme: werden 300 s lang erkannt", self.output.getvalue())

    def test_debug_tells_when_it_is_off(self):
        self.write_config("duplicates:\n  remember_seconds: 0\n")
        self.start(LOG_LEVEL="debug")
        self.assertIn("Doppelte Alarme: Erkennung abgeschaltet", self.output.getvalue())

    def test_info_stays_quiet_about_it(self):
        self.start()
        self.assertNotIn("Doppelte Alarme", self.output.getvalue())


class CsvLogStartupTests(WiringTestCase):
    """Der Start sagt immer, was mit der CSV passiert ist, auch wenn es keine gibt."""

    def lines(self):
        return self.output.getvalue().splitlines()

    def csv_lines(self):
        return [line for line in self.lines() if "CSV" in line]

    def test_a_loaded_csv_is_reported_at_info_with_the_number_of_records(self):
        with open("data/descriptions_ric.csv", "w", encoding="utf-8") as f:
            f.write(CSV)
        self.start()
        (line,) = self.csv_lines()
        self.assertEqual(line.split()[2], "INFO")
        self.assertTrue(line.endswith(
            f"CSV eingelesen (Feld ric): 2 Datensätze (1 Muster) aus {os.path.abspath('data/descriptions_ric.csv')}"), line)

    def test_no_csv_is_reported_too_with_the_place_where_it_was_searched(self):
        self.start()
        (line,) = self.csv_lines()
        self.assertEqual(line.split()[2], "INFO")
        self.assertTrue(line.endswith(
            f"Keine CSV für Feld ric (gesucht: {os.path.abspath('data/descriptions_ric.csv')}), es werden nur gelernte Namen genutzt"), line)

    def test_a_configured_but_missing_csv_is_a_warning(self):
        self.start(CSV_PATH_RIC="/boswatch3-config/descriptions_ric.csv")
        (line,) = self.csv_lines()
        self.assertEqual(line.split()[2], "WARNING")
        self.assertIn("CSV nicht gefunden (Feld ric)", line)
        self.assertTrue(line.endswith("/boswatch3-config/descriptions_ric.csv"))

    def test_a_csv_from_the_environment_is_reported(self):
        path = os.path.abspath("elsewhere.csv")
        with open(path, "w", encoding="utf-8") as f:
            f.write(CSV)
        self.start(CSV_PATH_RIC=path)
        self.assertIn(f"CSV eingelesen (Feld ric): 2 Datensätze (1 Muster) aus {path}", self.csv_lines()[0])

    def test_fields_without_a_name_search_get_no_csv_line(self):
        self.start(EXTRA_FIELDS="stadtteil,objekt")
        self.assertEqual(len(self.csv_lines()), 1)            # nur ric (mit description), nicht message, subricText, stadtteil

    def test_a_field_with_a_name_search_in_the_config_gets_its_own_line(self):
        with open("data/config.yaml", "w", encoding="utf-8") as f:
            f.write("fields:\n  - for: ric\n    add: description\n  - for: stadtteil\n    add: stadtteil_name\n")
        self.start()
        self.assertEqual(len(self.csv_lines()), 2)
        self.assertTrue(any("Feld stadtteil" in line and "descriptions_stadtteil.csv" in line for line in self.csv_lines()))

    def test_the_csv_line_comes_before_the_start_message(self):
        with open("data/descriptions_ric.csv", "w", encoding="utf-8") as f:
            f.write(CSV)
        self.start()
        text = "\n".join(self.lines())
        self.assertLess(text.index("CSV eingelesen"), text.index("gestartet"))

    def test_the_names_inside_the_csv_are_not_logged_at_info(self):
        with open("data/descriptions_ric.csv", "w", encoding="utf-8") as f:
            f.write(CSV)
        self.start()
        self.assertNotIn("Rettungswagen Musterstadt", self.output.getvalue())


class UnassignedCsvStartupTests(WiringTestCase):
    """Die Warnung beim Start vor einer CSV im Datenordner, die kein Feld verwendet."""
    CSV3 = "for,add,isRegex\n1000011,Wache Nord,false\n1000043,Lagedienst Musterstadt,false\n^23456([0-9]{2})$,Feuerwehr Musterstadt,true\n"
    CSV1 = "for,add,isRegex\n1000011,Wache Nord,false\n"

    def put(self, path, text=CSV1):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def levels(self, level):
        return [line for line in self.output.getvalue().splitlines() if line.split()[2:3] == [level]]

    def warnings(self):
        return self.levels("WARNING")

    def test_a_clean_data_folder_raises_no_warning(self):
        self.put("data/descriptions_ric.csv", self.CSV3)
        self.start()
        self.assertEqual(self.warnings(), [])

    def test_a_csv_in_the_data_folder_that_nobody_uses_is_a_warning(self):
        self.put("data/wachen.csv")
        app = self.start()                                            # der Start läuft trotzdem durch
        self.assertIsNotNone(app)
        (warning,) = self.warnings()
        self.assertIn(f"CSV {os.path.abspath('data/wachen.csv')} liegt im Datenordner, wird aber von keinem Feld verwendet", warning)
        self.assertIn("umbenennen", warning)

    def test_the_warning_comes_before_the_start_message(self):
        self.put("data/wachen.csv")
        self.start()
        lines = self.output.getvalue().splitlines()
        warned = next(i for i, line in enumerate(lines) if "liegt im Datenordner" in line)
        started = next(i for i, line in enumerate(lines) if "gestartet" in line)
        self.assertLess(warned, started)

    def test_a_correctly_named_csv_raises_no_warning_but_a_second_one_does(self):
        self.put("data/descriptions_ric.csv")
        self.put("data/alt.csv")
        self.start()
        (warning,) = self.warnings()
        self.assertIn("alt.csv", warning)
        self.assertNotIn("descriptions_ric.csv", warning)

    def test_a_wrongly_capitalised_name_is_explained(self):
        self.put("data/Descriptions_RIC.csv")
        self.start()
        (warning,) = self.warnings()
        self.assertIn("Der Name muss genau 'descriptions_ric.csv' lauten", warning)

    def test_the_variable_beats_the_default_file_and_the_default_file_is_flagged(self):
        self.put("data/descriptions_ric.csv")
        self.put("extern/andere.csv")
        self.start(CSV_PATH_RIC="extern/andere.csv")
        (warning,) = self.warnings()
        self.assertIn("descriptions_ric.csv", warning)
        self.assertIn("Für Feld 'ric' ist stattdessen extern/andere.csv eingestellt (CSV_PATH_RIC", warning)

    def test_a_csv_set_in_the_config_file_is_assigned(self):
        self.put("data/meine.csv")
        self.put("data/config.yaml", "fields:\n  - for: ric\n    csv: data/meine.csv\n")
        self.start()
        self.assertEqual(self.warnings(), [])
        self.assertTrue(any("CSV eingelesen (Feld ric): 1 Datensatz aus" in line for line in self.levels("INFO")))

    def test_files_in_subfolders_and_other_file_types_are_ignored(self):
        self.put("data/archiv/alt.csv")
        self.put("data/notizen.txt", "x")
        self.start()
        self.assertEqual(self.warnings(), [])

    def test_debug_confirms_that_the_folder_is_clean(self):
        self.put("data/descriptions_ric.csv")
        self.start(LOG_LEVEL="debug")
        self.assertIn("keine nicht zugeordnete CSV", self.output.getvalue())

    def test_the_csv_content_is_not_in_the_log_even_with_debug(self):
        self.put("data/wachen.csv", "for,add,isRegex\n1000011,Geheimer Name,false\n")
        self.put("data/descriptions_ric.csv", "for,add,isRegex\n1000043,Anderer Geheimer Name,false\n")
        self.start(LOG_LEVEL="debug")
        self.assertNotIn("Geheimer Name", self.output.getvalue())


class StartupErrorTests(WiringTestCase):
    def test_missing_token(self):
        with patch.dict(os.environ, {"ADMIN_USERS": "1"}, clear=True), self.assertRaises(SystemExit) as ctx:
            bot_app.main()
        self.assertIn("TELEGRAM_BOT_TOKEN", str(ctx.exception))

    def test_missing_admins(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "123:x"}, clear=True), self.assertRaises(SystemExit) as ctx:
            bot_app.main()
        self.assertIn("ADMIN_USERS ist leer", str(ctx.exception))

    def test_old_variable_name_gives_hint(self):
        env = {"TELEGRAM_BOT_TOKEN": "123:x", "ALLOWED_USERS": "1"}
        with patch.dict(os.environ, env, clear=True), self.assertRaises(SystemExit) as ctx:
            bot_app.main()
        self.assertIn("umbenannt", str(ctx.exception))

    def test_old_config_format_stops_the_start_with_explanation(self):
        with open("data/config.yaml", "w", encoding="utf-8") as f:
            f.write("fields:\n  ric:\n    mode: lookup\n")
        with patch.dict(os.environ, ENV, clear=True), self.assertRaises(SystemExit) as ctx:
            bot_app.main()
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



class MenuTests(unittest.TestCase):
    def test_menu_contains_every_command_with_description(self):
        entries = make_entries()
        menu = bot_app.build_menu(cfg.build_command_map(entries))
        names = [c.command for c in menu]
        self.assertEqual(names, ["start", "abo", "lastraw", "ric", "description", "message", "subrictext"])
        self.assertTrue(all(1 <= len(c.description) <= 256 for c in menu))
        self.assertIn("Fahrzeug / Wache suchen", [c.description for c in menu])


if __name__ == "__main__":
    unittest.main()
