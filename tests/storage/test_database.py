import os
import sqlite3
import tempfile
import unittest

from bos_telegram_bot.storage import database as db
from bos_telegram_bot import app as bot_app


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "bot.sqlite3")
        db.init_db(self.path)

    def test_init_is_idempotent(self):
        db.add_sub(self.path, 1, "ric", "1", "A", False)
        db.init_db(self.path)
        self.assertEqual(len(db.get_chat_subs(self.path, 1)), 1)

    def test_rename_field_migrates_subscriptions(self):
        db.add_sub(self.path, 1, "subric_text", "a", "Sub-RIC: a", False)
        db.add_sub(self.path, 2, "subric_text", "b", "Sub-RIC: b", False)
        self.assertEqual(db.rename_field(self.path, "subric_text", "subricText"), 2)
        self.assertEqual([s["field"] for s in db.get_all_subs(self.path)], ["subricText", "subricText"])

    def test_rename_field_drops_duplicates_instead_of_failing(self):
        db.add_sub(self.path, 1, "subric_text", "a", "alt", False)
        db.add_sub(self.path, 1, "subricText", "a", "neu", False)
        db.rename_field(self.path, "subric_text", "subricText")
        subs = db.get_all_subs(self.path)
        self.assertEqual([(s["field"], s["alias"]) for s in subs], [("subricText", "neu")])

    def test_rename_unknown_field_does_nothing(self):
        db.add_sub(self.path, 1, "ric", "1", "A", False)
        self.assertEqual(db.rename_field(self.path, "gibtsnicht", "x"), 0)
        self.assertEqual(len(db.get_all_subs(self.path)), 1)

    def test_legacy_rename_table_is_applied_by_main(self):
        self.assertEqual(bot_app.LEGACY_FIELD_RENAMES, {"subric_text": "subricText"})

    def test_learned_pairs_upsert_and_lookup(self):
        db.upsert_learned(self.path, "ric", "1", "Alt")
        db.upsert_learned(self.path, "ric", "1", "Neu")
        self.assertEqual(db.get_learned(self.path, "ric", "1"), "Neu")
        self.assertIsNone(db.get_learned(self.path, "ric", "2"))
        self.assertEqual(len(db.list_learned(self.path, "ric")), 1)
        self.assertEqual(db.list_learned(self.path, "anderes"), [])

    def test_remove_chat_subs_only_touches_that_chat(self):
        db.add_sub(self.path, 1, "ric", "1", "A", False)
        db.add_sub(self.path, 2, "ric", "1", "A", False)
        db.remove_chat_subs(self.path, 1)
        self.assertEqual([s["chat_id"] for s in db.get_all_subs(self.path)], [2])


class RequestChatTests(unittest.TestCase):
    """Aus welchem Chat eine Zugriffsanfrage kam, damit die Freigabe-Meldung dort ankommen kann."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "bot.sqlite3")
        db.init_db(self.path)

    def test_the_request_chat_is_stored_and_read_back(self):
        db.set_user_status(self.path, 77, "pending", "Fremde Person", -1001234)
        user = db.get_user(self.path, 77)
        self.assertEqual((user["status"], user["name"], user["request_chat_id"]), ("pending", "Fremde Person", -1001234))

    def test_an_unknown_user_is_none(self):
        self.assertIsNone(db.get_user(self.path, 999))

    def test_a_status_change_without_a_chat_keeps_the_stored_chat(self):
        db.set_user_status(self.path, 77, "pending", "Fremde Person", -1001234)
        db.set_user_status(self.path, 77, "allowed")                        # Freigabe: kein neuer Chat angegeben
        user = db.get_user(self.path, 77)
        self.assertEqual((user["status"], user["request_chat_id"], user["name"]), ("allowed", -1001234, "Fremde Person"))

    def test_a_new_request_replaces_the_chat(self):
        db.set_user_status(self.path, 77, "pending", "Fremde Person", -1001)
        db.set_user_status(self.path, 77, "denied")
        db.set_user_status(self.path, 77, "pending", None, -1002)           # nach der Ablehnung erneut gefragt, aus anderer Gruppe
        self.assertEqual(db.get_user(self.path, 77)["request_chat_id"], -1002)

    def test_a_user_without_a_request_chat_has_none(self):
        db.set_user_status(self.path, 55, "allowed", "Per Hand")           # z.B. mit /adduser
        self.assertIsNone(db.get_user(self.path, 55)["request_chat_id"])

    def test_listing_users_still_works_with_the_new_column(self):
        db.set_user_status(self.path, 1, "allowed", "A", -5)
        db.set_user_status(self.path, 2, "allowed", "B")
        self.assertEqual([u["user_id"] for u in db.list_users(self.path, "allowed")], [1, 2])


class ChatTemplateTests(unittest.TestCase):
    """Eigene Alarm-Templates je Chat (Befehl /template)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "bot.sqlite3")
        db.init_db(self.path)

    def test_a_chat_without_its_own_template_has_none(self):
        self.assertIsNone(db.get_chat_template(self.path, 123))
        self.assertEqual(db.get_chat_templates(self.path), {})

    def test_a_template_is_stored_and_read_back_unchanged(self):
        template = "🚨 <b>{MESSAGE}</b>\n\n{DESCRIPTION_LIST|RIC_LIST}  \n<i>{MATCHED}</i>"
        db.set_chat_template(self.path, 123, template, 5)
        self.assertEqual(db.get_chat_template(self.path, 123), template)             # Umbrüche und Leerzeichen bleiben

    def test_setting_again_replaces_the_template(self):
        db.set_chat_template(self.path, 123, "{MESSAGE}", 5)
        db.set_chat_template(self.path, 123, "{RIC}", 6)
        self.assertEqual(db.get_chat_template(self.path, 123), "{RIC}")
        self.assertEqual(len(db.get_chat_templates(self.path)), 1)

    def test_the_last_editor_is_remembered(self):
        import sqlite3
        db.set_chat_template(self.path, 123, "{MESSAGE}", 5)
        db.set_chat_template(self.path, 123, "{RIC}", 6)
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT updated_by FROM chat_templates WHERE chat_id = 123").fetchone(), (6,))

    def test_the_editor_may_be_unknown(self):
        db.set_chat_template(self.path, 123, "{MESSAGE}")
        self.assertEqual(db.get_chat_template(self.path, 123), "{MESSAGE}")

    def test_private_chats_and_groups_are_separate_entries(self):
        db.set_chat_template(self.path, 5, "{MESSAGE}", 5)
        db.set_chat_template(self.path, -1001234567890, "{RIC}", 5)
        self.assertEqual(db.get_chat_templates(self.path), {5: "{MESSAGE}", -1001234567890: "{RIC}"})

    def test_deleting_returns_whether_there_was_one(self):
        db.set_chat_template(self.path, 123, "{MESSAGE}", 5)
        self.assertTrue(db.delete_chat_template(self.path, 123))
        self.assertFalse(db.delete_chat_template(self.path, 123))
        self.assertIsNone(db.get_chat_template(self.path, 123))

    def test_deleting_leaves_other_chats_alone(self):
        db.set_chat_template(self.path, 1, "{MESSAGE}", 1)
        db.set_chat_template(self.path, 2, "{RIC}", 1)
        db.delete_chat_template(self.path, 1)
        self.assertEqual(db.get_chat_templates(self.path), {2: "{RIC}"})

    def test_quotes_and_special_characters_survive(self):
        template = "<a href=\"https://example.org/?a=1&b=2\">{MESSAGE}</a> 'x' \"y\" ä ö ü ß 🚨"
        db.set_chat_template(self.path, 1, template, 1)
        self.assertEqual(db.get_chat_template(self.path, 1), template)

    def test_templates_do_not_touch_subscriptions_or_users(self):
        db.add_sub(self.path, 1, "ric", "1000011", "RIC: 1000011", False)
        db.set_user_status(self.path, 1, "allowed", "A")
        db.set_chat_template(self.path, 1, "{MESSAGE}", 1)
        db.delete_chat_template(self.path, 1)
        self.assertEqual(len(db.get_all_subs(self.path)), 1)
        self.assertEqual(db.get_user_status(self.path, 1), "allowed")

    def test_an_old_database_without_the_table_gets_it_at_start_and_keeps_its_data(self):
        import sqlite3
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        old = os.path.join(tmp.name, "alt.sqlite3")
        conn = sqlite3.connect(old)
        conn.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY, status TEXT NOT NULL, name TEXT, "
                     "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("INSERT INTO users (user_id, status) VALUES (5, 'allowed')")
        conn.commit()
        conn.close()
        db.init_db(old)
        db.init_db(old)                                                              # zweimal: wiederholbar
        self.assertEqual(db.get_chat_templates(old), {})
        self.assertEqual(db.get_user_status(old, 5), "allowed")
        db.set_chat_template(old, 5, "{MESSAGE}", 5)
        self.assertEqual(db.get_chat_template(old, 5), "{MESSAGE}")


class UserTableMigrationTests(unittest.TestCase):
    """Datenbanken aus früheren Versionen kennen die neue Spalte nicht. Sie werden beim Start ergänzt."""

    def old_database(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "alt.sqlite3")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY, status TEXT NOT NULL, name TEXT, "
                     "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        conn.executemany("INSERT INTO users (user_id, status, name) VALUES (?, ?, ?)",
                         [(5, "allowed", "Alt-Nutzer"), (6, "pending", "Wartend"), (7, "denied", None)])
        conn.commit()
        conn.close()
        return path

    def test_the_column_is_added_and_existing_users_keep_their_data(self):
        path = self.old_database()
        db.init_db(path)
        rows = {u["user_id"]: (u["status"], u["name"], u["request_chat_id"]) for s in ("allowed", "pending", "denied")
                for u in db.list_users(path, s)}
        self.assertEqual(rows, {5: ("allowed", "Alt-Nutzer", None), 6: ("pending", "Wartend", None), 7: ("denied", None, None)})

    def test_migrating_twice_is_harmless(self):
        path = self.old_database()
        db.init_db(path)
        db.set_user_status(path, 6, "pending", None, -100)
        db.init_db(path)
        self.assertEqual(db.get_user(path, 6)["request_chat_id"], -100)

    def test_the_old_user_can_be_updated_afterwards(self):
        path = self.old_database()
        db.init_db(path)
        db.set_user_status(path, 6, "allowed")
        self.assertEqual(db.get_user(path, 6)["status"], "allowed")

    def test_a_fresh_database_has_the_column_from_the_start(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "neu.sqlite3")
        db.init_db(path)
        conn = sqlite3.connect(path)
        columns = [row[1] for row in conn.execute("PRAGMA table_info(users)")]
        conn.close()
        self.assertIn("request_chat_id", columns)


if __name__ == "__main__":
    unittest.main()
